"""Local-only regression checks for v3; never access the user's remote or notes."""
import importlib.util
import os
import json
import subprocess
import socket
import time
import threading
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

    def run_auto_sync(self):
        app = Mock()
        results = []
        app.start_git_task.side_effect = lambda label, worker, **kwargs: results.append(worker())
        with patch.object(app_module, 'REPO_PATH', self.a):
            app_module.LoggerApp.auto_sync(app)
        return results[0]

    def test_auto_sync_pulls_remote_and_pushes_only_logs(self):
        notes = self.a / 'content/notes'
        notes.mkdir(parents=True)
        (notes / 'test.md').write_text('local diary\n')
        (self.a / 'staged.txt').write_text('unrelated staged work\n')
        git(self.a, 'add', 'staged.txt')
        self.publish()
        ok, details = self.run_auto_sync()
        self.assertTrue(ok, details)
        self.assertEqual((self.a / 'remote.txt').read_text(), 'remote content\n')
        self.assertEqual(git(self.remote, 'show', 'HEAD:content/notes/test.md'), 'local diary')
        self.assertEqual(git(self.a, 'diff', '--cached', '--name-only'), 'staged.txt')
        self.assertEqual(git(self.remote, 'show', 'HEAD:staged.txt'), 'base')
        self.assertFalse(app_module.repository_needs_sync(self.a))
        (notes / 'test.md').write_text('new diary after push\n')
        self.assertTrue(app_module.repository_needs_sync(self.a))

    def test_exit_retries_committed_but_unpushed_work(self):
        (self.a / 'local.txt').write_text('committed locally\n')
        git(self.a, 'add', 'local.txt')
        git(self.a, 'commit', '-m', 'local work')
        self.assertTrue(app_module.repository_needs_sync(self.a))

    def test_auto_sync_conflict_keeps_backup_without_pushing(self):
        (self.a / 'shared.txt').write_text('local diary\n')
        self.publish('shared.txt', 'remote diary\n')
        remote_head = git(self.remote, 'rev-parse', 'HEAD')
        ok, details = self.run_auto_sync()
        self.assertFalse(ok)
        self.assertIn('备份保留', details)
        self.assertEqual(git(self.a, 'show', 'stash@{0}:shared.txt'), 'local diary')
        self.assertEqual(git(self.remote, 'rev-parse', 'HEAD'), remote_head)


