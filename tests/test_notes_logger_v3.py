"""Local-only regression checks for v3; never access the user's remote or notes."""
import importlib.util
import subprocess
import socket
import time
import urllib.request
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

MODULE_PATH = Path(__file__).resolve().parents[1] / 'notes_logger_v3.py'
spec = importlib.util.spec_from_file_location('notes_v3', MODULE_PATH)
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def configure(repo):
    git(repo, 'config', 'user.name', 'Local Test')
    git(repo, 'config', 'user.email', 'local-test@example.invalid')


class PullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote, self.a, self.b = (base / p for p in ('remote.git', 'a', 'b'))
        git(base, 'init', '--bare', str(self.remote))
        git(base, 'clone', str(self.remote), str(self.a))
        configure(self.a)
        (self.a / 'shared.txt').write_text('original\nsecond line\n')
        (self.a / 'staged.txt').write_text('base\n')
        git(self.a, 'add', '.')
        git(self.a, 'commit', '-m', 'seed')
        git(self.a, 'push', '-u', 'origin', 'HEAD')
        git(base, 'clone', str(self.remote), str(self.b))
        configure(self.b)

    def publish(self, name='remote.txt', text='remote content\n'):
        (self.b / name).write_text(text)
        git(self.b, 'add', name)
        git(self.b, 'commit', '-m', 'other device update')
        git(self.b, 'push')

    def test_clean_fast_forward(self):
        self.publish()
        ok, details = app_module.pull_repository(self.a)
        self.assertTrue(ok, details)
        self.assertEqual((self.a / 'remote.txt').read_text(), 'remote content\n')
        self.assertEqual(git(self.a, 'status', '--porcelain'), '')

    def test_dirty_work_and_index_restore_preserves_older_stash(self):
        (self.a / 'shared.txt').write_text('older stash\n')
        git(self.a, 'stash', 'push', '-m', 'older backup')
        older = git(self.a, 'rev-parse', 'refs/stash')
        (self.a / 'shared.txt').write_text('local unstaged\n')
        (self.a / 'staged.txt').write_text('local staged\n')
        git(self.a, 'add', 'staged.txt')
        (self.a / 'draft.txt').write_text('untracked draft\n')
        self.publish()
        ok, details = app_module.pull_repository(self.a)
        self.assertTrue(ok, details)
        self.assertEqual((self.a / 'shared.txt').read_text(), 'local unstaged\n')
        self.assertEqual((self.a / 'draft.txt').read_text(), 'untracked draft\n')
        self.assertEqual(git(self.a, 'diff', '--cached', '--name-only'), 'staged.txt')
        self.assertEqual(git(self.a, 'stash', 'list', '--format=%H'), older)

    def test_conflict_preserves_backup_and_blocks_another_pull(self):
        (self.a / 'shared.txt').write_text('local diary\n')
        self.publish('shared.txt', 'other device diary\n')
        ok, details = app_module.pull_repository(self.a)
        self.assertFalse(ok)
        self.assertIn('备份保留', details)
        self.assertEqual(git(self.a, 'show', 'stash@{0}:shared.txt'), 'local diary')
        self.assertTrue(git(self.a, 'diff', '--name-only', '--diff-filter=U'))
        ok, details = app_module.pull_repository(self.a)
        self.assertFalse(ok)
        self.assertIn('未解决的冲突', details)

    def test_divergence_restores_dirty_work_without_merging(self):
        (self.a / 'local-commit.txt').write_text('local commit\n')
        git(self.a, 'add', '.')
        git(self.a, 'commit', '-m', 'local commit')
        local_head = git(self.a, 'rev-parse', 'HEAD')
        (self.a / 'shared.txt').write_text('draft during divergence\n')
        self.publish()
        ok, details = app_module.pull_repository(self.a)
        self.assertFalse(ok)
        self.assertEqual(git(self.a, 'rev-parse', 'HEAD'), local_head)
        self.assertEqual((self.a / 'shared.txt').read_text(), 'draft during divergence\n')
        self.assertEqual(git(self.a, 'stash', 'list'), '')

    def test_untracked_collision_keeps_original_in_stash(self):
        (self.a / 'collision.txt').write_text('local untracked\n')
        self.publish('collision.txt', 'remote tracked\n')
        ok, details = app_module.pull_repository(self.a)
        self.assertFalse(ok)
        self.assertIn('备份保留', details)
        self.assertEqual(git(self.a, 'show', 'stash@{0}^3:collision.txt'), 'local untracked')
        self.assertEqual((self.a / 'collision.txt').read_text(), 'remote tracked\n')


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest('No graphical display: ' + str(exc))
        self.root.withdraw()
        self.addCleanup(self.destroy_root)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, 'init')
        configure(self.repo)
        notes = self.repo / 'content/notes'
        notes.mkdir(parents=True)
        self.month = notes / '2026-11.md'
        self.month.write_text('+++\ntitle = "Test"\n[[extra.logs]]\nday = 2\nstatus = "学习"\ncontent = "existing entry"\n+++\n')
        self.patches = [patch.object(app_module, 'REPO_PATH', self.repo),
                        patch.object(app_module.LoggerApp, 'auto_pull', lambda _s: None),
                        patch.object(app_module.messagebox, 'showinfo'),
                        patch.object(app_module.messagebox, 'showwarning'),
                        patch.object(app_module.messagebox, 'askyesno', return_value=True)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = app_module.LoggerApp(self.root)
        self.root.update()

    def destroy_root(self):
        for timer in self.root.tk.call('after', 'info'):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_calendar_loads_existing_and_clears_empty_day(self):
        self.app.choose_note_date('2026-11-02')
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'existing entry')
        self.assertEqual(self.app.status_combo.get(), '学习')
        self.app.choose_note_date('2026-11-03')
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), '')
        self.assertEqual(len(self.app.calendar_grid.winfo_children()), 12)

    def test_save_preserves_editor_and_updates_cached_month(self):
        self.app.choose_note_date('2026-11-03')
        self.app.content_text.insert('1.0', 'new diary\nsecond line')
        self.app.save_note_log()
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'new diary\nsecond line')
        saved = next(e for e in self.app.get_month_entries(2026, 11) if e['day'] == 3)
        self.assertEqual(saved['content'], 'new diary\nsecond line')
        self.assertFalse(self.app.has_note_draft())
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_not_called()
        self.assertIn('existing entry', self.month.read_text())

    def test_date_selection_reuses_calendar_and_month_parse(self):
        cards = dict(self.app.month_cards)
        with patch.object(app_module, 'parse_month_entries', wraps=app_module.parse_month_entries) as parse:
            for day in range(3, 25):
                self.app.choose_note_date(f'2026-11-{day:02d}')
            parse.assert_not_called()
        self.assertEqual(self.app.month_cards, cards)
        self.assertEqual(len(self.app.calendar_grid.winfo_children()), 12)
        self.assertFalse(hasattr(self.app, 'reader_tab'))

    def test_preview_opens_only_once_when_server_becomes_ready(self):
        import io
        process = Mock()
        process.poll.return_value = None
        process.stdout = io.StringIO('Building...\nWeb server is available at http://127.0.0.1:1111\nWeb server is available at http://127.0.0.1:1111\n')
        self.app.zola_process = process
        self.app.preview_wants_open = True
        with patch.object(app_module.webbrowser, 'open') as browser:
            self.app.read_preview_output(process)
            browser.assert_called_once_with('http://127.0.0.1:1111/notes/', new=1)
            self.app.choose_note_date('2026-11-03')
            self.app.content_text.insert('1.0', 'save without another browser tab')
            self.app.save_note_log()
            self.assertEqual(browser.call_count, 1)
        self.app.zola_process = None

    def test_real_zola_preview_rebuilds_without_opening_another_page(self):
        zola = Path.home() / '.local/bin/zola'
        if not zola.exists():
            self.skipTest('Zola is not installed')
        templates = self.repo / 'templates'
        templates.mkdir()
        (templates / 'section.html').write_text('<html><body>{{ section.content | safe }}</body></html>')
        (self.repo / 'config.toml').write_text('base_url = "https://example.invalid"\n')
        section = self.repo / 'content/notes/_index.md'
        section.write_text('+++\ntemplate = "section.html"\n+++\npreview version one\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.app.preview_port_var.set(str(port))
        def wait_for(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.root.update()
                if predicate():
                    return
                time.sleep(.02)
            self.fail('Preview did not reach expected state: ' + self.app.preview_log.get('1.0', 'end'))
        with patch.object(app_module.shutil, 'which', return_value=str(zola)), patch.object(app_module.webbrowser, 'open') as browser:
            self.app.browse_local_site()
            process = self.app.zola_process
            try:
                wait_for(lambda: self.app.preview_ready)
                browser.assert_called_once_with(f'http://127.0.0.1:{port}/notes/', new=1)
                with urllib.request.urlopen(self.app.preview_url(), timeout=2) as response:
                    self.assertIn('preview version one', response.read().decode())
                section.write_text(section.read_text().replace('version one', 'version two'))
                def updated():
                    with urllib.request.urlopen(self.app.preview_url(), timeout=2) as response:
                        return 'preview version two' in response.read().decode()
                wait_for(updated)
                self.assertEqual(browser.call_count, 1)
                self.assertFalse((self.repo / 'public').exists())
            finally:
                self.app.stop_preview()
                process.wait(timeout=5)
                wait_for(lambda: self.app.zola_process is None)

    def test_unsaved_draft_can_cancel_date_navigation(self):
        self.app.choose_note_date('2026-11-03')
        self.app.content_text.insert('1.0', 'unsaved draft')
        with patch.object(app_module.messagebox, 'askyesno', return_value=False):
            self.app.choose_note_date('2026-11-02')
        self.assertEqual(self.app.date_entry.get(), '2026-11-03')
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'unsaved draft')

    def test_remote_update_does_not_silently_overwrite_edited_day(self):
        self.app.choose_note_date('2026-11-02')
        self.app.content_text.insert(tk.END, ' local draft')
        self.month.write_text(self.month.read_text().replace('existing entry', 'remote update'))
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_called_once()
        self.assertIn('remote update', self.month.read_text())
        self.assertIn('local draft', self.app.content_text.get('1.0', 'end-1c'))

    def test_pull_refresh_keeps_unsaved_draft(self):
        self.app.choose_note_date('2026-11-02')
        self.app.content_text.insert(tk.END, ' my draft')
        self.month.write_text(self.month.read_text().replace('existing entry', 'new remote version'))
        with patch.object(self.app, 'start_git_task', side_effect=lambda label, worker, after: after()):
            self.app.manual_pull()
        self.assertIn('my draft', self.app.content_text.get('1.0', 'end-1c'))
        self.assertEqual(self.app.get_month_entries(2026, 11)[0]['content'], 'new remote version')

    def test_selected_push_leaves_unrelated_staged_file_alone(self):
        fixes = self.repo / 'content/fixes'
        fixes.mkdir()
        (fixes / '_index.md').write_text('+++\ntitle = "Fixes"\n+++\n')
        (self.repo / 'unrelated.txt').write_text('base\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'seed')
        with tempfile.TemporaryDirectory() as remote_dir:
            git(remote_dir, 'init', '--bare')
            git(self.repo, 'remote', 'add', 'origin', remote_dir)
            git(self.repo, 'push', '-u', 'origin', 'HEAD')
            self.month.write_text(self.month.read_text().replace('existing entry', 'changed diary'))
            (self.repo / 'unrelated.txt').write_text('unrelated staged work\n')
            git(self.repo, 'add', 'unrelated.txt')
            results = []
            with patch.object(self.app, 'start_git_task', side_effect=lambda _label, worker: results.append(worker())):
                self.app.manual_push_notes()
            self.assertTrue(results[0][0], results[0][1])
            self.assertEqual(git(self.repo, 'diff', '--cached', '--name-only'), 'unrelated.txt')
            self.assertEqual(git(self.repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'), 'content/notes/2026-11.md')


if __name__ == '__main__':
    unittest.main()
