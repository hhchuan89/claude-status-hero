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
        with patch.object(hl, '__file__', str(self.root / "a 'quote'" / 'hero_line.py')):
            self.assertTrue(hl.owned_command(hl.shell_cmd(sys.executable, hl.__file__)))
        live = self.root / 'another app' / 'hero_line.py'
        live.parent.mkdir()
        live.touch()
        self.assertFalse(hl.owned_command(hl.shell_cmd(sys.executable, str(live))))

    def test_dead_checkout_hooks_are_swept_but_live_foreign_hooks_survive(self):
        dead = self.root / 'old checkout' / 'hero_line.py'
        live = self.root / 'other tool' / 'hero_line.py'
        live.parent.mkdir()
        live.touch()
        foreign = {'type': 'command', 'command': hl.shell_cmd(sys.executable, str(live), '--hook', 'Stop')}
        mention = {'type': 'command', 'command': hl.shell_cmd('echo', str(dead), '--hook', 'Stop')}
        for writer in (hl.install, hl.uninstall):
            hooks = {}
            for event, matcher, arg in hl.HOOK_EVENTS:
                group = {'hooks': [{'type': 'command', 'command': hl.shell_cmd(sys.executable, str(dead), '--hook', arg)}]}
                if matcher:
                    group['matcher'] = matcher
                hooks.setdefault(event, []).append(group)
            hooks['Stop'].append({'hooks': [foreign, mention], 'extra': 9})
            self.settings.write_text(json.dumps({'statusLine': {'type': 'command', 'command': hl.shell_cmd(sys.executable, str(dead))}, 'hooks': hooks}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                writer(['--settings', str(self.settings)])
            data = json.loads(self.settings.read_text())
            self.assertIn('stale entries swept: 10', out.getvalue())
            self.assertIn({'hooks': [foreign, mention], 'extra': 9}, data['hooks']['Stop'])
            commands = [h['command'] for groups in data['hooks'].values() for g in groups for h in g['hooks']]
            self.assertEqual(sum(hl.owned_command(c) for c in commands), 9 if writer == hl.install else 0)
            self.assertFalse(any(hl.owned_command(c, stale_only=True) for c in commands))
        # Unknown filesystem access must not classify a possibly live tool as stale.
        with patch.object(hl.os, 'stat', side_effect=PermissionError):
            self.assertFalse(hl.owned_command(hl.shell_cmd(sys.executable, str(dead))))

    def test_replaced_statusline_is_private_and_restored_after_reinstall(self):
        foreign = {'type': 'command', 'command': 'bash "$HOME/.claude/statusline-command.sh" --token synthetic-private-marker',
                   'padding': 3, 'custom': {'verbatim': ['x', 2, None]}}
        self.settings.write_text(json.dumps({'statusLine': foreign}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hl.install(['--settings', str(self.settings)])
            hl.install(['--settings', str(self.settings)])
            self.assertEqual(hl.load_config()['replaced_statusline'], foreign)
            if os.name != 'nt':
                self.assertEqual((Path(hl.STATE_DIR) / 'config.json').stat().st_mode & 0o777, 0o600)
            hl.uninstall(['--settings', str(self.settings)])
        self.assertEqual(json.loads(self.settings.read_text())['statusLine'], foreign)
        self.assertIn('restored previous statusline', out.getvalue())
        self.assertNotIn('synthetic-private-marker', out.getvalue())
        # A foreign statusline selected after installation is left alone.
        replacement = {'type': 'command', 'command': 'another-tool'}
        self.settings.write_text(json.dumps({'statusLine': replacement}))
        hl.uninstall(['--settings', str(self.settings)])
        self.assertEqual(json.loads(self.settings.read_text())['statusLine'], replacement)

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
        hook = subprocess.Popen([sys.executable, '-c', "import hero_line as h; h.update_session('race', {'state':'needs_you','state_ts':2}, hook=True)"], env=env, cwd=str(ROOT))
        self.addCleanup(lambda: hook.poll() is None and hook.kill())
        time.sleep(1.3)  # longer than the metrics timeout, within the hook budget
        release.touch()
        self.assertEqual(metrics.wait(timeout=5), 0)
        self.assertEqual(hook.wait(timeout=5), 0)
        saved = hl.load_json(hl.sess_path('race'))
        self.assertEqual(saved['state'], 'needs_you')
        self.assertEqual(saved['cost'], 12)

    def test_hook_lock_timeout_preserves_state_and_tombstone(self):
        hl._ensure_dirs()
        cases = [('SessionStart', 'working'), ('NeedsYouPermission', 'needs_you'),
                 ('NeedsYouIdle', 'needs_you'), ('Stop', 'idle'),
                 ('UserPromptSubmit', 'working'), ('PostToolUse', 'working'),
                 ('PreCompact', 'compacting'), ('PostCompact', 'working'), ('SessionEnd', None)]
        for event, state in cases:
            with self.subTest(event=event), patch.object(hl, 'session_lock', side_effect=TimeoutError) as lock, patch.object(hl.sys, 'stdin', io.StringIO('{"session_id":"contended"}')):
                hl.handle_hook(event)
                lock.assert_called_once_with(timeout=3.0)
            saved = hl.load_json(hl.sess_path('contended'))
            if state is None:
                self.assertTrue(saved['ended'])
            else:
                self.assertEqual(saved['state'], state)
        before = hl.load_json(hl.sess_path('contended'))
        with patch.object(hl, 'session_lock', side_effect=TimeoutError):
            hl.update_session('contended', {'cost': 99})
        self.assertEqual(hl.load_json(hl.sess_path('contended')), before)
        with patch.object(hl, 'session_lock', side_effect=TimeoutError), patch.object(hl.sys, 'stdin', io.StringIO('{"session_id":"contended"}')):
            hl.handle_hook('SessionStart')
        self.assertFalse(hl.load_json(hl.sess_path('contended')).get('ended'))

    def test_session_end_survives_a_real_held_lock_within_hook_budget(self):
        env = dict(os.environ, STATUS_HERO_DIR=hl.STATE_DIR)
        with hl.session_lock():
            result = subprocess.run([sys.executable, str(ROOT / 'hero_line.py'), '--hook', 'SessionEnd'],
                                    input='{"session_id":"held-lock"}', text=True, capture_output=True,
                                    env=env, cwd=str(ROOT), timeout=4.5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(hl.load_json(hl.sess_path('held-lock'))['ended'])

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
        with patch.object(hb, 'live_sids', return_value={'another-window'}), patch.object(hl, 'pid_alive', return_value=True):
            self.assertEqual([s['sid'] for s in hb.fleet()], ['active'])
        self.assertTrue(Path(path).exists())

    def test_registry_omission_buries_a_session_whose_pid_is_unknown(self):
        # Windows: pid_alive is always None, so the runtime registry is the only
        # death signal. An X-button close must not leave a permanent ghost.
        hl._ensure_dirs()
        for sid, age in (('closed', 100), ('newborn', 10), ('listed', 100)):
            hl.atomic_write(hl.sess_path(sid), {'sid': sid, 'ts': time.time() - age, 'ppid': 4242, 'state': 'idle'})
        with patch.object(hb, 'live_sids', return_value={'listed'}), patch.object(hl, 'pid_alive', return_value=None):
            self.assertEqual({s['sid'] for s in hb.fleet()}, {'newborn', 'listed'})
        self.assertFalse(Path(hl.sess_path('closed')).exists())

    def test_solo_style_renders_three_lines_without_session_file_io(self):
        # solo exists so many concurrent windows don't each lock, write and
        # re-read the shared sessions dir on every refresh.
        hl.save_config({'style': 'solo'})
        with patch.object(hl, 'all_sessions', side_effect=AssertionError('fleet read')), \
                patch.object(hl, 'update_session', side_effect=AssertionError('session write')):
            rows = hl.render({'session_id': 'solo-window', 'model': {'display_name': 'Opus'}}, 80).split('\n')
        self.assertEqual(len(rows), 3)
        self.assertEqual([hl.disp_width(row) for row in rows], [hl.render_width(80)] * 3)
        self.assertFalse(Path(hl.sess_path('solo-window')).exists())
        self.assertEqual(hl.main(['--style', 'solo', '--settings', str(self.settings)]), 0)

    def test_redaction_is_linear_on_a_large_pasted_blob(self):
        # A minified JSON paste with one bare-host URL made the userinfo
        # pattern backtrack quadratically (158 KB took ~8 s in the prompt hook).
        blob = json.dumps({'source': 'https://example.com', **{'k%d' % i: i for i in range(12000)}}, separators=(',', ':'))
        start = time.perf_counter()
        hl.redact(blob)
        self.assertLess(time.perf_counter() - start, 0.5)

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

    def test_board_frames_fit_small_terminals(self):
        for cols, rows in ((20, 5), (1, 1), (40, 10)):
            out = io.StringIO()
            with patch.object(hb.shutil, 'get_terminal_size', return_value=os.terminal_size((cols, rows))), patch.object(hb.sys, 'stdout', out):
                self.assertEqual(hb.main(['--office', '--demo', '--once']), 0)
            lines = out.getvalue().splitlines()
            self.assertLessEqual(len(lines), rows)
            self.assertTrue(all(hb.disp_width(line) <= cols for line in lines))

    def test_track_does_not_leave_half_a_coin_under_the_hero(self):
        for width in range(8, 101):
            for pct in range(101):
                track = hl.track(pct, width, 'fox')
                self.assertEqual(hl.disp_width(track), width, (width, pct))

    def test_svg_export_preserves_trailing_zero_and_m_text(self):
        from tools import ansi2svg
        out = io.StringIO()
        with patch.object(ansi2svg.sys, 'stdin', io.StringIO('m\n0\n')), patch.object(ansi2svg.sys, 'stdout', out):
            ansi2svg.main()
        self.assertEqual(out.getvalue().count('<text '), 2)

    def test_unwritable_state_does_not_break_the_statusline(self):
        with patch.object(hl, 'session_lock', side_effect=PermissionError), patch.object(hl, 'read_fable', return_value=None):
            rows = hl.render({'session_id': 'readonly'}, 80).split('\n')
        self.assertEqual([hl.disp_width(row) for row in rows], [78] * 3)
        Path(hl.SESS_DIR).mkdir(parents=True)
        Path(hl.sess_path('old')).write_text('{"sid":"old","ended":true,"ts":1}')
        with patch.object(hl, 'prune_session', side_effect=PermissionError):
            self.assertEqual(hl.all_sessions(), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX terminal probe')
    def test_da_reply_is_not_lost_in_text_io_buffer(self):
        import hero_pixel as hp
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        for reply, expected, heuristic in ((b'\x1b[?1;2c', False, 'iTerm2'), (b'\x1b[?1;2;4c', True, '')):
            read_fd, write_fd = os.pipe()
            reader = io.TextIOWrapper(os.fdopen(read_fd, 'rb', buffering=0))
            try:
                os.write(write_fd, reply)
                with patch.object(hp.sys, 'stdin', reader), patch.object(hp.sys, 'stdout', Terminal()), patch('termios.tcgetattr', return_value=[]), patch('termios.tcsetattr'), patch('tty.setcbreak'), patch.dict(os.environ, {'STATUS_HERO_SIXEL': '', 'LC_TERMINAL': heuristic, 'TERM_PROGRAM': ''}):
                    self.assertEqual(hp.detect_sixel(), expected)
            finally:
                reader.close()
                os.close(write_fd)

    def test_config_save_reports_write_failure(self):
        with patch.object(hl, '_open_excl', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                hl.save_config({'style': 'list'})

    def test_installer_does_not_print_previous_command_contents(self):
        self.settings.write_text(json.dumps({'statusLine': {'type': 'command', 'command': 'other-tool --token synthetic-private-marker'}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hl.install(['--settings', str(self.settings)])
        self.assertNotIn('synthetic-private-marker', out.getvalue())
        self.assertIn('replaced', out.getvalue())

    def test_future_git_mtime_is_not_a_permanent_cache_hit(self):
        hl._ensure_dirs()
        cwd = str(self.root)
        key = hl.hashlib.sha1(cwd.encode()).hexdigest()[:16]
        cache = Path(hl.STATE_DIR) / ('git-' + key)
        cache.write_text('stale')
        future = time.time() + 3600
        os.utime(cache, (future, future))
        with patch.object(hl.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0, 'fresh\n'), subprocess.CompletedProcess([], 0, '')]):
            self.assertEqual(hl.git_branch(cwd), 'fresh')

    def test_statusline_cleanup_keeps_a_day_old_live_process(self):
        hl.update_session('sleeping', {'state': 'idle', 'ppid': os.getpid()})
        path = hl.sess_path('sleeping')
        saved = hl.load_json(path)
        saved['ts'] = time.time() - 90000
        hl.atomic_write(path, saved)
        hl.all_sessions()
        self.assertTrue(Path(path).exists())

    def test_age_cleanup_requires_dead_pid_or_seven_days_without_pid(self):
        hl._ensure_dirs()
        cases = [('live', 8, 101), ('unknown', 8, 102), ('dead', 2, 103),
                 ('recent-dead', .5, 103), ('no-pid', 2, None), ('old-no-pid', 8, None)]
        for reader in (hl.all_sessions, hb.fleet):
            for sid, days, pid in cases:
                hl.atomic_write(hl.sess_path(sid), {'sid': sid, 'ts': time.time() - days * 86400, 'ppid': pid})
            with patch.object(hb, 'live_sids', return_value=set()), patch.object(hl, 'pid_alive', side_effect=lambda pid: {101: True, 102: None, 103: False}.get(pid)):
                self.assertEqual({s['sid'] for s in reader()}, {'live', 'unknown', 'recent-dead', 'no-pid'})
            self.assertTrue(Path(hl.sess_path('unknown')).exists())
            self.assertFalse(Path(hl.sess_path('dead')).exists())
            self.assertFalse(Path(hl.sess_path('old-no-pid')).exists())

    def test_snippet_credentials_are_redacted_before_storage_and_truncation(self):
        secrets = ['sk-synthetic123', 'sk-ant-api03-synthetic123', 'ghp_synthetic123',
                   'gho_synthetic123', 'github_pat_synthetic123',
                   'xoxa-synthetic-123', 'xoxb-synthetic-123', 'xoxp-synthetic-123', 'xoxr-synthetic-123',
                   'AKIA' + 'A' * 16, 'ASIA' + 'B' * 16, 'Bearer synthetic-token123',
                   'eyJzeW50aGV0aWM.eyJ0ZXN0.signature', 'https://user:synthetic-password@example.test',
                   'https://user:synthetic@password@example.test',
                   'KEY=synthetic-value', 'TOKEN="synthetic value"', "SECRET='synthetic value'",
                   'PASSWORD=synthetic-value', 'API_KEY=synthetic-value', 'ACCESS_TOKEN=synthetic-value']
        for secret in secrets:
            with self.subTest(secret=secret):
                expected = 'https://[REDACTED]example.test' if '://' in secret else '[REDACTED]'
                self.assertEqual(hl.redact(secret), expected)
                for event, fields in [('UserPromptSubmit', {'prompt': secret}),
                                      ('PostToolUse', {'tool_name': 'Bash', 'tool_input': {'command': secret}})]:
                    with patch.object(hl.sys, 'stdin', io.StringIO(json.dumps(dict(session_id='private', **fields)))):
                        hl.handle_hook(event)
                    saved = hl.load_json(hl.sess_path('private'))
                    self.assertEqual(saved['activity'], ('you: ' if event == 'UserPromptSubmit' else 'Bash: ') + expected)
                    self.assertNotIn(secret, Path(hl.sess_path('private')).read_text())
        # A secret crossing the snippet boundary is masked in full first.
        for event, fields, prefix in [
            ('UserPromptSubmit', {'prompt': 'x' * 45 + ' sk-' + 's' * 100}, 'you: ' + 'x' * 45 + ' [REDACTED]'),
            ('PostToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'x' * 25 + ' ghp_' + 's' * 100}}, 'Bash: ' + 'x' * 25 + ' [REDACTED]')]:
            with patch.object(hl.sys, 'stdin', io.StringIO(json.dumps(dict(session_id='private', **fields)))):
                hl.handle_hook(event)
            self.assertEqual(hl.load_json(hl.sess_path('private'))['activity'], prefix)
        self.assertEqual(hl.redact('pytest -q tests/test_render.py'), 'pytest -q tests/test_render.py')

    def test_snippets_off_clears_existing_text_and_stores_no_new_text(self):
        hl.update_session('private', {'activity': 'synthetic old prompt', 'activity_ts': 1})
        self.assertEqual(hl.main(['--snippets', 'off']), 0)
        self.assertEqual(hl.load_config()['snippets'], 'off')
        saved = hl.load_json(hl.sess_path('private'))
        self.assertNotIn('activity', saved)
        self.assertNotIn('activity_ts', saved)
        for event, fields, state in [
            ('UserPromptSubmit', {'prompt': 'synthetic hidden prompt'}, 'working'),
            ('PostToolUse', {'tool_name': 'synthetic hidden tool', 'tool_input': {'command': 'synthetic hidden command'}}, 'working'),
            ('NeedsYouPermission', {}, 'needs_you'), ('Stop', {}, 'idle')]:
            with patch.object(hl.sys, 'stdin', io.StringIO(json.dumps(dict(session_id='private', **fields)))):
                hl.handle_hook(event)
            saved = hl.load_json(hl.sess_path('private'))
            self.assertEqual(saved['state'], state)
            self.assertNotIn('activity', saved)
            self.assertNotIn('synthetic hidden', Path(hl.sess_path('private')).read_text())
        self.assertEqual(hl.main(['--snippets', 'on']), 0)
        with patch.object(hl.sys, 'stdin', io.StringIO('{"session_id":"private","prompt":"visible again"}')):
            hl.handle_hook('UserPromptSubmit')
        self.assertEqual(hl.load_json(hl.sess_path('private'))['activity'], 'you: visible again')
        for argv in (['--snippets'], ['--snippets', 'invalid']):
            self.assertEqual(hl.main(argv), 1)
        self.assertEqual(hl.load_config()['snippets'], 'on')

    def test_sanitized_or_truncated_session_ids_do_not_alias(self):
        for left, right in (('../x', '___x'), ('a' * 80 + 'x', 'a' * 80 + 'y')):
            self.assertNotEqual(hl.sess_path(left), hl.sess_path(right))
            hl.update_session(left, {'cost': 1})
            hl.update_session(right, {'cost': 2})
            self.assertEqual(hl.load_json(hl.sess_path(left))['cost'], 1)
            self.assertEqual(hl.load_json(hl.sess_path(right))['cost'], 2)


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