class CloseTests(unittest.TestCase):
    def test_force_exit_backs_up_editor_even_when_sync_is_busy(self):
        app = Mock(git_busy=True, zola_process=None)
        app.note_form_snapshot.return_value = ('2026-11-08', '工作', '学习', '未保存的草稿', '第二段草稿')
        for name in ('doc_mode_var', 'doc_file_var', 'doc_target_var', 'doc_title_var', 'doc_section_var', 'doc_with_assets_var', 'doc_date_var', 'doc_description_var', 'doc_tags_var'):
            getattr(app, name).get.return_value = 'draft'
        with tempfile.TemporaryDirectory() as directory, patch.object(app_module, 'REPO_PATH', Path(directory)), patch.object(app_module.messagebox, 'askyesno', return_value=True), patch.object(app_module.os, '_exit') as exit_process:
            app_module.LoggerApp.force_exit(app)
            backups = list((Path(directory) / '.notes_logger_drafts').glob('*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text())['note'][2:], ['学习', '未保存的草稿', '第二段草稿'])
            exit_process.assert_called_once_with(0)
            app.auto_sync.assert_not_called()

    def test_force_exit_stays_open_when_backup_fails(self):
        app = Mock()
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / '.notes_logger_drafts'
            blocker.write_text('not a directory')
            with patch.object(app_module, 'REPO_PATH', Path(directory)), patch.object(app_module.messagebox, 'askyesno', return_value=True), patch.object(app_module.messagebox, 'showerror') as error, patch.object(app_module.os, '_exit') as exit_process:
                app_module.LoggerApp.force_exit(app)
                error.assert_called_once()
                exit_process.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX process group test')
    def test_cancel_interrupts_hung_git_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'git'
            executable.write_text('#!/usr/bin/python3\nimport subprocess,time\nsubprocess.Popen(["sleep", "30"])\ntime.sleep(30)\n')
            executable.chmod(0o755)
            event = threading.Event()
            app_module.GIT_TASK_CONTEXT.cancel = event
            timer = threading.Timer(0.3, event.set)
            timer.start()
            started = time.monotonic()
            try:
                with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']}):
                    ok, detail = app_module.repository_git(directory, ['push'])
                self.assertFalse(ok)
                self.assertIn('已取消', detail)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                timer.cancel()
                del app_module.GIT_TASK_CONTEXT.cancel

    def test_close_saves_draft_before_sync(self):
        app = Mock(git_busy=False, skip_sync_on_close=False)
        app.has_note_draft.side_effect = [True, False]
        with patch.object(app_module, 'repository_needs_sync', return_value=True):
            app_module.LoggerApp.on_close(app)
        self.assertEqual(app.method_calls, [
            unittest.mock.call.has_note_draft(), unittest.mock.call.save_note_log(),
            unittest.mock.call.has_note_draft(), unittest.mock.call.auto_sync(close_after=True)])
        app.root.destroy.assert_not_called()

    def test_close_after_successful_push_does_not_sync_again(self):
        app = Mock(git_busy=False, skip_sync_on_close=False)
        app.has_note_draft.return_value = False
        with patch.object(app_module, 'repository_needs_sync', return_value=False):
            app_module.LoggerApp.on_close(app)
        app.auto_sync.assert_not_called()
        app.root.destroy.assert_called_once()

    def test_close_keeps_editor_open_when_save_fails(self):
        app = Mock(git_busy=False, skip_sync_on_close=False)
        app.has_note_draft.return_value = True
        app_module.LoggerApp.on_close(app)
        app.auto_sync.assert_not_called()
        app.root.destroy.assert_not_called()

    def test_close_waits_for_running_sync(self):
        app = Mock(git_busy=True)
        app_module.LoggerApp.on_close(app)
        self.assertTrue(app.close_pending)
        app.git_cancel.set.assert_called_once()
        app.save_note_log.assert_not_called()
        app.root.destroy.assert_not_called()

    def test_cancelled_sync_closes_without_starting_another_push(self):
        app = Mock(git_busy=False, skip_sync_on_close=True)
        app.has_note_draft.return_value = False
        app_module.LoggerApp.on_close(app)
        app.auto_sync.assert_not_called()
        app.root.destroy.assert_called_once()


class NoteStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.month = self.repo / 'content/notes/2026-11.md'
        self.month.parent.mkdir(parents=True)
        self.month.write_text('+++\ntitle = "Test"\ndate = 2026-11-01\n[[extra.logs]]\nday = 2\nstatus = "学习"\ncontent = "original"\n+++\n')
        self.app = app_module.LoggerApp.__new__(app_module.LoggerApp)
        self.app.git_busy = False
        self.app.loaded_note_date = '2026-11-02'
        self.app.note_disk_entry = ('学习', '', 'original', '')
        self.app.date_entry = Mock()
        self.app.date_entry.get.return_value = '2026-11-02'
        self.app.status_combo = Mock()
        self.app.status_combo.get.return_value = '学习'
        self.app.status2_combo = Mock()
        self.app.status2_combo.get.return_value = '工作'
        self.app.content_text = Mock()
        self.app.content_text.get.return_value = 'updated diary'
        self.app.content2_text = Mock()
        self.app.content2_text.get.return_value = ''
        self.app.status_var = Mock()
        self.app.refresh_month_entries = Mock()
        for patcher in (patch.object(app_module, 'REPO_PATH', self.repo),
                        patch.object(app_module.messagebox, 'showwarning')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self):
        return app_module.parse_month_entries(self.month, 2026, 11)[0]

    def test_legacy_entry_has_optional_empty_second_status(self):
        self.assertEqual(app_module.entry_signature(self.entry()), ('学习', '', 'original', ''))

    def test_empty_diary_status_can_be_changed_and_saved(self):
        self.month.write_text(self.month.read_text().replace('content = "original"', 'content = ""'))
        self.app.note_disk_entry = ('学习', '', '', '')
        self.app.status_combo.get.return_value = '其他'
        self.app.status2_combo.get.return_value = ''
        self.app.content_text.get.return_value = ''
        self.app.save_note_log()
        self.assertEqual(app_module.entry_signature(self.entry()), ('其他', '', '', ''))
        app_module.messagebox.showwarning.assert_not_called()

    def test_two_statuses_round_trip_in_order_and_can_be_reversed(self):
        self.app.save_note_log()
        self.assertEqual(app_module.entry_status_label(self.entry()), '学习 → 工作')
        self.assertFalse(self.app.has_note_draft())
        self.app.status_combo.get.return_value = '工作'
        self.app.status2_combo.get.return_value = '学习'
        self.assertTrue(self.app.has_note_draft())
        self.app.save_note_log()
        self.assertEqual(app_module.entry_status_label(self.entry()), '工作 → 学习')
        app_module.messagebox.showwarning.assert_not_called()

    def test_second_status_can_be_removed(self):
        self.app.save_note_log()
        self.app.status2_combo.get.return_value = ''
        self.app.save_note_log()
        self.assertEqual(self.entry()['status2'], '')
        self.assertNotIn('status2 =', self.month.read_text())

    def test_external_second_status_change_blocks_overwrite(self):
        self.month.write_text(self.month.read_text().replace('status = "学习"', 'status = "学习"\nstatus2 = "休息"'))
        before = self.month.read_text()
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_called_once()
        self.assertEqual(self.month.read_text(), before)

    def test_separate_contents_round_trip_and_dirty_tracking(self):
        self.app.content2_text.get.return_value = 'second diary\nnext line'
        self.app.save_note_log()
        self.assertEqual(self.entry()['content'], 'updated diary')
        self.assertEqual(self.entry()['content2'], 'second diary\nnext line')
        self.assertFalse(self.app.has_note_draft())
        self.app.content2_text.get.return_value = 'revised second diary'
        self.assertTrue(self.app.has_note_draft())
        self.app.save_note_log()
        self.assertEqual(self.entry()['content2'], 'revised second diary')
        app_module.messagebox.showwarning.assert_not_called()

    def test_content2_without_status_is_preserved_in_editor(self):
        self.app.status2_combo.get.return_value = ''
        self.app.content2_text.get.return_value = 'must not discard'
        before = self.month.read_text()
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_called_once()
        self.assertEqual(self.month.read_text(), before)
        self.assertEqual(self.app.content2_text.get(), 'must not discard')

    def test_external_second_content_change_blocks_overwrite(self):
        self.app.save_note_log()
        self.month.write_text(self.month.read_text().replace('status2 = "工作"', 'status2 = "工作"\ncontent2 = "external second content"'))
        before = self.month.read_text()
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_called_once()
        self.assertEqual(self.month.read_text(), before)

    def test_cached_entries_remain_stable_while_sync_changes_disk(self):
        self.app.month_cache = {}
        original = self.app.get_month_entries(2026, 11)
        signature = self.app.month_file_signature(2026, 11)
        self.app.git_busy = True
        self.month.write_text(self.month.read_text().replace('original', 'new remote content'))
        with patch.object(app_module, 'parse_month_entries') as parse:
            self.assertEqual(self.app.get_month_entries(2026, 11), original)
            self.assertEqual(self.app.month_file_signature(2026, 11), signature)
            parse.assert_not_called()
        self.app.git_busy = False
        self.assertEqual(self.app.get_month_entries(2026, 11)[0]['content'], 'new remote content')

    def test_fix_keeps_separate_save_path(self):
        self.app.status_combo.get.return_value = 'Fix'
        self.app.save_fix_from_note = Mock()
        self.app.save_note_log()
        self.app.save_fix_from_note.assert_called_once_with('2026-11-02', 'updated diary')
        self.assertEqual(self.entry()['content'], 'original')


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
                        patch.object(app_module.LoggerApp, 'auto_sync', lambda _self: None),
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

    def test_dual_status_editor_calendar_and_reload(self):
        self.app.choose_note_date('2026-11-02')
        self.app.status2_combo.set('工作')
        self.app.content2_text.insert('1.0', 'second segment')
        self.app.save_note_log()
        self.app.choose_note_date('2026-11-03')
        self.assertEqual(self.app.status2_combo.get(), '')
        self.app.choose_note_date('2026-11-02')
        self.assertEqual(self.app.status2_combo.get(), '工作')
        self.assertEqual(self.app.content2_text.get('1.0', 'end-1c'), 'second segment')
        card, outline = self.app.calendar_cells['2026-11-02']
        polygons = [item for item in card.find_withtag('day:2026-11-02') if card.type(item) == 'polygon']
        self.assertEqual(len(polygons), 1)
        self.assertEqual(card.itemcget(polygons[0], 'fill'), '#426895')
        x1, y1, x2, y2 = card.coords(outline)
        self.assertEqual(card.coords(polygons[0]), [x2, y1, x2, y2, x1, y2])
        self.assertIn('学习 → 工作', self.app.calendar_summaries['2026-11-02'])

    def test_both_editors_remain_visible_at_supported_window_sizes(self):
        self.root.deiconify()
        for width, height in ((1160, 850), (1000, 740)):
            self.root.geometry(f'{width}x{height}')
            self.root.update()
            for widget in (self.app.content_text, self.app.content2_text):
                self.assertGreaterEqual(widget.winfo_height(), 65)
                self.assertGreaterEqual(widget.winfo_width(), 200)
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     self.root.winfo_rooty() + self.root.winfo_height())

    def test_typed_date_loads_both_fields_without_false_draft_prompt(self):
        self.app.choose_note_date('2026-11-03')
        self.app.date_entry.delete(0, tk.END)
        self.app.date_entry.insert(0, '2026-11-02')
        app_module.messagebox.askyesno.reset_mock()
        self.app.load_date_field()
        self.assertEqual(self.app.loaded_note_date, '2026-11-02')
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'existing entry')
        self.assertEqual(self.app.content2_text.get('1.0', 'end-1c'), '')
        app_module.messagebox.askyesno.assert_not_called()

    def test_typed_date_can_cancel_navigation_and_preserve_second_draft(self):
        self.app.choose_note_date('2026-11-02')
        self.app.content2_text.insert('1.0', 'second draft')
        self.app.date_entry.delete(0, tk.END)
        self.app.date_entry.insert(0, '2026-11-03')
        with patch.object(app_module.messagebox, 'askyesno', return_value=False):
            self.app.load_date_field()
        self.assertEqual(self.app.date_entry.get(), '2026-11-02')
        self.assertEqual(self.app.content2_text.get('1.0', 'end-1c'), 'second draft')

    def test_month_list_click_during_slow_sync_uses_cache_and_preserves_draft(self):
        self.app.choose_note_date('2026-11-03')
        release, changed = threading.Event(), threading.Event()
        def worker():
            self.month.write_text(self.month.read_text().replace('existing entry', 'new remote version'))
            changed.set()
            release.wait(5)
            return True, 'done'
        self.addCleanup(release.set)
        with patch.object(self.app, 'refresh_dirty_files'):
            self.app.start_git_task('Slow local test', worker)
            self.assertTrue(changed.wait(2))
            item = next(i for i, entry in self.app.month_entries.items() if entry['day'] == 2)
            self.app.month_tree.selection_set(item)
            before = time.monotonic()
            self.root.update()
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertTrue(self.app.git_busy)
            self.assertEqual(self.app.loaded_note_date, '2026-11-02')
            self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'existing entry')
            self.app.status2_combo.set('工作')
            self.app.content2_text.insert('1.0', 'draft during sync')
            self.app.save_note_log()
            self.assertIn('new remote version', self.month.read_text())
            release.set()
            deadline = time.monotonic() + 3
            while self.app.git_busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(self.app.git_busy)
        self.assertEqual(self.app.content2_text.get('1.0', 'end-1c'), 'draft during sync')
        self.assertEqual(self.app.get_month_entries(2026, 11)[0]['content'], 'new remote version')
        self.app.save_note_log()
        app_module.messagebox.showwarning.assert_called_once()

    def test_sync_refreshes_unedited_date_after_cache_navigation(self):
        self.app.choose_note_date('2026-11-03')
        self.app.git_busy = True
        self.month.write_text(self.month.read_text().replace('existing entry', 'updated while syncing'))
        self.app.choose_note_date('2026-11-02')
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'existing entry')
        self.app.git_busy = False
        self.app.refresh_notes_after_sync()
        self.assertEqual(self.app.content_text.get('1.0', 'end-1c'), 'updated while syncing')

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
        self.app.refresh_notes_after_sync()
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
