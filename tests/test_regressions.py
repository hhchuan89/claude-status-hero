"""Audit regressions; all fixtures and injected HOME stay inside the repo."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import shlex
import time
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

    def test_concurrent_metrics_cannot_rollback_hook(self):
        hl.update_session('race', {'state': 'working', 'state_ts': 1})
        ready, release = self.root / 'ready', self.root / 'release'
        env = dict(os.environ, STATUS_HERO_DIR=hl.STATE_DIR)
        code = """import hero_line as h, pathlib, time
original = h.atomic_write
def paused(path, data):
    pathlib.Path(%r).touch()
    deadline = time.monotonic() + 10
    while not pathlib.Path(%r).exists() and time.monotonic() < deadline:
        time.sleep(.005)
    original(path, data)
h.atomic_write = paused
h.update_session('race', {'cost': 12})
""" % (str(ready), str(release))
        metrics = subprocess.Popen([sys.executable, '-c', code], env=env, cwd=str(ROOT))
        self.addCleanup(lambda: metrics.poll() is None and metrics.kill())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(ready.exists())
        hook = subprocess.Popen([sys.executable, '-c', "import hero_line as h; h.update_session('race', {'state':'needs_you','state_ts':2})"], env=env, cwd=str(ROOT))
        self.addCleanup(lambda: hook.poll() is None and hook.kill())
        time.sleep(.15)
        release.touch()
        self.assertEqual(metrics.wait(timeout=5), 0)
        self.assertEqual(hook.wait(timeout=5), 0)
        saved = hl.load_json(hl.sess_path('race'))
        self.assertEqual(saved['state'], 'needs_you')
        self.assertEqual(saved['cost'], 12)

    def test_only_session_start_can_resume_tombstone(self):
        with patch.object(hl.sys, 'stdin', io.StringIO('{"session_id":"end"}')):
            hl.handle_hook('SessionEnd')
        self.assertTrue(hl.load_json(hl.sess_path('end'))['ended'])
        hl.update_session('end', {'state': 'working', 'state_ts': time.time()})
        self.assertTrue(hl.load_json(hl.sess_path('end'))['ended'])
        with patch.object(hl.sys, 'stdin', io.StringIO('{"session_id":"end"}')):
            hl.handle_hook('SessionStart')
        self.assertFalse(hl.load_json(hl.sess_path('end')).get('ended'))

    def test_autostart_uses_private_unique_backups_and_safe_tempfiles(self):
        self.settings.write_text('{"custom": 1}')
        victim = self.root / 'victim'
        victim.write_text('untouched')
        old_tmp = Path(str(self.settings) + '.tmp.%d' % os.getpid())
        old_backup = Path(str(self.settings) + '.autostart-backup')
        old_tmp.symlink_to(victim)
        old_backup.symlink_to(victim)
        self.assertEqual(hb.autostart('on'), 0)
        self.assertEqual(victim.read_text(), 'untouched')
        self.assertEqual(hb.autostart('off'), 0)
        backups = list(self.root.glob('settings.json.status-hero-backup-*'))
        self.assertEqual(len(backups), 2)
        if os.name != 'nt':
            self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(b.stat().st_mode & 0o777 == 0o600 for b in backups))

    def test_settings_writers_share_a_lock(self):
        self.settings.write_text('{}')
        calls = []
        @contextlib.contextmanager
        def lock(path):
            calls.append(path)
            yield
        with patch.object(hl, 'settings_lock', lock):
            hl.install(['--settings', str(self.settings)])
            hl.uninstall(['--settings', str(self.settings)])
            hb.autostart('on')
        self.assertEqual(calls, [str(self.settings)] * 3)

    def test_windows_hook_quoting_roundtrips_shell_metacharacters(self):
        arg = 'C:\\a$(printf injected) & `printf injected`\\hero_line.py'
        for module in (hl, hb):
            with patch.object(module.os, 'name', 'nt'):
                cmd = module.shell_cmd('python3', arg, '--hook', 'Stop')
            self.assertEqual(shlex.split(cmd)[1], arg.replace('\\', '/'))
            self.assertIn("'", cmd)

    def test_windows_launch_fallback_does_not_use_cmd_shell(self):
        with patch.object(hb.shutil, 'which', return_value=None), patch.object(hb.subprocess, 'Popen') as launch:
            hb._win_launch_board('python.exe', 'C:/a&b/hero_board.py', ['--office'])
        args, kwargs = launch.call_args
        self.assertEqual(args[0], ['python.exe', 'C:/a&b/hero_board.py', '--office'])
        self.assertEqual(kwargs['creationflags'], 0x10)

    def test_reinstall_preserves_style_and_style_edits_have_backup(self):
        hl.save_config({'style': 'list'})
        hl.install(['--settings', str(self.settings)])
        self.assertEqual(json.loads(self.settings.read_text())['statusLine']['refreshInterval'], 2)
        before = len(list(self.root.glob('settings.json.status-hero-backup-*')))
        self.assertEqual(hl.main(['--style', 'gauge', '--settings', str(self.settings)]), 0)
        self.assertEqual(len(list(self.root.glob('settings.json.status-hero-backup-*'))), before + 1)

    def test_external_text_cannot_leak_c1_bidi_or_surrogates(self):
        for module in (hl, hb):
            self.assertEqual(module.sanitize('a\x9bb\u202ec\ud800d'), 'abcd')
            with patch.object(module, 'ASCII', True):
                self.assertTrue(module.sanitize('项目🦊').isascii())

    def test_wrong_cwd_types_and_large_numbers_do_not_break_render(self):
        with patch.object(hl, 'read_fable', return_value=None):
            for cwd in (None, 42, [], {'x': 1}):
                rows = hl.render({'workspace': {'current_dir': cwd}}, 80).split('\n')
                self.assertEqual([hl.disp_width(row) for row in rows], [78] * 3)
        for module in (hl, hb):
            self.assertIsNone(module.num(10 ** 1000))
            self.assertEqual(module.disp_width(module.bar(1e308, 12)), 12)

    def test_render_error_fallback_keeps_width_and_height(self):
        for style, height in (('gauge', 3), ('list', 7), ('fleet', 10)):
            out = io.StringIO()
            with patch.object(hl, 'render', side_effect=ValueError), patch.object(hl, 'load_config', return_value={'style': style}), patch.object(hl, 'term_width', return_value=60), patch.object(hl.sys, 'stdin', io.StringIO('{}')), patch.object(hl.sys, 'stdout', out):
                self.assertEqual(hl.main([]), 0)
            self.assertEqual([hl.disp_width(row) for row in out.getvalue().splitlines()], [58] * height)

    def test_terminal_narrower_than_forty_does_not_wrap_rows(self):
        with patch.object(hl, 'read_fable', return_value=None):
            for width in (1, 2, 3, 10, 20, 39):
                rows = hl.render({}, width).split('\n')
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(hl.disp_width(row) <= width for row in rows))

    def test_fleet_scan_does_not_starve_live_sessions_or_trust_hostile_types(self):
        Path(hl.SESS_DIR).mkdir(parents=True)
        for i in range(70):
            Path(hl.SESS_DIR, '%03d.json' % i).write_text(json.dumps({'sid': str(i), 'ts': 1, 'ended': True}))
        Path(hl.SESS_DIR, 'z-live.json').write_text(json.dumps({'sid': 'live', 'ts': time.time(), 'hero': [], 'state': {}}))
        sessions = hl.all_sessions()
        self.assertEqual([s['sid'] for s in sessions], ['live'])
        hl.fleet_summary(sessions * 2, 'live')
        hl.pick_hero('new')
        self.assertEqual(len(list(Path(hl.SESS_DIR).glob('*.json'))), 1)

    def test_cleanup_cannot_delete_a_refreshed_session(self):
        hl.update_session('race', {'state': 'idle'})
        path = hl.sess_path('race')
        old = hl.load_json(path)
        hl.update_session('race', {'cost': 5})
        hl.prune_session(path, old)
        self.assertEqual(hl.load_json(path)['cost'], 5)

    def test_registry_omission_cannot_delete_a_live_process(self):
        hl.update_session('active', {'state': 'idle', 'ppid': os.getpid()})
        path = hl.sess_path('active')
        saved = hl.load_json(path)
        saved['ts'] = time.time() - 100
        hl.atomic_write(path, saved)
        with patch.object(hb, 'live_sids', return_value={'another-window'}):
            self.assertEqual([s['sid'] for s in hb.fleet()], ['active'])
        self.assertTrue(Path(path).exists())

    def test_pending_launch_prevents_a_startup_window_storm(self):
        with patch.object(hb, '_board_already_open', return_value=False), patch.object(hb.sys, 'platform', 'darwin'), patch.object(hb, '_mac_launch_board', return_value=0) as launch:
            self.assertEqual(hb.ensure_board(['--ensure', '--office']), 0)
            self.assertEqual(hb.ensure_board(['--ensure', '--office']), 0)
        self.assertEqual(launch.call_count, 1)

    def test_pidfile_does_not_follow_a_symlink(self):
        Path(hb.STATE_DIR).mkdir(parents=True)
        victim = self.root / 'victim'
        victim.write_text('untouched')
        Path(hb._pidfile()).symlink_to(victim)
        hb._write_pidfile()
        self.assertEqual(victim.read_text(), 'untouched')
        self.assertEqual(Path(hb._pidfile()).read_text(), str(os.getpid()))


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
