import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_PATH = os.environ.get("NUTDEER_REPO_PATH", str(SCRIPT_DIR / "nutdeer.github.io"))


class LoggerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Zola 日志管理小工具")
        self.root.geometry("560x560")

        self.status_var = tk.StringVar(value="正在初始化...")
        self.status_label = ttk.Label(root, textvariable=self.status_var, foreground="blue", padding=5)
        self.status_label.pack(fill=tk.X)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(expand=True, fill=tk.BOTH, padx=10, pady=5)

        self.notes_tab = ttk.Frame(self.notebook)
        self.docs_tab = ttk.Frame(self.notebook)
        self.sync_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.notes_tab, text="[+] 新增/修改日志")
        self.notebook.add(self.docs_tab, text="[+] 文档日志")
        self.notebook.add(self.sync_tab, text="[>] 同步仓库")

        self.setup_notes_tab()
        self.setup_docs_tab()
        self.setup_sync_tab()

        threading.Thread(target=self.auto_pull, daemon=True).start()

    def setup_notes_tab(self):
        ttk.Label(self.notes_tab, text="日期 (YYYY-MM-DD):").pack(anchor=tk.W, padx=10, pady=(10, 2))
        self.date_entry = ttk.Entry(self.notes_tab)
        self.date_entry.insert(0, datetime.today().strftime("%Y-%m-%d"))
        self.date_entry.pack(fill=tk.X, padx=10)

        ttk.Label(self.notes_tab, text="状态:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        self.status_combo = ttk.Combobox(self.notes_tab, values=["工作", "学习", "休息", "Fix"], state="readonly")
        self.status_combo.current(0)
        self.status_combo.pack(fill=tk.X, padx=10)

        ttk.Label(self.notes_tab, text="内容:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        self.content_text = tk.Text(self.notes_tab, height=10)
        self.content_text.pack(fill=tk.BOTH, expand=True, padx=10)

        ttk.Label(
            self.notes_tab,
            text=(
                "选 Fix 时：第一行写标题，后续行写解决内容；"
                "同名标题会更新，只填同名标题会删除。"
            ),
            foreground="gray",
            wraplength=510,
        ).pack(anchor=tk.W, padx=10, pady=(6, 0))

        self.save_btn = ttk.Button(
            self.notes_tab,
            text="保存写入本地 Markdown (存在则覆盖)",
            command=self.save_note_log,
        )
        self.save_btn.pack(pady=15)

    def setup_docs_tab(self):
        ttk.Label(self.docs_tab, text="Markdown 文件:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        file_row = ttk.Frame(self.docs_tab)
        file_row.pack(fill=tk.X, padx=10)

        self.doc_file_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self.doc_file_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(file_row, text="选择", command=self.select_doc_file).pack(side=tk.LEFT, padx=(8, 0))

        self.doc_with_assets_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.docs_tab,
            text="带图片资源：复制同名 .assets 文件夹，并保存为 index.md",
            variable=self.doc_with_assets_var,
            command=self.update_doc_target_hint,
        ).pack(anchor=tk.W, padx=10, pady=(12, 4))

        ttk.Label(self.docs_tab, text="目标栏目:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        self.doc_section_var = tk.StringVar(value="logs")
        section_combo = ttk.Combobox(
            self.docs_tab,
            textvariable=self.doc_section_var,
            values=["logs", "tech"],
            state="readonly",
        )
        section_combo.pack(fill=tk.X, padx=10)
        section_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_doc_target_hint())

        ttk.Label(self.docs_tab, text="目标名称:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        self.doc_target_var = tk.StringVar()
        target_entry = ttk.Entry(self.docs_tab, textvariable=self.doc_target_var)
        target_entry.pack(fill=tk.X, padx=10)
        target_entry.bind("<KeyRelease>", lambda _event: self.update_doc_target_hint())

        self.doc_target_hint = ttk.Label(self.docs_tab, text="", foreground="gray")
        self.doc_target_hint.pack(anchor=tk.W, padx=10, pady=(4, 0))

        self.import_doc_btn = ttk.Button(
            self.docs_tab,
            text="保存到所选栏目",
            command=self.import_doc_log,
        )
        self.import_doc_btn.pack(pady=18)

        ttk.Label(
            self.docs_tab,
            text=(
                "普通文档保存为 content/<栏目>/<目标名称>.md；"
                "带图文档保存为 content/<栏目>/<目标名称>/index.md，"
                "并复制源文档同级的 <源文件名>.assets 文件夹。"
            ),
            foreground="gray",
            wraplength=510,
        ).pack(anchor=tk.W, padx=10, pady=(8, 0))

        self.update_doc_target_hint()

    def setup_sync_tab(self):
        ttk.Button(
            self.sync_tab,
            text="刷新更改列表",
            command=self.refresh_dirty_files,
        ).pack(anchor=tk.E, padx=10, pady=(10, 0))

        self.git_log = tk.Text(self.sync_tab, height=21, state=tk.DISABLED, bg="#f4f4f4")
        self.git_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        btn_row = ttk.Frame(self.sync_tab)
        btn_row.pack(pady=12)

        self.push_notes_btn = ttk.Button(
            btn_row,
            text="推送 Notes/Fixes 到 GitHub",
            command=self.manual_push_notes,
        )
        self.push_notes_btn.pack(side=tk.LEFT, padx=6)

        self.push_logs_btn = ttk.Button(
            btn_row,
            text="仅推送文档目录到 GitHub",
            command=self.manual_push_docs,
        )
        self.push_logs_btn.pack(side=tk.LEFT, padx=6)

        self.push_all_btn = ttk.Button(
            btn_row,
            text="推送全部变更到 GitHub",
            command=self.manual_push_all,
        )
        self.push_all_btn.pack(side=tk.LEFT, padx=6)

        self.refresh_dirty_files()

    def log_git_msg(self, msg):
        if not hasattr(self, "git_log"):
            return
        self.git_log.config(state=tk.NORMAL)
        self.git_log.insert(tk.END, msg + "\n")
        self.git_log.see(tk.END)
        self.git_log.config(state=tk.DISABLED)

    def run_git_cmd(self, args):
        try:
            result = subprocess.run(
                args,
                cwd=REPO_PATH,
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
            )
            return True, result.stdout.strip()
        except subprocess.CalledProcessError as e:
            return False, (e.stderr or e.stdout).strip()

    def refresh_dirty_files(self):
        success, out = self.run_git_cmd(["git", "status", "--short"])
        if not success:
            self.log_git_msg("\n--- 未提交更改 ---")
            self.log_git_msg("无法读取 git 状态。\n" + out)
            return

        self.log_git_msg("\n--- 未提交更改 ---")
        if out:
            self.log_git_msg(out)
        else:
            self.log_git_msg("无未提交更改。")

    def auto_pull(self):
        if not os.path.exists(REPO_PATH):
            self.status_var.set("[Error] 错误: 找不到仓库，请修改代码中的 REPO_PATH")
            return

        self.status_var.set("[*] 正在检查远程仓库更新...")
        self.log_git_msg("执行: git fetch...")
        success, out = self.run_git_cmd(["git", "fetch"])
        if not success:
            self.status_var.set("[Error] Fetch 失败，请检查网络")
            self.log_git_msg(out)
            return

        success, upstream = self.run_git_cmd(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if not success:
            self.status_var.set("[OK] 已 fetch；当前分支没有 upstream")
            self.log_git_msg(upstream)
            return

        success, counts = self.run_git_cmd(["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
        if not success:
            self.status_var.set("[Error] 无法比较本地和远程版本")
            self.log_git_msg(counts)
            return

        ahead, behind = [int(x) for x in counts.split()]
        self.log_git_msg(f"当前分支: ahead {ahead}, behind {behind}")

        success, dirty_files = self.run_git_cmd(["git", "status", "--porcelain"])
        if not success:
            self.status_var.set("[Error] 无法检查本地工作区状态")
            self.log_git_msg(dirty_files)
            return

        if dirty_files:
            changed_count = len(dirty_files.splitlines())
            self.log_git_msg(f"工作区有 {changed_count} 个未提交变更。")
        self.refresh_dirty_files()

        if ahead == 0 and behind == 0:
            if dirty_files:
                self.status_var.set("[!] 本地和远程提交一致，但有未提交变更")
                self.log_git_msg("本地和远程提交一致，但工作区有未提交变更，需要提交后才能推送到远程。")
            else:
                self.status_var.set("[OK] 本地和远程仓库一致")
                self.log_git_msg("本地和远程一致，无需 pull 或 push。")
            return

        if ahead > 0 and behind == 0:
            self.status_var.set(f"[!] 本地领先远程 {ahead} 个提交，待 Push")
            self.log_git_msg(f"本地有 {ahead} 个提交尚未推送到远程。")
            return

        if ahead > 0 and behind > 0:
            self.status_var.set(f"[!] 本地领先 {ahead} 个、远程领先 {behind} 个，需手动处理")
            self.log_git_msg("本地和远程都有新提交，分支已分叉。")
            self.log_git_msg("请先手动处理合并或变基，工具不会自动 pull。")
            return

        self.status_var.set(f"[*] 发现远程新版本 {behind} 个提交，正在 Pull...")
        self.log_git_msg("执行: git pull --ff-only...")
        success, pull_out = self.run_git_cmd(["git", "pull", "--ff-only"])
        if success:
            self.status_var.set("[OK] 仓库已更新至最新")
            self.log_git_msg(pull_out)
        else:
            self.status_var.set("[Error] Pull 失败，可能有本地提交或冲突")
            self.log_git_msg(pull_out)
        self.refresh_dirty_files()

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
            yyyy = dt.strftime("%Y")
            mm = dt.strftime("%m")
            day_int = int(dt.strftime("%d"))
        except ValueError:
            messagebox.showerror("错误", "日期格式错误，请使用 YYYY-MM-DD")
            return

        content_escaped = content.replace("\n", "  ")
        notes_dir = os.path.join(REPO_PATH, "content", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        file_path = os.path.join(notes_dir, f"{yyyy}-{mm}.md")
        new_block_str = f'\nday = {day_int}\nstatus = "{status}"\ncontent = "{content_escaped}"\n\n'

        if not os.path.exists(file_path):
            full_content = f"""+++
title = "{yyyy}年{int(mm)}月日志"
date = {yyyy}-{mm}-01
# 【必读说明】
# date 是必需的，Zola 靠它来判断哪个月在前面，哪个月在后面。title 可留作备忘。
# status 状态可选词："工作"、"休息"、"学习"。直接复制下方的块即可新增一天。

[[extra.logs]]{new_block_str}+++
"""
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
            action_msg = "已创建新月份并写入。"
        else:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()

            parts = text.split("+++")
            if len(parts) < 3:
                messagebox.showerror("错误", "Markdown 文件格式异常，找不到成对的 +++ 。")
                return

            frontmatter = parts[1]
            blocks = frontmatter.split("[[extra.logs]]")
            header = blocks[0]
            log_blocks = blocks[1:]
            parsed_blocks = []
            replaced = False

            for block in log_blocks:
                day_match = re.search(r"day\s*=\s*(\d+)", block)
                if not day_match:
                    parsed_blocks.append((-1, block))
                    continue

                b_day = int(day_match.group(1))
                if b_day == day_int:
                    parsed_blocks.append((day_int, new_block_str))
                    replaced = True
                else:
                    parsed_blocks.append((b_day, block))

            if not replaced:
                parsed_blocks.append((day_int, new_block_str))
                action_msg = "已追加新日志。"
            else:
                action_msg = f"已成功覆盖 {day_int} 号的旧日志！"

            parsed_blocks.sort(key=lambda x: x[0])
            new_frontmatter = header
            for _, block_content in parsed_blocks:
                new_frontmatter += "[[extra.logs]]" + block_content
            if not new_frontmatter.endswith("\n"):
                new_frontmatter += "\n"

            parts[1] = new_frontmatter
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("+++".join(parts))

        self.content_text.delete("1.0", tk.END)
        self.content_text.edit_modified(False)
        self.status_var.set("[!] 已保存到本地，尚未 Push")
        self.refresh_dirty_files()
        messagebox.showinfo("成功", f"{action_msg}\n文件：{yyyy}-{mm}.md\n可去『同步仓库』页进行 Push。")

    def toml_escape(self, value):
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n")

    def toml_unescape(self, value):
        return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")

    def build_fix_block(self, date_str, title, content):
        return (
            "\n[[extra.fixes]]\n"
            f"date = {date_str}\n"
            f'title = "{self.toml_escape(title)}"\n'
            f'content = "{self.toml_escape(content)}"\n'
        )

    def save_fix_from_note(self, date_str, raw_content):
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("错误", "日期格式错误，请使用 YYYY-MM-DD")
            return

        lines = [line.rstrip() for line in raw_content.splitlines()]
        title = lines[0].strip()
        content = "\n".join(line for line in lines[1:]).strip()

        if not title:
            messagebox.showwarning("提示", "Fix 第一行标题不能为空！")
            return

        fixes_dir = os.path.join(REPO_PATH, "content", "fixes")
        os.makedirs(fixes_dir, exist_ok=True)
        file_path = os.path.join(fixes_dir, "_index.md")

        if not os.path.exists(file_path):
            base_content = """+++
title = "Fixes"
description = "Small troubleshooting notes and compact fixes."
template = "fixes_list.html"
+++
"""
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(base_content)

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        parts = text.split("+++")
        if len(parts) < 3:
            messagebox.showerror("错误", "Fixes 文件格式异常，找不到成对的 +++ 。")
            return

        frontmatter = parts[1]
        blocks = frontmatter.split("[[extra.fixes]]")
        header = blocks[0].rstrip()
        fix_blocks = blocks[1:]
        updated_blocks = []
        found = False
        delete_requested = content == ""

        for block in fix_blocks:
            title_match = re.search(r'title\s*=\s*"((?:\\.|[^"])*)"', block)
            if not title_match:
                updated_blocks.append(block)
                continue

            block_title = self.toml_unescape(title_match.group(1))
            if block_title == title:
                found = True
                if not delete_requested:
                    updated_blocks.append(self.build_fix_block(date_str, title, content).replace("[[extra.fixes]]", "", 1))
                continue

            updated_blocks.append(block)

        if delete_requested and not found:
            self.content_text.delete("1.0", tk.END)
            self.content_text.edit_modified(False)
            self.status_var.set("[OK] Fix 不存在，未写入")
            self.refresh_dirty_files()
            return

        if not delete_requested and not found:
            updated_blocks.append(self.build_fix_block(date_str, title, content).replace("[[extra.fixes]]", "", 1))

        new_frontmatter = header
        for block in updated_blocks:
            new_frontmatter += "\n[[extra.fixes]]" + block.rstrip() + "\n"
        if not new_frontmatter.endswith("\n"):
            new_frontmatter += "\n"

        parts[1] = new_frontmatter
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("+++".join(parts))

        self.content_text.delete("1.0", tk.END)
        self.content_text.edit_modified(False)
        self.status_var.set("[!] Fix 已保存到本地，尚未 Push")
        self.refresh_dirty_files()
        if delete_requested:
            messagebox.showinfo("成功", f"Fix 已删除：\n{title}\n可去『同步仓库』页进行 Push。")
        elif found:
            messagebox.showinfo("成功", f"Fix 已更新：\n{title}\n可去『同步仓库』页进行 Push。")
        else:
            messagebox.showinfo("成功", "Fix 已保存到 content/fixes/_index.md\n可去『同步仓库』页进行 Push。")

    def select_doc_file(self):
        path = filedialog.askopenfilename(
            title="选择 Markdown 文档",
            filetypes=[("Markdown files", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return

        self.doc_file_var.set(path)
        if not self.doc_target_var.get().strip():
            self.doc_target_var.set(os.path.splitext(os.path.basename(path))[0])
        self.update_doc_target_hint()

    def update_doc_target_hint(self):
        target = self.doc_target_var.get().strip() or "<目标名称>"
        section = self.doc_section_var.get()
        if self.doc_with_assets_var.get():
            hint = os.path.join("content", section, target, "index.md")
        else:
            hint = os.path.join("content", section, f"{target}.md")
        self.doc_target_hint.config(text=f"将保存到：{hint}")

    def import_doc_log(self):
        src_md = self.doc_file_var.get().strip()
        section = self.doc_section_var.get()
        target_name = self.doc_target_var.get().strip()

        if not src_md:
            messagebox.showwarning("提示", "请选择 Markdown 文件。")
            return
        if not os.path.isfile(src_md):
            messagebox.showerror("错误", "Markdown 文件不存在。")
            return
        if not src_md.lower().endswith(".md"):
            messagebox.showerror("错误", "请选择 .md 文件。")
            return
        if not target_name:
            messagebox.showwarning("提示", "目标名称不能为空。")
            return
        if not self.is_safe_target_name(target_name):
            messagebox.showerror("错误", "目标名称不能包含路径分隔符，也不能是 . 或 ..。")
            return

        section_dir = os.path.join(REPO_PATH, "content", section)
        os.makedirs(section_dir, exist_ok=True)

        try:
            if self.doc_with_assets_var.get():
                src_dir = os.path.dirname(src_md)
                src_stem = os.path.splitext(os.path.basename(src_md))[0]
                assets_dir = os.path.join(src_dir, f"{src_stem}.assets")
                if not os.path.isdir(assets_dir):
                    messagebox.showerror("错误", f"找不到图片资源文件夹：\n{assets_dir}")
                    return

                target_dir = os.path.join(section_dir, target_name)
                target_md = os.path.join(target_dir, "index.md")
                target_assets_dir = os.path.join(target_dir, os.path.basename(assets_dir))
                if os.path.exists(target_dir) and not messagebox.askyesno("确认覆盖", f"目标已存在，是否覆盖？\n{target_dir}"):
                    return

                os.makedirs(target_dir, exist_ok=True)
                shutil.copy2(src_md, target_md)
                if os.path.exists(target_assets_dir):
                    shutil.rmtree(target_assets_dir)
                shutil.copytree(assets_dir, target_assets_dir)
                saved_path = os.path.join("content", section, target_name, "index.md")
            else:
                target_path = os.path.join(section_dir, f"{target_name}.md")
                if os.path.exists(target_path) and not messagebox.askyesno("确认覆盖", f"目标已存在，是否覆盖？\n{target_path}"):
                    return

                shutil.copy2(src_md, target_path)
                saved_path = os.path.join("content", section, f"{target_name}.md")
        except OSError as e:
            messagebox.showerror("错误", f"保存失败：\n{e}")
            return

        self.refresh_dirty_files()
        messagebox.showinfo("成功", f"文档已保存：\n{saved_path}\n可去『同步仓库』页推送文档目录。")

    def is_safe_target_name(self, target_name):
        if target_name in {".", ".."}:
            return False
        return os.path.basename(target_name) == target_name and "\\" not in target_name

    def set_push_buttons_state(self, state):
        self.push_notes_btn.config(state=state)
        self.push_logs_btn.config(state=state)
        self.push_all_btn.config(state=state)

    def push_paths(self, paths, label, commit_prefix):
        def push_thread():
            self.set_push_buttons_state(tk.DISABLED)
            self.status_var.set(f"[*] 正在 Push {label} 到 GitHub...")
            self.log_git_msg(f"\n--- 开始推送 {label} ---")

            self.log_git_msg(f"执行: git add {' '.join(paths)}")
            success, out = self.run_git_cmd(["git", "add"] + paths)
            if not success:
                self.log_git_msg("[Error] git add 失败！\n" + out)
                self.status_var.set("[Error] git add 失败")
                self.refresh_dirty_files()
                self.set_push_buttons_state(tk.NORMAL)
                return

            commit_msg = f"{commit_prefix}: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            self.log_git_msg(f"执行: git commit -m '{commit_msg}'...")
            success, out = self.run_git_cmd(["git", "commit", "-m", commit_msg])
            self.log_git_msg(out)
            if not success:
                if "nothing to commit" in out or "无文件要提交" in out or "没有要提交" in out:
                    self.status_var.set(f"[OK] {label} 没有需要提交的变更")
                else:
                    self.status_var.set("[Error] Commit 失败，请检查日志")
                self.refresh_dirty_files()
                self.set_push_buttons_state(tk.NORMAL)
                return

            self.log_git_msg("执行: git push...")
            success, out = self.run_git_cmd(["git", "push"])
            if success:
                self.log_git_msg("[OK] Push 成功！\n" + out)
                self.status_var.set(f"[OK] {label} Push 成功！")
            else:
                self.log_git_msg("[Error] Push 失败！\n" + out)
                self.status_var.set("[Error] Push 失败，请检查终端日志")

            self.refresh_dirty_files()
            self.set_push_buttons_state(tk.NORMAL)

        threading.Thread(target=push_thread, daemon=True).start()

    def manual_push_notes(self):
        self.push_paths(["content/notes/", "content/fixes/"], "Notes/Fixes", "Update notes and fixes")

    def manual_push_docs(self):
        self.push_paths(["content/logs/", "content/tech/"], "Docs", "Update docs")

    def manual_push_all(self):
        self.push_paths(["-A"], "All changes", "Update site")


if __name__ == "__main__":
    root = tk.Tk()
    app = LoggerApp(root)
    root.mainloop()
