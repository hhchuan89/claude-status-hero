"""Audit regressions; all fixtures and injected HOME stay inside the repo."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    from support import isolate
    isolate(str(ROOT))
import hero_line as hl
import hero_board as hb


class AuditTests(unittest.TestCase):
    def enterContext(self, cm):
        value = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return value

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root / 'settings.json'
        self.enterContext(patch.object(hl, 'STATE_DIR', str(self.root / 'state')))
        self.enterContext(patch.object(hl, 'SESS_DIR', str(self.root / 'state/sessions')))
        self.enterContext(patch.object(hb, 'STATE_DIR', hl.STATE_DIR))
        self.enterContext(patch.object(hb, 'SESS_DIR', hl.SESS_DIR))
        self.enterContext(patch.object(hb, '_settings_path', lambda: str(self.settings)))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_invalid_settings_untouched(self):
        for raw in ('{broken', 'null', '[]', '42', ''):
            for writer in (hl.install, hl.uninstall):
                self.settings.write_text(raw)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    writer(['--settings', str(self.settings)])
                self.assertEqual(self.settings.read_text(), raw)
            self.assertEqual(hb.autostart('on'), 1)
            self.assertEqual(self.settings.read_text(), raw)

    def test_mixed_hooks_preserve_foreign_entries(self):
        own = {'type': 'command', 'command': hl.shell_cmd(sys.executable, str(ROOT / 'hero_line.py'), '--hook', 'Stop')}
        foreign = {'type': 'command', 'command': 'echo hero_line.py --hook Stop'}
        group = {'matcher': '*', 'extra': 9, 'hooks': [own, foreign]}
        self.settings.write_text(json.dumps({'custom': 1, 'hooks': {'Stop': [group]}}))
        hl.uninstall(['--settings', str(self.settings)])
        result = json.loads(self.settings.read_text())
        self.assertEqual(result['hooks']['Stop'], [dict(group, hooks=[foreign])])
        self.assertEqual(result['custom'], 1)
        board_own = dict(own, command=hb.shell_cmd(sys.executable, str(ROOT / 'hero_board.py'), '--ensure', '--pixel'))
        group['hooks'] = [board_own, foreign]
        self.settings.write_text(json.dumps({'hooks': {'SessionStart': [group]}}))
        self.assertEqual(hb.autostart('off'), 0)
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['SessionStart'], [dict(group, hooks=[foreign])])

    def test_ownership_is_not_a_substring(self):
        self.assertFalse(hl.owned_command('echo hero_line.py'))
        self.assertFalse(hl.owned_command('python3 /tmp/other-hero_line.py'))
        self.assertTrue(hl.owned_command(hl.shell_cmd(sys.executable, "/tmp/a 'quote'/hero_line.py")))


def run_checks(check):
    class Results(unittest.TestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            check(test._testMethodName, True)
        def addFailure(self, test, err):
            super().addFailure(test, err)
            check(test._testMethodName, False, self._exc_info_to_string(err, test))
        def addError(self, test, err):
            super().addError(test, err)
            check(test._testMethodName, False, self._exc_info_to_string(err, test))
    unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests).run(Results())


if __name__ == '__main__':
    unittest.main()
