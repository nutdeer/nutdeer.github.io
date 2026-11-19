"""Build the real journal template and execute its renderer without a browser."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('zola') and shutil.which('node'), 'Requires Zola and Node')
class JournalStatusTests(unittest.TestCase):
    def test_legacy_and_ordered_dual_status_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            (site / 'templates').mkdir()
            (site / 'content/notes').mkdir(parents=True)
            (site / 'config.toml').write_text('base_url = "https://example.invalid"\n')
            (site / 'templates/page.html').write_text('{% block main %}{% endblock main %}')
            shutil.copyfile(ROOT / 'templates/journal.html', site / 'templates/journal.html')
            (site / 'content/notes/_index.md').write_text('+++\ntemplate = "journal.html"\n+++\n')
            (site / 'content/notes/2026-07.md').write_text('''+++
title = "Test month"
date = 2026-07-01
[[extra.logs]]
day = 1
status = "学习"
status2 = "工作"
content = "First **study**."
content2 = "Then **work**."
[[extra.logs]]
day = 2
status = "工作"
status2 = "学习"
content = "Reverse order."
[[extra.logs]]
day = 3
status = "休息"
content = "Legacy single status."
+++
''')
            subprocess.run(['zola', 'build'], cwd=site, capture_output=True, text=True, check=True)
            output = site / 'public/notes/index.html'
            html = output.read_text()
            self.assertNotIn('aria-label="然后"', html)
            self.assertNotIn('calendar-legend', html)
            self.assertIn('<p>Then <strong>work</strong>.</p>', html)
            self.assertIn('linear-gradient(to bottom right, var(--day-first) 50%, var(--day-second) 50%)', html)
            rendered = subprocess.run(['node', '-e', r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const grid = {innerHTML: ''};
const sandbox = {window: {}, document: {getElementById: () => grid}};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
vm.runInContext('renderCalendar()', sandbox);
assert.equal((grid.innerHTML.match(/interactive dual-status/g) || []).length, 2);
const cells = grid.innerHTML.match(/<div class="day-cell interactive dual-status[^]*?<\/div>\s*<\/div>/g);
assert(cells[0].includes('--day-first: var(--cal-study); --day-second: var(--cal-work)'));
assert(cells[1].includes('--day-first: var(--cal-work); --day-second: var(--cal-study)'));
assert(cells[0].indexOf('>学习</span>') < cells[0].indexOf('>工作</span>'));
assert(grid.innerHTML.includes('<strong>study</strong>'), cells[0]);
assert(grid.innerHTML.includes('<strong>work</strong>'));
assert(grid.innerHTML.indexOf('<strong>study</strong>') < grid.innerHTML.indexOf('<strong>work</strong>'));
assert(grid.innerHTML.includes('background-color: var(--cal-rest)'));
''', str(output)], capture_output=True, text=True)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)


if __name__ == '__main__':
    unittest.main()
