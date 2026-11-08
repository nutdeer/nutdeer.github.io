"""Nutdeer notes and document logger (v3).

This is deliberately a separate program from notes_logger.py and notes_logger_v2.py.  It manages the
same Zola content directory but leaves the old tool untouched.
"""

import calendar
import html
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import tempfile
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
# v3 lives in the site repository, while the older program lives one level up.
REPO_PATH = Path(os.environ.get("NUTDEER_REPO_PATH", SCRIPT_DIR)).expanduser()

DAY_RE = re.compile(r"day\s*=\s*(\d+)")
TOML_STRING_RE_TEMPLATE = r'{key}\s*=\s*"((?:\\.|[^"])*)"'
GIT_TASK_CONTEXT = threading.local()
NETWORK_PROCESSES = set()
NETWORK_PROCESS_LOCK = threading.Lock()


def kill_git_process(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def toml_unescape(value):
    """Decode the limited TOML escapes used by this tool and the old logger."""
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def extract_toml_string(block, key, default=""):
    match = re.search(TOML_STRING_RE_TEMPLATE.format(key=re.escape(key)), block)
    return toml_unescape(match.group(1)) if match else default


def parse_month_entries(month_file, year, month):
    """Return editable entries from every ``[[extra.logs]]`` block in one month.

    The day pattern intentionally matches the existing logger.  Old entries store
    visual line breaks as two spaces, so those are restored to actual line breaks
    before the value is shown in the editor.
    """
    path = Path(month_file)
    if not path.is_file():
        return []

    text = path.read_text(encoding="utf-8")
    parts = text.split("[[extra.logs]]")[1:]
    entries = []
    for block in parts:
        day_match = DAY_RE.search(block)
        if not day_match:
            continue
        day = int(day_match.group(1))
        try:
            date_value = datetime(int(year), int(month), day).strftime("%Y-%m-%d")
        except ValueError:
            continue
        content = extract_toml_string(block, "content").replace("  ", "\n")
        entries.append(
            {
                "date": date_value,
                "day": day,
                "status": extract_toml_string(block, "status"),
                "content": content,
            }
        )
    return sorted(entries, key=lambda entry: entry["day"], reverse=True)


def repository_git(repo, args):
    """Run Git without waiting for terminal password prompts."""
    network = args[0] in ("fetch", "pull", "push", "ls-remote")
    cancel = getattr(GIT_TASK_CONTEXT, "cancel", None)
    if network and cancel is not None and cancel.is_set():
        return False, "已取消网络同步，本地文件和提交保留。"
    try:
        process = subprocess.Popen(
            ["git"] + args, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if network:
            with NETWORK_PROCESS_LOCK:
                NETWORK_PROCESSES.add(process)
        deadline = time.monotonic() + (30 if network else 120)
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                cancelled = network and cancel is not None and cancel.is_set()
                if cancelled or time.monotonic() >= deadline:
                    # Kill the transport too, so inherited output pipes cannot hang communicate().
                    kill_git_process(process)
                    process.communicate(timeout=2)
                    return False, ("已取消网络同步" if cancelled else "Git 操作超时") + "；本地文件和提交保留，推送结果需下次联网核对。"
        output = "\n".join(p.strip() for p in (stdout, stderr) if p.strip())
        return process.returncode == 0, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    finally:
        if network and 'process' in locals():
            with NETWORK_PROCESS_LOCK:
                NETWORK_PROCESSES.discard(process)


def pull_repository(repo):
    """Fast-forward a repository, restoring tracked/untracked work via a named stash.

    Never reset, force-push or discard a stash after a failed restore. The backup's
    object ID is included in the result so it remains recoverable after conflicts.
    """
    ok, conflicts = repository_git(repo, ["diff", "--name-only", "--diff-filter=U"])
    if not ok or conflicts:
        return False, "仓库存在未解决的冲突，请先处理后再拉取。\n" + conflicts
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        ok, marker_path = repository_git(repo, ["rev-parse", "--git-path", marker])
        if not ok:
            return False, marker_path
        path = Path(marker_path)
        if (path if path.is_absolute() else Path(repo) / path).exists():
            return False, "仓库正在合并、变基或拣选提交，请先完成当前操作。"
    ok, _ = repository_git(repo, ["rev-parse", "--verify", "@{u}"])
    if not ok:
        return False, "当前分支未配置远端跟踪分支，无法拉取。"
    ok, dirty = repository_git(repo, ["status", "--porcelain"])
    if not ok:
        return False, dirty
    backup = None
    messages = []
    if dirty:
        _, old_stash = repository_git(repo, ["rev-parse", "--verify", "refs/stash"])
        ok, output = repository_git(repo, ["stash", "push", "--include-untracked", "-m", "Nutdeer v3 pull backup " + datetime.now().isoformat(timespec="seconds")])
        if not ok:
            return False, "临时保存本地改动失败，尚未拉取。\n" + output
        ok, backup = repository_git(repo, ["rev-parse", "--verify", "refs/stash"])
        if not ok or backup == old_stash:
            return False, "未能创建本地改动备份，尚未拉取。"
        messages.append("本地改动已临时保存：" + backup)
    pulled, output = repository_git(repo, ["pull", "--ff-only"])
    messages.append(output)
    if backup:
        restored, output = repository_git(repo, ["stash", "apply", "--index", backup])
        messages.append(output)
        if not restored:
            messages.append("恢复本地改动遇到冲突。备份保留在 Git stash：" + backup + "\n请在同步日志中查看冲突，处理前不要再次推送。")
            return False, "\n".join(messages)
        # Drop only the backup created by this operation, preserving older stashes.
        ok, stashes = repository_git(repo, ["stash", "list", "--format=%H"])
        if ok and backup in stashes.splitlines():
            index = stashes.splitlines().index(backup)
            dropped, detail = repository_git(repo, ["stash", "drop", f"stash@{{{index}}}"])
            if not dropped:
                messages.append("本地改动已恢复；备份仍保留：" + backup + "\n" + detail)
        messages.append("本地改动已恢复。")
    messages.append("拉取完成。" if pulled else "拉取未完成；分支分叉时不会自动合并，请检查上方日志。")
    return pulled, "\n".join(messages)


class LoggerApp:
    BG = "#f5f7f4"
    CARD = "#ffffff"
    INK = "#2b4f3f"
    MUTED = "#6d7f76"
    ACCENT = "#8ba89a"
    ACCENT_DARK = "#557568"
    BORDER = "#d9e3dc"

    def __init__(self, root):
        self.root = root
        self.ui_queue = queue.Queue()
        self.git_busy = False
        self.close_pending = False
        self.skip_sync_on_close = False
        self.git_cancel = threading.Event()
        self.month_cache = {}
        self.calendar_signature = None
        self.calendar_selected = None
        self.month_list_signature = None
        self.calendar_scroll_timer = None
        self.root.after(50, self.drain_ui_queue)
        self.root.title("Nutdeer 日志管理 v3")
        self.root.geometry("1160x850")
        self.root.minsize(1000, 740)
        self.root.configure(bg=self.BG)
        self.setup_style()

        self.status_var = tk.StringVar(value="准备就绪：点击日历选日期；本地浏览可打开网站，保存后网页自动刷新。")
        status_bar = ttk.Frame(root)
        status_bar.pack(fill=tk.X)
        ttk.Button(status_bar, text="强制退出", command=self.force_exit).pack(side=tk.RIGHT, padx=10)
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel", padding=(14, 8)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.root.bind("<Control-Shift-Q>", lambda _event: self.force_exit())

        self.notebook = ttk.Notebook(root, style="App.TNotebook")
        self.notebook.pack(expand=True, fill=tk.BOTH, padx=14, pady=(8, 14))

        self.notes_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.docs_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.preview_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.sync_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.notebook.add(self.notes_tab, text="日常日志 Notes")
        self.notebook.add(self.docs_tab, text="文档日志 Logs / Tech")
        self.notebook.add(self.preview_tab, text="本地浏览")
        self.notebook.add(self.sync_tab, text="同步仓库")

        self.setup_notes_tab()
        self.setup_docs_tab()
        self.setup_preview_tab()
        self.setup_sync_tab()
        self.refresh_month_entries()
        self.choose_note_date(datetime.today().strftime("%Y-%m-%d"))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after_idle(self.auto_sync)

    def ui(self, callback, *args):
        if threading.current_thread() is threading.main_thread():
            callback(*args)
        else:
            self.ui_queue.put((callback, args))

    def drain_ui_queue(self):
        for _ in range(100):
            try:
                callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self.root.after(50, self.drain_ui_queue)

    def set_status(self, message):
        self.ui(self.status_var.set, message)

    def setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("App.TFrame", background=self.BG)
        style.configure("TLabel", background=self.BG, foreground=self.INK, font=("Arial", 10))
        style.configure("Hint.TLabel", background=self.BG, foreground=self.MUTED, font=("Arial", 9))
        style.configure("Status.TLabel", background="#e8f0ea", foreground=self.INK, font=("Arial", 10, "bold"))
        style.configure("Title.TLabel", background=self.BG, foreground=self.INK, font=("Arial", 12, "bold"))
        style.configure("TEntry", fieldbackground=self.CARD, bordercolor=self.BORDER, padding=6)
        style.configure("TCombobox", fieldbackground=self.CARD, bordercolor=self.BORDER, padding=5)
        style.configure("TButton", background=self.ACCENT, foreground="white", borderwidth=0, padding=(10, 7))
        style.map("TButton", background=[("active", self.ACCENT_DARK), ("disabled", "#bdc9c1")])
        style.configure("Soft.TButton", background="#e4eee7", foreground=self.INK)
        style.map("Soft.TButton", background=[("active", "#d3e2d7")])
        style.configure("App.TNotebook", background=self.BG, borderwidth=0)
        style.configure("App.TNotebook.Tab", background="#e2ebe5", foreground=self.INK, padding=(16, 9), font=("Arial", 10, "bold"))
        style.map("App.TNotebook.Tab", background=[("selected", self.CARD)], foreground=[("selected", self.ACCENT_DARK)])
        style.configure("Treeview", background=self.CARD, fieldbackground=self.CARD, foreground=self.INK, rowheight=27, bordercolor=self.BORDER)
        style.configure("Treeview.Heading", background="#e4eee7", foreground=self.INK, font=("Arial", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", self.ACCENT)], foreground=[("selected", "white")])

    def setup_notes_tab(self):
        panes = ttk.Panedwindow(self.notes_tab, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        calendar_panel = ttk.Frame(panes, style="App.TFrame", width=420)
        editor = ttk.Frame(panes, style="App.TFrame", padding=(16, 0, 0, 0))
        panes.add(calendar_panel, weight=0)
        panes.add(editor, weight=1)
        self.calendar_year = datetime.today().year
        nav = ttk.Frame(calendar_panel, style="App.TFrame")
        nav.pack(fill=tk.X)
        ttk.Button(nav, text="上一年", style="Soft.TButton", command=lambda: self.change_calendar_year(-1)).pack(side=tk.LEFT)
        self.calendar_year_label = ttk.Label(nav, style="Title.TLabel", anchor=tk.CENTER)
        self.calendar_year_label.pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(nav, text="下一年", style="Soft.TButton", command=lambda: self.change_calendar_year(1)).pack(side=tk.RIGHT)
        ttk.Button(calendar_panel, text="回到今天", style="Soft.TButton", command=self.go_to_today).pack(pady=6)
        ttk.Label(calendar_panel, text="蓝：工作  绿：学习  金：休息  灰：暂无记录", style="Hint.TLabel").pack(pady=(0, 8))
        body = ttk.Frame(calendar_panel, style="App.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        self.calendar_canvas = tk.Canvas(body, width=410, bg=self.BG, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.calendar_canvas.yview)
        self.calendar_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.calendar_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.calendar_grid = ttk.Frame(self.calendar_canvas, style="App.TFrame")
        self.calendar_canvas.create_window((0, 0), window=self.calendar_grid, anchor="nw")
        self.calendar_grid.bind("<Configure>", self.update_calendar_scrollregion)
        self.calendar_canvas.bind("<Configure>", self.update_calendar_scrollregion)
        self.calendar_canvas.bind("<Map>", lambda _e: self.scroll_calendar_to_selected())
        self.calendar_hint = tk.StringVar(value="点击日期，载入旧日记或开始新的一天。")
        ttk.Label(calendar_panel, textvariable=self.calendar_hint, style="Hint.TLabel", wraplength=400).pack(fill=tk.X, pady=(6, 0))
        editor.columnconfigure(0, weight=1)
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(3, weight=1)

        ttk.Label(editor, text="日常日志", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(editor, text="点击左侧日期，即可读取或补写日记。", style="Hint.TLabel").grid(row=0, column=1, sticky="e")

        form = ttk.Frame(editor, style="App.TFrame")
        form.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="日期 (YYYY-MM-DD)").grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.date_entry = ttk.Entry(form)
        self.date_entry.insert(0, datetime.today().strftime("%Y-%m-%d"))
        self.date_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        self.date_entry.bind("<FocusOut>", lambda _event: self.refresh_month_entries())
        self.date_entry.bind("<Return>", lambda _event: self.refresh_month_entries())
        ttk.Label(form, text="状态").grid(row=0, column=2, sticky="w", padx=(0, 7))
        self.status_combo = ttk.Combobox(form, values=["工作", "学习", "休息", "Fix"], state="readonly")
        self.status_combo.current(0)
        self.status_combo.grid(row=0, column=3, sticky="ew")

        ttk.Label(editor, text="内容").grid(row=2, column=0, sticky="w", pady=(10, 3))
        self.content_text = tk.Text(editor, height=9, wrap=tk.WORD, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.BORDER, padx=9, pady=8)
        self.content_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        ttk.Label(editor, text="Fix：第一行是标题，余下是解决内容；只保留标题则删除同名 Fix。", style="Hint.TLabel").grid(row=4, column=0, columnspan=2, sticky="nw", pady=(5, 0))

        button_row = ttk.Frame(editor, style="App.TFrame")
        button_row.grid(row=5, column=0, columnspan=2, pady=(8, 14))
        ttk.Button(button_row, text="新建空白", style="Soft.TButton", command=self.clear_note_form).pack(side=tk.LEFT, padx=4)
        self.save_note_btn = ttk.Button(button_row, text="保存日记", command=self.save_note_log)
        self.save_note_btn.pack(side=tk.LEFT, padx=4)

        ttk.Button(button_row, text="本地浏览", style="Soft.TButton", command=self.browse_local_site).pack(side=tk.LEFT, padx=4)

        list_header = ttk.Frame(editor, style="App.TFrame")
        list_header.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(list_header, text="本月已有日志", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Button(list_header, text="刷新列表", style="Soft.TButton", command=self.refresh_month_entries).pack(side=tk.RIGHT)

        list_frame = ttk.Frame(editor, style="App.TFrame")
        list_frame.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        editor.rowconfigure(7, weight=1)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.month_tree = ttk.Treeview(list_frame, columns=("date", "status", "summary"), show="headings", height=6)
        self.month_tree.heading("date", text="日期")
        self.month_tree.heading("status", text="状态")
        self.month_tree.heading("summary", text="内容摘要")
        self.month_tree.column("date", width=105, stretch=False, anchor=tk.CENTER)
        self.month_tree.column("status", width=80, stretch=False, anchor=tk.CENTER)
        self.month_tree.column("summary", width=530, anchor=tk.W)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.month_tree.yview)
        self.month_tree.configure(yscrollcommand=scrollbar.set)
        self.month_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.month_tree.bind("<<TreeviewSelect>>", self.load_selected_month_entry)
        self.month_entries = {}
        self.note_baseline = self.note_form_snapshot()
        self.loaded_note_date = None
        self.note_disk_entry = None

    def note_form_snapshot(self):
        return (self.date_entry.get(), self.status_combo.get(), self.content_text.get("1.0", "end-1c"))

    def has_note_draft(self):
        return self.note_form_snapshot() != self.note_baseline

    def allow_note_navigation(self):
        return not self.has_note_draft() or messagebox.askyesno("未保存的日记", "当前编辑尚未保存。要放弃这些编辑并切换日期吗？")

    def clear_note_form(self):
        self.choose_note_date(datetime.today().strftime("%Y-%m-%d"), blank=True)

    def change_calendar_year(self, delta):
        year = self.calendar_year + delta
        if 1 <= year <= 9999:
            self.calendar_year = year
            self.render_calendar()

    def month_file_signature(self, year, month):
        path = REPO_PATH / "content" / "notes" / f"{year:04d}-{month:02d}.md"
        try:
            stat = path.stat()
            return str(path), stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return str(path), None, None

    def get_month_entries(self, year, month):
        signature = self.month_file_signature(year, month)
        key = (year, month)
        cached = self.month_cache.get(key)
        if cached is None or cached[0] != signature:
            entries = parse_month_entries(signature[0], year, month)
            self.month_cache[key] = (signature, entries)
        return self.month_cache[key][1]

    def update_calendar_selection(self):
        selected = self.date_entry.get().strip()
        today = datetime.today().strftime("%Y-%m-%d")
        for day in {self.calendar_selected, selected}:
            if day in self.calendar_cells:
                canvas, item = self.calendar_cells[day]
                canvas.itemconfig(item, outline=self.ACCENT_DARK if day == selected else ("#9d8cc7" if day == today else ""), width=2)
        self.calendar_selected = selected

    def render_calendar(self):
        signature = (self.calendar_year, datetime.today().date(),
                     tuple(self.month_file_signature(self.calendar_year, m) for m in range(1, 13)))
        if signature == self.calendar_signature:
            self.update_calendar_selection()
            return
        self.calendar_signature = signature
        for widget in self.calendar_grid.winfo_children():
            widget.destroy()
        self.calendar_year_label.config(text=f"{self.calendar_year} 年")
        self.month_cards, self.calendar_cells, self.calendar_summaries = {}, {}, {}
        colors = {"工作": "#426895", "学习": "#8ba89a", "休息": "#bfa46f", "Fix": "#997bb0"}
        today = datetime.today().strftime("%Y-%m-%d")
        for month in range(1, 13):
            card = tk.Canvas(self.calendar_grid, width=190, height=218, bg=self.CARD,
                             highlightthickness=1, highlightbackground=self.BORDER)
            card.grid(row=(month-1)//2, column=(month-1)%2, padx=4, pady=5)
            self.month_cards[month] = card
            card.create_text(95, 20, text=f"{month} 月", fill=self.INK, font=("Arial", 11, "bold"))
            entries = {entry["day"]: entry for entry in self.get_month_entries(self.calendar_year, month)}
            for col, label in enumerate("一二三四五六日"):
                card.create_text(23 + col*24, 45, text=label, fill=self.MUTED, font=("Arial", 9))
            for row, week in enumerate(calendar.monthcalendar(self.calendar_year, month)):
                for col, day in enumerate(week):
                    if not day:
                        continue
                    date_str = f"{self.calendar_year:04d}-{month:02d}-{day:02d}"
                    entry = entries.get(day)
                    color = colors.get(entry["status"], "#8ba89a") if entry else ("#d2d7dd" if col >= 5 else "#e9ecef")
                    x, y = 13 + col*24, 62 + row*24
                    tag = "day:" + date_str
                    item = card.create_rectangle(x, y, x+20, y+20, fill=color,
                        outline="#9d8cc7" if date_str == today else "", width=2, tags=(tag,))
                    card.create_text(x+10, y+10, text=str(day), fill="white" if entry else "#65736c", font=("Arial", 9), tags=(tag,))
                    self.calendar_cells[date_str] = (card, item)
                    self.calendar_summaries[date_str] = "暂无记录" if not entry else entry["status"] + " · " + " ".join(entry["content"].split())[:100]
            card.bind("<Button-1>", self.on_calendar_click)
            card.bind("<Motion>", self.on_calendar_hover)
            card.bind("<MouseWheel>", lambda e: self.calendar_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            card.bind("<Button-4>", lambda _e: self.calendar_canvas.yview_scroll(-1, "units"))
            card.bind("<Button-5>", lambda _e: self.calendar_canvas.yview_scroll(1, "units"))
        self.update_calendar_selection()

    def calendar_date_at(self, event):
        for item in reversed(event.widget.find_overlapping(event.x, event.y, event.x, event.y)):
            for tag in event.widget.gettags(item):
                if tag.startswith("day:"):
                    return tag[4:]
        return None

    def on_calendar_click(self, event):
        date_str = self.calendar_date_at(event)
        if date_str:
            self.choose_note_date(date_str)

    def on_calendar_hover(self, event):
        date_str = self.calendar_date_at(event)
        event.widget.config(cursor="hand2" if date_str else "")
        if date_str:
            summary = date_str + "  " + self.calendar_summaries[date_str]
            if self.calendar_hint.get() != summary:
                self.calendar_hint.set(summary)

    def update_calendar_scrollregion(self, _event=None):
        bounds = self.calendar_canvas.bbox("all")
        if not bounds:
            return
        # Leave enough space below December to align the final month row at the top.
        last_card = getattr(self, "month_cards", {}).get(12)
        bottom = max(bounds[3], last_card.winfo_y() + self.calendar_canvas.winfo_height()) if last_card else bounds[3]
        self.calendar_canvas.configure(scrollregion=(0, 0, bounds[2], bottom))

    def go_to_today(self):
        today = datetime.today()
        date_str = today.strftime("%Y-%m-%d")
        if self.date_entry.get().strip() != date_str:
            self.choose_note_date(date_str)
        if self.date_entry.get().strip() == date_str:
            self.calendar_year = today.year
            self.render_calendar()
            self.root.after_idle(self.scroll_calendar_to_selected)

    def scroll_calendar_to_selected(self):
        date_value = self.month_from_date_field()
        if date_value and date_value.year == self.calendar_year:
            self.calendar_canvas.update_idletasks()
            self.update_calendar_scrollregion()
            top = self.month_cards[date_value.month].winfo_y()
            height = max(1, float(self.calendar_canvas.cget("scrollregion").split()[3]))
            self.calendar_canvas.yview_moveto(max(0, top - 5) / height)

    def choose_note_date(self, date_str, blank=False):
        if self.git_busy:
            self.status_var.set("仓库同步中，请稍候再切换日期。")
            return
        if not self.allow_note_navigation():
            return
        previous_date = self.month_from_date_field()
        date_value = datetime.strptime(date_str, "%Y-%m-%d")
        entries = self.get_month_entries(date_value.year, date_value.month)
        disk_entry = next((e for e in entries if e["date"] == date_str), None)
        entry = None if blank else disk_entry
        self.date_entry.delete(0, tk.END)
        self.date_entry.insert(0, date_str)
        self.status_combo.set(entry["status"] if entry else "工作")
        self.content_text.delete("1.0", tk.END)
        if entry:
            self.content_text.insert("1.0", entry["content"])
        self.loaded_note_date = date_str
        self.note_disk_entry = (disk_entry["status"], disk_entry["content"]) if disk_entry else None
        self.note_baseline = self.note_form_snapshot()
        self.calendar_year = date_value.year
        self.refresh_month_entries()
        if previous_date is None or (previous_date.year, previous_date.month) != (date_value.year, date_value.month):
            if self.calendar_scroll_timer:
                self.root.after_cancel(self.calendar_scroll_timer)
            self.calendar_scroll_timer = self.root.after(100, self.scroll_calendar_to_selected)
        self.status_var.set(f"{date_str}：" + ("已载入日记。" if entry else "暂无记录，可以开始写。"))

    def month_from_date_field(self):
        try:
            return datetime.strptime(self.date_entry.get().strip(), "%Y-%m-%d")
        except ValueError:
            return None

    def refresh_month_entries(self):
        self.render_calendar()
        date_value = self.month_from_date_field()
        if not date_value:
            return
        signature = self.month_file_signature(date_value.year, date_value.month)
        if signature == self.month_list_signature:
            return
        self.month_list_signature = signature
        for item in self.month_tree.get_children():
            self.month_tree.delete(item)
        self.month_entries = {}
        for entry in self.get_month_entries(date_value.year, date_value.month):
            summary = " ".join(entry["content"].split()) or "（无内容）"
            if len(summary) > 75:
                summary = summary[:74] + "…"
            item = self.month_tree.insert("", tk.END, values=(entry["date"], entry["status"], summary))
            self.month_entries[item] = entry

    def load_selected_month_entry(self, _event):
        selected = self.month_tree.selection()
        if selected and selected[0] in self.month_entries:
            date_str = self.month_entries[selected[0]]["date"]
            if date_str != self.loaded_note_date:
                self.choose_note_date(date_str)

    def toml_escape(self, value):
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n")

    def save_note_log(self):
        if self.git_busy:
            return
        date_str = self.date_entry.get().strip()
        status = self.status_combo.get()
        content = self.content_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "内容不能为空！")
            return
        if status == "Fix":
            self.save_fix_from_note(date_str, content)
            return
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("错误", "日期格式错误，请使用 YYYY-MM-DD")
            return

        notes_dir = REPO_PATH / "content" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        file_path = notes_dir / f"{dt:%Y-%m}.md"
        current = next((entry for entry in parse_month_entries(file_path, dt.year, dt.month)
                        if entry["date"] == date_str), None)
        current_signature = (current["status"], current["content"]) if current else None
        if date_str == self.loaded_note_date and current_signature != self.note_disk_entry:
            messagebox.showwarning("日记已更新", "这一天的本地内容已被其他操作更新。草稿仍在编辑框中，请先复制草稿，再点击日历重新载入并核对。")
            return
        escaped_content = self.toml_escape(content.replace("\n", "  "))
        new_block = f'\nday = {dt.day}\nstatus = "{self.toml_escape(status)}"\ncontent = "{escaped_content}"\n\n'

        if not file_path.exists():
            file_path.write_text(
                f'''+++\ntitle = "{dt.year}年{dt.month}月日志"\ndate = {dt:%Y-%m}-01\n# 【必读说明】\n# date 是必需的，Zola 靠它来判断哪个月在前面，哪个月在后面。title 可留作备忘。\n# status 状态可选词："工作"、"休息"、"学习"。\n\n[[extra.logs]]{new_block}+++\n''',
                encoding="utf-8",
            )
            action_msg = "已创建新月份并写入。"
        else:
            text = file_path.read_text(encoding="utf-8")
            parts = text.split("+++")
            if len(parts) < 3:
                messagebox.showerror("错误", "Markdown 文件格式异常，找不到成对的 +++。")
                return
            header, log_blocks = parts[1].split("[[extra.logs]]")[0], parts[1].split("[[extra.logs]]")[1:]
            parsed_blocks, replaced = [], False
            for block in log_blocks:
                day_match = DAY_RE.search(block)
                if not day_match:
                    parsed_blocks.append((-1, block))
                elif int(day_match.group(1)) == dt.day:
                    parsed_blocks.append((dt.day, new_block))
                    replaced = True
                else:
                    parsed_blocks.append((int(day_match.group(1)), block))
            if not replaced:
                parsed_blocks.append((dt.day, new_block))
                action_msg = "已追加新日志。"
            else:
                action_msg = f"已覆盖 {dt.day} 号的旧日志。"
            parsed_blocks.sort(key=lambda item: item[0])
            new_frontmatter = header + "".join("[[extra.logs]]" + block for _, block in parsed_blocks)
            parts[1] = new_frontmatter if new_frontmatter.endswith("\n") else new_frontmatter + "\n"
            file_path.write_text("+++".join(parts), encoding="utf-8")

        self.note_baseline = self.note_form_snapshot()
        self.loaded_note_date = date_str
        self.note_disk_entry = (status, content.replace("\n", "  ").replace("  ", "\n"))
        self.status_var.set(f"{date_str} 已保存，本地网页会自动刷新；尚未推送。")
        self.refresh_month_entries()

    def build_fix_block(self, date_str, title, content):
        return f'\n[[extra.fixes]]\ndate = {date_str}\ntitle = "{self.toml_escape(title)}"\ncontent = "{self.toml_escape(content)}"\n'

    def save_fix_from_note(self, date_str, raw_content):
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("错误", "日期格式错误，请使用 YYYY-MM-DD")
            return
        lines = [line.rstrip() for line in raw_content.splitlines()]
        title, content = lines[0].strip(), "\n".join(lines[1:]).strip()
        if not title:
            messagebox.showwarning("提示", "Fix 第一行标题不能为空！")
            return
        file_path = REPO_PATH / "content" / "fixes" / "_index.md"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.exists():
            file_path.write_text('+++\ntitle = "Fixes"\ndescription = "Small troubleshooting notes and compact fixes."\ntemplate = "fixes_list.html"\n+++\n', encoding="utf-8")
        text = file_path.read_text(encoding="utf-8")
        parts = text.split("+++")
        if len(parts) < 3:
            messagebox.showerror("错误", "Fixes 文件格式异常，找不到成对的 +++。")
            return
        header, old_blocks = parts[1].split("[[extra.fixes]]")[0].rstrip(), parts[1].split("[[extra.fixes]]")[1:]
        updated, found = [], False
        for block in old_blocks:
            old_title = extract_toml_string(block, "title")
            if old_title != title:
                updated.append(block)
                continue
            found = True
            if content:
                updated.append(self.build_fix_block(date_str, title, content).replace("[[extra.fixes]]", "", 1))
        if content and not found:
            updated.append(self.build_fix_block(date_str, title, content).replace("[[extra.fixes]]", "", 1))
        if not content and not found:
            self.status_var.set("Fix 不存在，未写入。")
            return
        parts[1] = header + "".join("\n[[extra.fixes]]" + block.rstrip() + "\n" for block in updated)
        file_path.write_text("+++".join(parts), encoding="utf-8")
        self.note_baseline = self.note_form_snapshot()
        self.status_var.set("Fix 已保存到本地，本地网页会自动刷新；尚未推送。")

    def setup_docs_tab(self):
        self.docs_tab.columnconfigure(0, weight=1)
        ttk.Label(self.docs_tab, text="文档日志", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.docs_tab, text="正文专心写内容，frontmatter 由这里的表单生成。", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 12))

        form = ttk.Frame(self.docs_tab, style="App.TFrame")
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.doc_mode_var = tk.StringVar(value="markdown")
        mode_row = ttk.Frame(form, style="App.TFrame")
        ttk.Label(mode_row, text="导入模式").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(mode_row, text="Markdown 文档", value="markdown", variable=self.doc_mode_var, command=self.update_doc_mode).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Radiobutton(mode_row, text="PDF 文档", value="pdf", variable=self.doc_mode_var, command=self.update_doc_mode).pack(side=tk.LEFT)
        mode_row.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.doc_file_label = ttk.Label(form, text="Markdown 文件")
        self.doc_file_label.grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        file_row = ttk.Frame(form, style="App.TFrame")
        file_row.grid(row=1, column=1, sticky="ew", pady=4)
        file_row.columnconfigure(0, weight=1)
        self.doc_file_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self.doc_file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(file_row, text="选择文件", style="Soft.TButton", command=self.select_doc_file).grid(row=0, column=1, padx=(8, 0))

        self.doc_date_var = tk.StringVar(value=datetime.today().strftime("%Y-%m-%d"))
        self.doc_description_var = tk.StringVar()
        self.doc_tags_var = tk.StringVar()
        self.doc_title_var = tk.StringVar()
        self.add_doc_entry(form, 2, "日期 (YYYY-MM-DD)", self.doc_date_var)
        self.add_doc_entry(form, 3, "描述", self.doc_description_var)
        self.add_doc_entry(form, 4, "标签（逗号分隔）", self.doc_tags_var)
        self.title_row = self.add_doc_entry(form, 5, "PDF 标题（必填）", self.doc_title_var)

        self.doc_with_assets_var = tk.BooleanVar(value=False)
        self.assets_row = ttk.Frame(form, style="App.TFrame")
        ttk.Checkbutton(self.assets_row, text="带图片资源：复制同名 .assets 文件夹，并保存为 index.md", variable=self.doc_with_assets_var, command=self.update_doc_target_hint).pack(anchor=tk.W)
        self.assets_row.grid(row=6, column=0, columnspan=2, sticky="w", pady=(7, 3))

        self.doc_section_var = tk.StringVar(value="logs")
        self.doc_target_var = tk.StringVar()
        self.add_doc_combo(form, 7, "目标栏目", self.doc_section_var, ["logs", "tech"])
        self.add_doc_entry(form, 8, "目标名称", self.doc_target_var)
        self.doc_target_var.trace_add("write", lambda *_args: self.update_doc_target_hint())
        self.doc_target_hint = ttk.Label(form, text="", style="Hint.TLabel")
        self.doc_target_hint.grid(row=9, column=1, sticky="w", pady=(3, 8))
        self.save_doc_btn = ttk.Button(form, text="保存到所选栏目", command=self.import_doc_log)
        self.save_doc_btn.grid(row=10, column=0, columnspan=2, pady=(8, 4))
        self.doc_help = ttk.Label(form, text="", style="Hint.TLabel", wraplength=720, justify=tk.LEFT)
        self.doc_help.grid(row=11, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.update_doc_mode()

    def add_doc_entry(self, parent, row, label, variable):
        label_widget = ttk.Label(parent, text=label)
        label_widget.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        return label_widget, entry

    def add_doc_combo(self, parent, row, label, variable, values):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        combo.grid(row=row, column=1, sticky="ew", pady=4)
        combo.bind("<<ComboboxSelected>>", lambda _event: self.update_doc_target_hint())

    def update_doc_mode(self):
        is_pdf = self.doc_mode_var.get() == "pdf"
        self.doc_file_label.config(text="PDF 文件" if is_pdf else "Markdown 文件")
        if is_pdf:
            for widget in self.title_row:
                widget.grid()
            self.assets_row.grid_remove()
            self.doc_help.config(text="PDF 会复制到目标目录，并生成含内嵌预览和下载链接的 index.md。")
        else:
            for widget in self.title_row:
                widget.grid_remove()
            self.assets_row.grid()
            self.doc_help.config(text="Markdown 第一行必须是 # 标题；工具会摘出标题并自动生成 frontmatter。已手写 +++ 的旧文档会原样复制。")
        self.update_doc_target_hint()

    def select_doc_file(self):
        is_pdf = self.doc_mode_var.get() == "pdf"
        path = filedialog.askopenfilename(
            title="选择 PDF 文档" if is_pdf else "选择 Markdown 文档",
            filetypes=[("PDF files", "*.pdf")] if is_pdf else [("Markdown files", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return
        self.doc_file_var.set(path)
        if not self.doc_target_var.get().strip():
            self.doc_target_var.set(Path(path).stem)
        self.update_doc_target_hint()

    def update_doc_target_hint(self):
        target = self.doc_target_var.get().strip() or "<目标名称>"
        section = self.doc_section_var.get()
        if self.doc_mode_var.get() == "pdf" or self.doc_with_assets_var.get():
            destination = Path("content") / section / target / "index.md"
        else:
            destination = Path("content") / section / f"{target}.md"
        self.doc_target_hint.config(text=f"将保存到：{destination}")

    def validate_doc_form(self, suffix):
        src_value = self.doc_file_var.get().strip()
        target = self.doc_target_var.get().strip()
        if not src_value:
            messagebox.showwarning("提示", "请选择文档文件。")
            return None
        src = Path(src_value)
        if not src.is_file() or src.suffix.lower() != suffix:
            messagebox.showerror("错误", f"请选择 {suffix} 文件。")
            return None
        try:
            datetime.strptime(self.doc_date_var.get().strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("错误", "日期格式错误，请使用 YYYY-MM-DD。")
            return None
        if not target:
            messagebox.showwarning("提示", "目标名称不能为空。")
            return None
        if not self.is_safe_target_name(target):
            messagebox.showerror("错误", "目标名称不能包含路径分隔符，也不能是 . 或 ..。")
            return None
        return src, target

    def build_frontmatter(self, title):
        tags = [tag.strip() for tag in self.doc_tags_var.get().split(",") if tag.strip()]
        tags_value = ", ".join(f'"{self.toml_escape(tag)}"' for tag in tags)
        return (
            "+++\n"
            f'title = "{self.toml_escape(title)}"\n'
            f"date = {self.doc_date_var.get().strip()}\n"
            f'description = "{self.toml_escape(self.doc_description_var.get().strip())}"\n'
            "[taxonomies]\n"
            f"tags = [{tags_value}]\n"
            "+++\n\n"
        )

    def import_doc_log(self):
        if self.git_busy:
            return
        if self.doc_mode_var.get() == "pdf":
            self.import_pdf_log()
        else:
            self.import_markdown_log()

    def import_markdown_log(self):
        validated = self.validate_doc_form(".md")
        if not validated:
            return
        src, target = validated
        raw = src.read_text(encoding="utf-8")
        legacy = raw.lstrip("\ufeff").startswith("+++")
        if legacy:
            output = raw
        else:
            lines = raw.splitlines(keepends=True)
            if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
                messagebox.showwarning("需要标题", "请在文档开头加 # 标题，然后再导入。")
                return
            title = lines[0][2:].strip()
            output = self.build_frontmatter(title) + "".join(lines[1:]).lstrip("\n")
        section_dir = REPO_PATH / "content" / self.doc_section_var.get()
        try:
            if self.doc_with_assets_var.get():
                assets = src.parent / f"{src.stem}.assets"
                if not assets.is_dir():
                    messagebox.showerror("错误", f"找不到图片资源文件夹：\n{assets}")
                    return
                target_dir = section_dir / target
                target_md = target_dir / "index.md"
                if target_dir.exists() and not messagebox.askyesno("确认覆盖", f"目标已存在，是否覆盖 index.md 与图片资源？\n{target_dir}"):
                    return
                target_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(output, encoding="utf-8")
                target_assets = target_dir / assets.name
                if target_assets.exists():
                    shutil.rmtree(target_assets)
                shutil.copytree(assets, target_assets)
                saved_path = Path("content") / self.doc_section_var.get() / target / "index.md"
            else:
                target_path = section_dir / f"{target}.md"
                if target_path.exists() and not messagebox.askyesno("确认覆盖", f"目标已存在，是否覆盖？\n{target_path}"):
                    return
                section_dir.mkdir(parents=True, exist_ok=True)
                target_path.write_text(output, encoding="utf-8")
                saved_path = Path("content") / self.doc_section_var.get() / f"{target}.md"
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("错误", f"保存失败：\n{exc}")
            return
        self.finish_doc_import(saved_path, "旧 frontmatter 已原样复制。" if legacy else "已自动生成 frontmatter。")

    def import_pdf_log(self):
        validated = self.validate_doc_form(".pdf")
        if not validated:
            return
        src, target = validated
        title = self.doc_title_var.get().strip()
        if not title:
            messagebox.showwarning("提示", "PDF 标题不能为空。")
            return
        target_dir = REPO_PATH / "content" / self.doc_section_var.get() / target
        target_pdf = target_dir / src.name
        target_index = target_dir / "index.md"
        if target_dir.exists() and not messagebox.askyesno("确认覆盖", f"目标目录已存在，是否覆盖 PDF 与 index.md？\n{target_dir}"):
            return
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target_pdf)
            url_name = quote(src.name)
            target_index.write_text(
                self.build_frontmatter(title)
                + f'<embed src="{html.escape(url_name, quote=True)}" type="application/pdf" width="100%" height="800px">\n\n'
                + f"[下载 PDF]({url_name})\n",
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("错误", f"保存失败：\n{exc}")
            return
        self.finish_doc_import(Path("content") / self.doc_section_var.get() / target / "index.md", "PDF 已复制，并生成预览页。")

    def finish_doc_import(self, saved_path, detail):
        self.status_var.set("文档已保存到本地，尚未 Push。")
        self.status_var.set(f"{saved_path} 已保存，本地网页会自动刷新。")

    @staticmethod
    def is_safe_target_name(target_name):
        return target_name not in {".", ".."} and os.path.basename(target_name) == target_name and "\\" not in target_name

    def setup_preview_tab(self):
        self.zola_process = None
        self.preview_ready = False
        self.preview_wants_open = False
        self.preview_base_url = None
        ttk.Label(self.preview_tab, text="本地浏览", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(self.preview_tab, text="一键打开本地网站。保持网页开着，保存日记或文档后会自动刷新，无需推送。", style="Hint.TLabel").pack(anchor=tk.W, pady=(2, 12))
        control_row = ttk.Frame(self.preview_tab, style="App.TFrame")
        control_row.pack(fill=tk.X)
        ttk.Label(control_row, text="端口").pack(side=tk.LEFT, padx=(0, 6))
        self.preview_port_var = tk.StringVar(value="1111")
        self.preview_port_entry = ttk.Entry(control_row, textvariable=self.preview_port_var, width=8)
        self.preview_port_entry.pack(side=tk.LEFT, padx=(0, 12))
        self.start_preview_btn = ttk.Button(control_row, text="打开本地网站", command=self.browse_local_site)
        self.stop_preview_btn = ttk.Button(control_row, text="停止本地服务", style="Soft.TButton", command=self.stop_preview, state=tk.DISABLED)
        self.open_browser_btn = ttk.Button(control_row, text="再次打开网页", style="Soft.TButton", command=self.open_preview_browser, state=tk.DISABLED)
        self.start_preview_btn.pack(side=tk.LEFT, padx=4)
        self.stop_preview_btn.pack(side=tk.LEFT, padx=4)
        self.open_browser_btn.pack(side=tk.LEFT, padx=4)
        self.preview_log = tk.Text(self.preview_tab, height=20, state=tk.DISABLED, bg=self.CARD, fg=self.INK, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.BORDER, padx=9, pady=8)
        self.preview_log.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    def log_preview_msg(self, message):
        def append():
            self.preview_log.config(state=tk.NORMAL)
            self.preview_log.insert(tk.END, message + "\n")
            lines = int(self.preview_log.index("end-1c").split(".")[0])
            if lines > 300:
                self.preview_log.delete("1.0", f"{lines - 300}.0")
            self.preview_log.see(tk.END)
            self.preview_log.config(state=tk.DISABLED)
        self.ui(append)

    def preview_url(self):
        base = self.preview_base_url or f"http://127.0.0.1:{self.preview_port_var.get().strip()}"
        return base + "/notes/"

    def browse_local_site(self):
        if self.zola_process and self.zola_process.poll() is None:
            if self.preview_ready:
                self.open_preview_browser()
            else:
                self.preview_wants_open = True
                self.status_var.set("本地网站正在生成，就绪后会自动打开。")
            return
        self.start_preview()

    def start_preview(self):
        if self.zola_process and self.zola_process.poll() is None:
            return
        port = self.preview_port_var.get().strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            messagebox.showerror("错误", "端口必须是 1-65535 的数字。")
            return
        zola = shutil.which("zola")
        if zola is None:
            messagebox.showerror("错误", "找不到 zola 命令，请先安装 Zola。")
            return
        output = tempfile.TemporaryDirectory(prefix="nutdeer-preview-")
        try:
            process = subprocess.Popen(
                [zola, "serve", "--interface", "127.0.0.1", "--port", port,
                 "--output-dir", str(Path(output.name) / "site"), "--force", "--debounce", "250"],
                cwd=REPO_PATH, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            output.cleanup()
            messagebox.showerror("错误", f"启动失败：\n{exc}")
            return
        self.zola_process = process
        self.preview_ready = False
        self.preview_wants_open = True
        self.preview_base_url = f"http://127.0.0.1:{port}"
        self.start_preview_btn.config(state=tk.DISABLED)
        self.preview_port_entry.config(state=tk.DISABLED)
        self.stop_preview_btn.config(state=tk.NORMAL)
        self.status_var.set("正在生成本地网站，就绪后会自动打开浏览器…")
        threading.Thread(target=self.read_preview_output, args=(process,), daemon=True).start()
        threading.Thread(target=self.watch_preview_process, args=(process, output), daemon=True).start()

    def read_preview_output(self, process):
        try:
            for line in process.stdout:
                self.log_preview_msg(line.rstrip())
                if "Web server is available at" in line or "Listening for requests" in line:
                    self.ui(self.on_preview_ready, process)
        finally:
            process.stdout.close()

    def on_preview_ready(self, process):
        if self.zola_process is not process or process.poll() is not None or self.preview_ready:
            return
        self.preview_ready = True
        self.start_preview_btn.config(state=tk.NORMAL)
        self.open_browser_btn.config(state=tk.NORMAL)
        self.status_var.set(f"本地网站运行中：{self.preview_url()}；保存后网页自动刷新。")
        if self.preview_wants_open:
            self.preview_wants_open = False
            webbrowser.open(self.preview_url(), new=1)

    def watch_preview_process(self, process, output):
        return_code = process.wait()
        output.cleanup()
        def on_exit():
            if self.zola_process is process:
                self.zola_process = None
                self.preview_ready = False
                self.preview_wants_open = False
                self.start_preview_btn.config(state=tk.NORMAL)
                self.preview_port_entry.config(state=tk.NORMAL)
                self.stop_preview_btn.config(state=tk.DISABLED)
                self.open_browser_btn.config(state=tk.DISABLED)
                self.status_var.set(f"本地网站启动或运行失败（代码 {return_code}），请查看“本地浏览”日志。" if return_code else "本地网站已停止。")
        self.ui(on_exit)

    def stop_preview(self):
        process = self.zola_process
        if not process or process.poll() is not None:
            return
        self.preview_wants_open = False
        process.terminate()
        def stop():
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        threading.Thread(target=stop, daemon=True).start()

    def open_preview_browser(self):
        if self.zola_process and self.zola_process.poll() is None and self.preview_ready:
            webbrowser.open(self.preview_url())

    def force_exit(self):
        if not messagebox.askyesno("强制退出", "将备份编辑框内容并立即退出，不等待 Git 同步。\n未完成的仓库操作及 stash 备份将在下次启动时保留供检查。\n确定强制退出？"):
            return
        try:
            backup_dir = REPO_PATH / ".notes_logger_drafts"
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
            data = {"note": self.note_form_snapshot(), "documents": {}}
            for name in ("doc_mode_var", "doc_file_var", "doc_target_var", "doc_title_var", "doc_section_var", "doc_with_assets_var", "doc_date_var", "doc_description_var", "doc_tags_var"):
                variable = getattr(self, name, None)
                if variable is not None:
                    data["documents"][name] = variable.get()
            with backup_path.open("w", encoding="utf-8") as backup:
                json.dump(data, backup, ensure_ascii=False, indent=2)
                backup.flush()
                os.fsync(backup.fileno())
        except OSError as exc:
            messagebox.showerror("草稿备份失败", "尚未退出，请先复制编辑框内容。\n" + str(exc))
            return
        self.git_cancel.set()
        with NETWORK_PROCESS_LOCK:
            for process in list(NETWORK_PROCESSES):
                kill_git_process(process)
        if self.zola_process is not None and self.zola_process.poll() is None:
            try:
                self.zola_process.terminate()
            except OSError:
                pass
        os._exit(0)

    def on_close(self):
        if self.git_busy:
            self.close_pending = True
            self.skip_sync_on_close = True
            self.git_cancel.set()
            self.status_var.set("正在取消网络操作；恢复本地文件后保存并退出。")
            return
        self.close_pending = False
        if self.has_note_draft():
            try:
                self.save_note_log()
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))
                return
            if self.has_note_draft():
                return
        if self.skip_sync_on_close:
            self.stop_preview()
            self.root.destroy()
        else:
            self.auto_sync(close_after=True)

    def auto_sync(self, close_after=False):
        def sync():
            ok, output = pull_repository(REPO_PATH)
            if not ok:
                return False, output
            paths = [p for p in ("content/notes", "content/fixes", "content/logs", "content/tech")
                     if (REPO_PATH / p).is_dir()]
            if paths:
                ok, detail = repository_git(REPO_PATH, ["add", "--"] + paths)
                if not ok:
                    return False, detail
                ok, staged = repository_git(REPO_PATH, ["diff", "--cached", "--name-only", "--"] + paths)
                if not ok:
                    return False, staged
                if staged:
                    ok, detail = repository_git(REPO_PATH, ["commit", "-m", f"Auto save logs: {datetime.now():%Y-%m-%d %H:%M}", "--only", "--"] + paths)
                    if not ok:
                        return False, detail
            ok, detail = repository_git(REPO_PATH, ["push"])
            return ok, output + "\n" + detail

        def finished(success):
            if close_after:
                # Edits made while synchronizing must also be saved before exit.
                if self.has_note_draft():
                    self.on_close()
                    return
                if success or messagebox.askyesno("自动同步未完成", "本地文件已保留。网络、权限或仓库冲突导致未能同步，请查看同步日志。\n仍要退出吗？"):
                    self.stop_preview()
                    self.root.destroy()
            elif not self.has_note_draft():
                self.choose_note_date(self.date_entry.get().strip())
            else:
                self.refresh_month_entries()

        self.start_git_task("自动保存并同步", sync, on_complete=finished)

    def setup_sync_tab(self):
        ttk.Label(self.sync_tab, text="多端同步", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(self.sync_tab, text="换设备后先拉取。拉取时临时保存并恢复本地改动；有冲突会保留备份，分支分叉不会自动合并。", style="Hint.TLabel", wraplength=1000).pack(anchor=tk.W, pady=(2, 8))
        toolbar = ttk.Frame(self.sync_tab, style="App.TFrame")
        toolbar.pack(fill=tk.X)
        self.pull_btn = ttk.Button(toolbar, text="拉取远端更新", command=self.manual_pull)
        self.pull_btn.pack(side=tk.LEFT)
        self.check_remote_btn = ttk.Button(toolbar, text="检查远端", style="Soft.TButton", command=self.auto_pull)
        self.check_remote_btn.pack(side=tk.LEFT, padx=6)
        self.refresh_git_btn = ttk.Button(toolbar, text="刷新更改列表", style="Soft.TButton", command=self.refresh_dirty_files)
        self.refresh_git_btn.pack(side=tk.RIGHT)
        self.git_log = tk.Text(self.sync_tab, height=20, state=tk.DISABLED, bg=self.CARD, fg=self.INK, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.BORDER, padx=9, pady=8)
        self.git_log.pack(fill=tk.BOTH, expand=True, pady=(7, 10))
        row = ttk.Frame(self.sync_tab, style="App.TFrame")
        row.pack()
        self.push_notes_btn = ttk.Button(row, text="推送 Notes / Fixes", command=self.manual_push_notes)
        self.push_logs_btn = ttk.Button(row, text="推送文档目录", command=self.manual_push_docs)
        self.push_all_btn = ttk.Button(row, text="推送全部变更", command=self.manual_push_all)
        for button in (self.push_notes_btn, self.push_logs_btn, self.push_all_btn):
            button.pack(side=tk.LEFT, padx=4)
        self.refresh_dirty_files()

    def log_git_msg(self, message):
        def append():
            self.git_log.config(state=tk.NORMAL)
            self.git_log.insert(tk.END, message + "\n")
            self.git_log.see(tk.END)
            self.git_log.config(state=tk.DISABLED)
        self.ui(append)

    def run_git_cmd(self, args):
        return repository_git(REPO_PATH, args[1:] if args[0] == "git" else args)

    def refresh_dirty_files(self):
        success, output = self.run_git_cmd(["git", "status", "--short"])
        self.log_git_msg("\n--- 未提交更改 ---\n" + (output or "无未提交更改。" if success else "无法读取 git 状态。\n" + output))

    def set_push_buttons_state(self, state):
        for button in (self.push_notes_btn, self.push_logs_btn, self.push_all_btn,
                       self.pull_btn, self.check_remote_btn, self.refresh_git_btn, self.save_note_btn, self.save_doc_btn):
            button.config(state=state)

    def start_git_task(self, label, worker, on_success=None, on_complete=None):
        if self.git_busy:
            self.status_var.set("仓库操作进行中，请稍候。")
            return
        self.git_busy = True
        self.git_cancel.clear()
        self.set_push_buttons_state(tk.DISABLED)
        self.status_var.set(label + "…")
        def run():
            GIT_TASK_CONTEXT.cancel = self.git_cancel
            try:
                success, output = worker()
            except Exception as exc:
                success, output = False, str(exc)
            finally:
                del GIT_TASK_CONTEXT.cancel
            def finish():
                self.git_busy = False
                self.set_push_buttons_state(tk.NORMAL)
                self.log_git_msg("\n--- " + label + " ---\n" + output)
                self.status_var.set(label + ("完成。" if success else "未完成，请查看同步日志。"))
                if success and on_success:
                    on_success()
                self.refresh_dirty_files()
                if self.close_pending:
                    self.root.after_idle(self.on_close)
                elif on_complete:
                    on_complete(success)
            self.ui(finish)
        # Finish restoring local work even if the application is closed unexpectedly.
        threading.Thread(target=run, daemon=False).start()

    def auto_pull(self):
        def check():
            success, output = self.run_git_cmd(["git", "fetch"])
            if not success:
                return False, output
            success, counts = self.run_git_cmd(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"])
            if not success:
                return False, counts
            ahead, behind = map(int, counts.split())
            return True, f"本地领先 {ahead} 个提交，远端领先 {behind} 个提交。" + ("请点“拉取远端更新”。" if behind else "")
        self.start_git_task("检查远端", check)

    def manual_pull(self):
        def reload_local():
            date_str = self.date_entry.get().strip()
            if not self.has_note_draft() and self.month_from_date_field():
                self.choose_note_date(date_str)
            else:
                self.refresh_month_entries()
                self.status_var.set("拉取完成；编辑框中未保存的草稿已保留。")
        self.start_git_task("拉取远端更新", lambda: pull_repository(REPO_PATH), reload_local)

    def push_paths(self, paths, label, commit_prefix):
        def push():
            ok, conflicts = self.run_git_cmd(["git", "diff", "--name-only", "--diff-filter=U"])
            if not ok or conflicts:
                return False, "请先处理仓库冲突。\n" + conflicts
            ok, output = self.run_git_cmd(["git", "add"] + paths)
            if not ok:
                return False, output
            selected_paths = [] if paths == ["-A"] else paths
            ok, staged = self.run_git_cmd(["git", "diff", "--cached", "--name-only", "--"] + selected_paths)
            if not ok:
                return False, staged
            if staged:
                command = ["git", "commit", "-m", f"{commit_prefix}: {datetime.now():%Y-%m-%d %H:%M}"]
                if selected_paths:
                    command += ["--only", "--"] + selected_paths
                ok, output = self.run_git_cmd(command)
                if not ok:
                    return False, output
            # Still push when a previous attempt committed successfully but failed at push.
            return self.run_git_cmd(["git", "push"])
        self.start_git_task("推送 " + label, push)

    def manual_push_notes(self):
        self.push_paths(["content/notes/", "content/fixes/"], "Notes / Fixes", "Update notes and fixes")

    def manual_push_docs(self):
        self.push_paths(["content/logs/", "content/tech/"], "文档目录", "Update docs")

    def manual_push_all(self):
        self.push_paths(["-A"], "全部变更", "Update site")


if __name__ == "__main__":
    root = tk.Tk()
    LoggerApp(root)
    root.mainloop()
