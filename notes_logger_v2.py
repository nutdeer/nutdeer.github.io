"""Nutdeer notes and document logger (v2).

This is deliberately a separate program from notes_logger.py.  It manages the
same Zola content directory but leaves the old tool untouched.
"""

import html
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
# v2 lives in the site repository, while the older program lives one level up.
REPO_PATH = Path(os.environ.get("NUTDEER_REPO_PATH", SCRIPT_DIR)).expanduser()

DAY_RE = re.compile(r"day\s*=\s*(\d+)")
TOML_STRING_RE_TEMPLATE = r'{key}\s*=\s*"((?:\\.|[^"])*)"'


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
        self.root.title("Nutdeer 日志管理 v2")
        self.root.geometry("820x760")
        self.root.minsize(720, 650)
        self.root.configure(bg=self.BG)
        self.setup_style()

        self.status_var = tk.StringVar(value="准备就绪；旧版工具未被修改。")
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel", padding=(14, 8)).pack(fill=tk.X)

        self.notebook = ttk.Notebook(root, style="App.TNotebook")
        self.notebook.pack(expand=True, fill=tk.BOTH, padx=14, pady=(8, 14))

        self.notes_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.docs_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.preview_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.sync_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=14)
        self.notebook.add(self.notes_tab, text="日常日志 Notes")
        self.notebook.add(self.docs_tab, text="文档日志 Logs / Tech")
        self.notebook.add(self.preview_tab, text="本地预览")
        self.notebook.add(self.sync_tab, text="同步仓库")

        self.setup_notes_tab()
        self.setup_docs_tab()
        self.setup_preview_tab()
        self.setup_sync_tab()
        self.refresh_month_entries()
        threading.Thread(target=self.auto_pull, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        self.notes_tab.columnconfigure(0, weight=1)
        self.notes_tab.columnconfigure(1, weight=1)
        self.notes_tab.rowconfigure(4, weight=1)

        ttk.Label(self.notes_tab, text="日常日志", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.notes_tab, text="保存同一天会直接覆盖；先在下方选旧条目即可继续编辑。", style="Hint.TLabel").grid(row=0, column=1, sticky="e")

        form = ttk.Frame(self.notes_tab, style="App.TFrame")
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

        ttk.Label(self.notes_tab, text="内容").grid(row=2, column=0, sticky="w", pady=(10, 3))
        self.content_text = tk.Text(self.notes_tab, height=9, wrap=tk.WORD, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.BORDER, padx=9, pady=8)
        self.content_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        ttk.Label(self.notes_tab, text="Fix：第一行是标题，余下是解决内容；只保留标题则删除同名 Fix。", style="Hint.TLabel").grid(row=4, column=0, columnspan=2, sticky="nw", pady=(5, 0))

        button_row = ttk.Frame(self.notes_tab, style="App.TFrame")
        button_row.grid(row=5, column=0, columnspan=2, pady=(8, 14))
        ttk.Button(button_row, text="新建空白", style="Soft.TButton", command=self.clear_note_form).pack(side=tk.LEFT, padx=4)
        ttk.Button(button_row, text="保存到本地 Markdown", command=self.save_note_log).pack(side=tk.LEFT, padx=4)

        list_header = ttk.Frame(self.notes_tab, style="App.TFrame")
        list_header.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(list_header, text="本月已有日志", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Button(list_header, text="刷新列表", style="Soft.TButton", command=self.refresh_month_entries).pack(side=tk.RIGHT)

        list_frame = ttk.Frame(self.notes_tab, style="App.TFrame")
        list_frame.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self.notes_tab.rowconfigure(7, weight=1)
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

    def clear_note_form(self):
        self.date_entry.delete(0, tk.END)
        self.date_entry.insert(0, datetime.today().strftime("%Y-%m-%d"))
        self.status_combo.set("工作")
        self.content_text.delete("1.0", tk.END)
        self.month_tree.selection_remove(self.month_tree.selection())
        self.refresh_month_entries()

    def month_from_date_field(self):
        try:
            return datetime.strptime(self.date_entry.get().strip(), "%Y-%m-%d")
        except ValueError:
            return None

    def refresh_month_entries(self):
        for item in self.month_tree.get_children():
            self.month_tree.delete(item)
        self.month_entries = {}
        date_value = self.month_from_date_field()
        if not date_value:
            return
        month_path = REPO_PATH / "content" / "notes" / f"{date_value:%Y-%m}.md"
        for entry in parse_month_entries(month_path, date_value.year, date_value.month):
            summary = " ".join(entry["content"].split()) or "（无内容）"
            if len(summary) > 75:
                summary = summary[:74] + "…"
            item = self.month_tree.insert("", tk.END, values=(entry["date"], entry["status"], summary))
            self.month_entries[item] = entry

    def load_selected_month_entry(self, _event):
        selected = self.month_tree.selection()
        if not selected:
            return
        entry = self.month_entries.get(selected[0])
        if not entry:
            return
        self.date_entry.delete(0, tk.END)
        self.date_entry.insert(0, entry["date"])
        self.status_combo.set(entry["status"] or "工作")
        self.content_text.delete("1.0", tk.END)
        self.content_text.insert("1.0", entry["content"])
        self.status_var.set(f"已载入 {entry['date']}，修改后直接保存即可覆盖。")

    def toml_escape(self, value):
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n")

    def save_note_log(self):
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

        self.content_text.delete("1.0", tk.END)
        self.status_var.set("已保存到本地，尚未 Push。")
        self.refresh_month_entries()
        self.refresh_dirty_files()
        messagebox.showinfo("成功", f"{action_msg}\n文件：{file_path.name}\n可去“同步仓库”页 Push。")

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
        self.content_text.delete("1.0", tk.END)
        self.status_var.set("Fix 已保存到本地，尚未 Push。")
        self.refresh_dirty_files()
        messagebox.showinfo("成功", "Fix 已更新。" if found else "Fix 已保存到 content/fixes/_index.md。")

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
        ttk.Button(form, text="保存到所选栏目", command=self.import_doc_log).grid(row=10, column=0, columnspan=2, pady=(8, 4))
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
        self.refresh_dirty_files()
        messagebox.showinfo("成功", f"文档已保存：\n{saved_path}\n{detail}\n可去“同步仓库”页 Push。")

    @staticmethod
    def is_safe_target_name(target_name):
        return target_name not in {".", ".."} and os.path.basename(target_name) == target_name and "\\" not in target_name

    def setup_preview_tab(self):
        self.zola_process = None
        ttk.Label(self.preview_tab, text="本地预览", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(self.preview_tab, text="启动 zola serve，保存内容后浏览器会自动刷新，无需 Push。", style="Hint.TLabel").pack(anchor=tk.W, pady=(2, 8))

        control_row = ttk.Frame(self.preview_tab, style="App.TFrame")
        control_row.pack(fill=tk.X)
        ttk.Label(control_row, text="端口").pack(side=tk.LEFT, padx=(0, 6))
        self.preview_port_var = tk.StringVar(value="1111")
        ttk.Entry(control_row, textvariable=self.preview_port_var, width=8).pack(side=tk.LEFT, padx=(0, 12))
        self.start_preview_btn = ttk.Button(control_row, text="启动本地网页", command=self.start_preview)
        self.stop_preview_btn = ttk.Button(control_row, text="停止", style="Soft.TButton", command=self.stop_preview, state=tk.DISABLED)
        self.open_browser_btn = ttk.Button(control_row, text="打开浏览器", style="Soft.TButton", command=self.open_preview_browser, state=tk.DISABLED)
        self.start_preview_btn.pack(side=tk.LEFT, padx=4)
        self.stop_preview_btn.pack(side=tk.LEFT, padx=4)
        self.open_browser_btn.pack(side=tk.LEFT, padx=4)

        self.preview_log = tk.Text(self.preview_tab, height=20, state=tk.DISABLED, bg=self.CARD, fg=self.INK, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.BORDER, padx=9, pady=8)
        self.preview_log.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    def log_preview_msg(self, message):
        def append():
            self.preview_log.config(state=tk.NORMAL)
            self.preview_log.insert(tk.END, message + "\n")
            self.preview_log.see(tk.END)
            self.preview_log.config(state=tk.DISABLED)
        self.root.after(0, append)

    def preview_url(self):
        return f"http://127.0.0.1:{self.preview_port_var.get().strip()}/"

    def start_preview(self):
        if self.zola_process and self.zola_process.poll() is None:
            messagebox.showinfo("提示", "本地预览已在运行。")
            return
        port = self.preview_port_var.get().strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            messagebox.showerror("错误", "端口必须是 1-65535 的数字。")
            return
        if shutil.which("zola") is None:
            messagebox.showerror("错误", "找不到 zola 命令，请先安装 Zola。")
            return
        try:
            self.zola_process = subprocess.Popen(
                ["zola", "serve", "--interface", "127.0.0.1", "--port", port],
                cwd=REPO_PATH, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            messagebox.showerror("错误", f"启动失败：\n{exc}")
            return
        self.start_preview_btn.config(state=tk.DISABLED)
        self.stop_preview_btn.config(state=tk.NORMAL)
        self.open_browser_btn.config(state=tk.NORMAL)
        self.status_var.set(f"本地预览运行中：{self.preview_url()}")
        threading.Thread(target=self.read_preview_output, daemon=True).start()
        threading.Thread(target=self.watch_preview_process, daemon=True).start()
        self.root.after(1500, self.open_preview_browser)

    def read_preview_output(self):
        process = self.zola_process
        for line in process.stdout:
            self.log_preview_msg(line.rstrip())

    def watch_preview_process(self):
        process = self.zola_process
        return_code = process.wait()
        def on_exit():
            if self.zola_process is process:
                self.zola_process = None
                self.start_preview_btn.config(state=tk.NORMAL)
                self.stop_preview_btn.config(state=tk.DISABLED)
                self.open_browser_btn.config(state=tk.DISABLED)
                if return_code:
                    self.status_var.set(f"zola serve 异常退出（代码 {return_code}），请查看日志。")
                else:
                    self.status_var.set("本地预览已停止。")
        self.root.after(0, on_exit)

    def stop_preview(self):
        process = self.zola_process
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def open_preview_browser(self):
        if not self.zola_process or self.zola_process.poll() is not None:
            return
        webbrowser.open(self.preview_url())

    def on_close(self):
        self.stop_preview()
        self.root.destroy()

    def setup_sync_tab(self):
        ttk.Label(self.sync_tab, text="同步仓库", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(self.sync_tab, text="工具只会自动检查远程；遇到分叉不会自行合并。", style="Hint.TLabel").pack(anchor=tk.W, pady=(2, 8))
        ttk.Button(self.sync_tab, text="刷新更改列表", style="Soft.TButton", command=self.refresh_dirty_files).pack(anchor=tk.E)
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
        if not hasattr(self, "git_log"):
            return
        self.git_log.config(state=tk.NORMAL)
        self.git_log.insert(tk.END, message + "\n")
        self.git_log.see(tk.END)
        self.git_log.config(state=tk.DISABLED)

    def run_git_cmd(self, args):
        try:
            result = subprocess.run(args, cwd=REPO_PATH, capture_output=True, text=True, check=True, encoding="utf-8")
            return True, result.stdout.strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            return False, (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)).strip()

    def refresh_dirty_files(self):
        success, output = self.run_git_cmd(["git", "status", "--short"])
        self.log_git_msg("\n--- 未提交更改 ---")
        self.log_git_msg(output if success and output else ("无未提交更改。" if success else "无法读取 git 状态。\n" + output))

    def auto_pull(self):
        if not REPO_PATH.is_dir():
            self.status_var.set("错误：找不到仓库，请检查 NUTDEER_REPO_PATH。")
            return
        self.status_var.set("正在检查远程仓库更新…")
        success, output = self.run_git_cmd(["git", "fetch"])
        if not success:
            self.status_var.set("Fetch 失败，请检查网络。")
            self.log_git_msg(output)
            return
        success, upstream = self.run_git_cmd(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if not success:
            self.status_var.set("已 fetch；当前分支没有 upstream。")
            return
        success, counts = self.run_git_cmd(["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
        if not success:
            self.status_var.set("无法比较本地和远程版本。")
            return
        ahead, behind = [int(value) for value in counts.split()]
        if ahead or behind:
            self.status_var.set(f"本地领先 {ahead}、远程领先 {behind}；请在同步页确认后处理。")
        else:
            self.status_var.set("本地和远程仓库一致。")
        self.refresh_dirty_files()

    def set_push_buttons_state(self, state):
        for button in (self.push_notes_btn, self.push_logs_btn, self.push_all_btn):
            button.config(state=state)

    def push_paths(self, paths, label, commit_prefix):
        def push_thread():
            self.set_push_buttons_state(tk.DISABLED)
            self.status_var.set(f"正在 Push {label} 到 GitHub…")
            success, output = self.run_git_cmd(["git", "add"] + paths)
            if success:
                commit_msg = f"{commit_prefix}: {datetime.now():%Y-%m-%d %H:%M}"
                success, output = self.run_git_cmd(["git", "commit", "-m", commit_msg])
            if success:
                success, output = self.run_git_cmd(["git", "push"])
            self.log_git_msg(output)
            self.status_var.set(f"{label} Push 成功。" if success else f"{label} Push 失败，请检查日志。")
            self.refresh_dirty_files()
            self.set_push_buttons_state(tk.NORMAL)
        threading.Thread(target=push_thread, daemon=True).start()

    def manual_push_notes(self):
        self.push_paths(["content/notes/", "content/fixes/"], "Notes / Fixes", "Update notes and fixes")

    def manual_push_docs(self):
        self.push_paths(["content/logs/", "content/tech/"], "Docs", "Update docs")

    def manual_push_all(self):
        self.push_paths(["-A"], "全部变更", "Update site")


if __name__ == "__main__":
    root = tk.Tk()
    LoggerApp(root)
    root.mainloop()
