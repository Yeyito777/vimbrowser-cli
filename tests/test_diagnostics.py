"""Transport-only fixtures: no browser, account or host display access."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ipc, commands


class DiagnosticsTests(unittest.TestCase):
    def test_explicit_missing_environment_never_falls_through(self):
        with patch.dict(os.environ, {'VIMBROWSER_IPC': '/nonexistent/fixture.sock'}):
            self.assertEqual(str(ipc.socket_path()), '/nonexistent/fixture.sock')

    def test_connect_failure_is_not_sent(self):
        with self.assertRaises(ipc.IpcError) as caught:
            ipc.send('mutation secret', socket_override='/nonexistent/fixture.sock')
        self.assertEqual(caught.exception.phase, 'connect')
        self.assertEqual(caught.exception.outcome, 'not_sent')
        self.assertNotIn('secret', str(caught.exception))

    def test_slow_drip_has_total_deadline_not_per_read_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + '/ipc.sock'
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen(1)
            def serve():
                conn, _ = listener.accept()
                with conn:
                    conn.recv(4096)
                    for _ in range(30):
                        try:
                            conn.sendall(b'x')
                        except OSError:
                            break
                        time.sleep(.02)
            thread = threading.Thread(target=serve)
            thread.start()
            began = time.monotonic()
            try:
                with self.assertRaises(ipc.IpcError) as caught:
                    ipc.send('mutation secret', socket_override=path, timeout=.12)
                self.assertLess(time.monotonic() - began, .35)
                self.assertEqual(caught.exception.phase, 'receive')
                self.assertEqual(caught.exception.outcome, 'unknown')
                self.assertGreater(caught.exception.received_bytes, 0)
                self.assertNotIn('secret', str(caught.exception))
            finally:
                thread.join(1)
                listener.close()

    def test_auth_is_evidence_not_a_root_cause_or_signin_diagnosis(self):
        result = commands._auth_evidence('https://weblogin.utoronto.ca/idp?token=PRIVATE#SECRET', 'https://outlook.office.com/mail/')
        self.assertEqual(result['state'], 'authentication_redirect_evidence')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(commands._auth_evidence('https://weblogin.utoronto.ca.evil.test/', None)['state'], 'unknown')
        self.assertEqual(commands._auth_evidence('https://example.test/auth', None)['state'], 'unknown')

    def test_diagnose_does_not_probe_renderer_or_print_other_tabs(self):
        calls = []
        def send(command, **kwargs):
            calls.append(command)
            if command == 'health':
                return '{"ok":true,"operation":{"state":"in_flight"}}'
            return json.dumps({'tab': {'id': 581, 'url': 'https://weblogin.utoronto.ca/?PRIVATE',
                                     'title': 'PRIVATE', 'loading': False}, 'main_frame_valid': True})
        output = io.StringIO()
        with patch.object(ipc, 'send', send), contextlib.redirect_stdout(output):
            commands.cmd_diagnose(['581', '--socket', '/fixture', '--expected-origin', 'https://outlook.office.com'])
        result = json.loads(output.getvalue())
        self.assertEqual(calls, ['health', 'diagnose-tab 581'])
        self.assertEqual(result['renderer_readiness'], 'unknown_not_probed')
        self.assertEqual(result['historical_timeout_cause'], 'unknown')
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_diagnose_exists_requires_native_found_metadata(self):
        import copy
        found = {'ok': True, 'tab': {'id': 581, 'url': 'https://example.test/'},
                 'browser_present': False, 'main_frame_valid': False}
        cases = [('lazy', found, True),
                 ('materialized', {**found, 'browser_present': True, 'main_frame_valid': True}, True),
                 ('missing', {'ok': True, 'tab': None}, False)]
        for key in ('browser_present', 'main_frame_valid'):
            partial = copy.deepcopy(found)
            partial.pop(key)
            cases.append(('missing-' + key, partial, 'unknown'))
            malformed = copy.deepcopy(found)
            malformed[key] = 'unknown'
            cases.append(('invalid-' + key, malformed, 'unknown'))
        for where in ('top', 'tab'):
            for key, value, expected in (('exists', False, False), ('error', {'code': 'fixture'}, 'unknown'), ('ok', False, 'unknown')):
                payload = copy.deepcopy(found)
                (payload if where == 'top' else payload['tab'])[key] = value
                cases.append((where + '-' + key, payload, expected))
        mismatched = copy.deepcopy(found)
        mismatched['tab']['id'] = 582
        cases.append(('mismatched-id', mismatched, 'unknown'))
        cases.append(('legacy', {'tab': {'id': 581}, 'legacy_metadata': True}, 'unknown'))
        for label, payload, expected in cases:
            with self.subTest(label=label):
                output = io.StringIO()
                def send(command, **kwargs):
                    return json.dumps({'ok': True} if command == 'health' else payload)
                with patch.object(ipc, 'send', send), contextlib.redirect_stdout(output):
                    commands.cmd_diagnose(['581', '--socket', '/fixture'])
                result = json.loads(output.getvalue())
                self.assertEqual(result['target']['exists'], expected)
                self.assertEqual(result['renderer_readiness'], 'unknown_not_probed')

    def test_diagnose_timeout_never_retries(self):
        calls = []
        def send(command, **kwargs):
            calls.append(command)
            raise ipc.IpcError('timeout', phase='receive')
        with patch.object(ipc, 'send', send), contextlib.redirect_stdout(io.StringIO()):
            commands.cmd_diagnose(['581', '--socket', '/fixture'])
        self.assertEqual(calls, ['health', 'diagnose-tab 581'])

    def test_brief_has_one_mutation_and_no_retry(self):
        calls = []
        def send(args, command):
            calls.append(command)
            return '{"ok":true,"tab":{"id":42}}\n'
        with patch.object(commands, '_send', send), contextlib.redirect_stdout(io.StringIO()):
            commands.cmd_open_context(['--brief', 'fixture', 'https://example.test/'])
        self.assertEqual(calls, ['open-background-context-tab-brief fixture https://example.test/'])

    def test_invalid_deadline(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ipc.IpcError) as caught:
                ipc.send('status', socket_override='/fixture', timeout=value)
            self.assertEqual(caught.exception.outcome, 'not_sent')

if __name__ == '__main__':
    unittest.main()
