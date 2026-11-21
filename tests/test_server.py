import io
import contextlib
import imaplib
import json
import os
import ssl
import subprocess
import sys
import time
import unittest
from unittest import mock
from icloud_mail_readonly import mail, server

RAW = b'Subject: Hello\r\nFrom: sender@example.invalid\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello fixture!'
HEADER = b'Subject: Hello\r\nFrom: sender@example.invalid\r\n\r\n'

class FakeSocket:
    def __init__(self, oversized=False):
        self.pending = bytearray(b'* OK ready\r\n')
        self.sent = []
        self.closed = False
        self.oversized = oversized
        self.literal_read = 0
        self.in_oversized = False
        self.recv_sizes = []
    def makefile(self, mode):
        return io.BytesIO()
    def settimeout(self, value):
        pass
    def recv(self, n):
        self.recv_sizes.append(n)
        if self.in_oversized:
            self.literal_read += min(n, len(self.pending))
        data = self.pending[:n]
        del self.pending[:n]
        return bytes(data)
    def sendall(self, data):
        self.sent.append(data)
        tag, command = data.rstrip(b'\r\n').split(b' ', 1)
        if command == b'CAPABILITY':
            response = b'* CAPABILITY IMAP4rev1\r\n' + tag + b' OK capability\r\n'
        elif command.startswith(b'LOGIN '):
            response = tag + b' OK logged in\r\n'
        elif command.startswith(b'LIST '):
            response = b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n' + tag + b' OK list\r\n'
        elif command.startswith(b'EXAMINE '):
            response = b'* 1 EXISTS\r\n* OK [UIDVALIDITY 99] valid\r\n' + tag + b' OK [READ-ONLY] done\r\n'
        elif command.startswith(b'FETCH '):
            response = b'* 1 FETCH (UID 42 RFC822.SIZE ' + str(len(RAW)).encode() + b' BODY[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)] {' + str(len(HEADER)).encode() + b'}\r\n' + HEADER + b')\r\n' + tag + b' OK fetch\r\n'
        elif command.startswith(b'UID FETCH '):
            if self.oversized:
                response = b'* 1 FETCH (UID 42 RFC822.SIZE 999999 BODY[] {999999}\r\n'
                self.pending.extend(response)
                self.pending.extend(b'X' * 64)
                # Once the announcement has been read, any attempted literal
                # read would consume these sentinel bytes.
                self.oversized_boundary = len(self.pending) - 64
                return
            response = b'* 1 FETCH (UID 42 RFC822.SIZE ' + str(len(RAW)).encode() + b' BODY[]<0> {' + str(len(RAW)).encode() + b'}\r\n' + RAW + b')\r\n' + tag + b' OK fetch\r\n'
        else:
            raise AssertionError('Unexpected fake IMAP command')
        self.pending.extend(response)
    def shutdown(self, how):
        self.closed = True
    def close(self):
        self.closed = True

class MailTests(unittest.TestCase):
    def setUp(self):
        self.sock = FakeSocket()
        self.socket_patch = mock.patch.object(mail.BoundedIMAP, '_create_socket', return_value=self.sock)
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
    def run_mail(self, op, args):
        return mail.run(op, args, 'fake@example.invalid', 'fake-password')
    def test_folders_and_tls_context(self):
        original = ssl.create_default_context
        contexts = []
        def context():
            c = original()
            contexts.append(c)
            return c
        with mock.patch.object(mail.ssl, 'create_default_context', side_effect=context):
            self.assertEqual(self.run_mail('list_folders', {}), {'folders': ['INBOX']})
        self.assertTrue(contexts[0].check_hostname)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.sock.closed)
    def test_fixed_host_and_tls_server_hostname(self):
        import imaplib
        with mock.patch.object(imaplib.IMAP4, '_create_socket', return_value=self.sock) as create, mock.patch.object(mail.ssl.SSLContext, 'wrap_socket', return_value=self.sock) as wrap, mock.patch.object(mail.BoundedIMAP, '_create_socket', mail.imaplib.IMAP4_SSL._create_socket):
            client = mail.BoundedIMAP()
            try:
                self.assertEqual((client.host, client.port), ('imap.mail.me.com', 993))
                self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'imap.mail.me.com')
                self.assertEqual(create.call_args.args[-1], mail.TIMEOUT)
            finally:
                client.shutdown()

    def test_page_uses_examine_and_peek(self):
        result = self.run_mail('list_messages', {'mailbox': 'INBOX', 'limit': 3})
        self.assertEqual(result['messages'][0]['uid'], '42')
        self.assertEqual(result['uidvalidity'], '99')
        sent = b''.join(self.sock.sent)
        self.assertIn(b'EXAMINE "INBOX"', sent)
        self.assertIn(b'BODY.PEEK[HEADER.FIELDS', sent)
        for forbidden in (b' SELECT ', b' CLOSE', b' EXPUNGE', b' STORE', b' COPY', b' MOVE', b' APPEND'):
            self.assertNotIn(forbidden, sent)
    def test_read_uses_uid_fetch_peek(self):
        result = self.run_mail('read_message', {'uid': '42'})
        self.assertEqual(result['text'], 'Hello fixture!')
        self.assertIn(b'UID FETCH 42 (RFC822.SIZE BODY.PEEK[]<0.262144>)', b''.join(self.sock.sent))
    def test_oversized_literal_rejected_before_read(self):
        self.sock.oversized = True
        clients = []
        original = mail.BoundedIMAP.read
        def read(client, size):
            clients.append((size, len(client.buffer)))
            return original(client, size)
        with mock.patch.object(mail.BoundedIMAP, 'read', read), self.assertRaises(mail.MailError):
            self.run_mail('read_message', {'uid': '42'})
        self.assertEqual(clients[0][0], 999999)
        self.assertLessEqual(clients[0][1], mail.READ_CHUNK - 1)
        self.assertEqual(len(self.sock.pending) + clients[0][1], 64)
        self.assertTrue(self.sock.closed)

    def test_debug_cannot_leak_during_constructor_or_login(self):
        captured = io.StringIO()
        old_recv = self.sock.recv
        def recv(n):
            imaplib.Debug = 4
            return old_recv(n)
        with mock.patch.object(imaplib, 'Debug', 4), mock.patch.object(self.sock, 'recv', recv), contextlib.redirect_stderr(captured):
            client = mail.BoundedIMAP()
            client.debug = 4
            client.login('fake-user', 'fake-password-secret')
            client.shutdown()
        self.assertIn(b'fake-password-secret', b''.join(self.sock.sent))
        self.assertEqual(captured.getvalue(), '')
        self.assertEqual(client.debug, 0)

    def test_chunked_lines_and_literal_integrity(self):
        client = mail.BoundedIMAP()
        self.sock.recv_sizes.clear()
        data = b'A' * 5000 + b'\r\n' + b'* 1 FETCH (BODY[] {7}\r\n' + b'ab\ncd\x00!' + b')\r\n'
        self.sock.pending.extend(data)
        before = client.remaining
        self.assertEqual(client.readline(), b'A' * 5000 + b'\r\n')
        self.assertEqual(client.readline(), b'* 1 FETCH (BODY[] {7}\r\n')
        self.assertEqual(client.read(7), b'ab\ncd\x00!')
        self.assertEqual(client.readline(), b')\r\n')
        self.assertEqual(before - client.remaining, len(data))
        self.assertLessEqual(len(self.sock.recv_sizes), 3)
        self.assertTrue(all(n <= mail.READ_CHUNK for n in self.sock.recv_sizes))
        client.shutdown()

    def test_line_and_wire_limits_with_readahead(self):
        client = mail.BoundedIMAP()
        self.sock.pending.extend(b'abc\nXYZ')
        client.remaining = 6
        self.assertEqual(client.readline(), b'abc\n')
        self.assertEqual(client.remaining, 0)
        self.assertEqual(client.read(2), b'XY')
        with self.assertRaises(client.abort):
            client.read(1)
        self.assertEqual(bytes(self.sock.pending), b'Z')
        self.sock.pending[:] = b'* OK ready\r\n'
        client = mail.BoundedIMAP()
        self.sock.pending.extend(b'a' * mail.MAX_LINE + b'\n')
        with self.assertRaises(client.abort):
            client.readline()

    def test_deadline_applies_to_buffered_bytes(self):
        client = mail.BoundedIMAP()
        self.sock.pending.extend(b'one\ntwo\n')
        self.assertEqual(client.readline(), b'one\n')
        client.deadline = time.monotonic() - 1
        with self.assertRaises(client.abort):
            client.readline()

    def test_short_reads_and_exact_caps(self):
        client = mail.BoundedIMAP()
        original = self.sock.recv
        with mock.patch.object(self.sock, 'recv', side_effect=lambda n: original(min(n, 37))):
            self.sock.pending.extend(b'a' * (mail.MAX_LINE - 1) + b'\n' + b'Z' * mail.MAX_LITERAL)
            self.assertEqual(client.readline(), b'a' * (mail.MAX_LINE - 1) + b'\n')
            self.assertEqual(client.read(mail.MAX_LITERAL), b'Z' * mail.MAX_LITERAL)
            self.assertEqual(len(client.buffer), 0)
        client.shutdown()

    def test_injection_and_bounds_rejected_before_connect(self):
        for op, args in [('read_message', {'uid': '1\r\nA STORE 1 FLAGS (\\Seen)'}), ('read_message', {'uid': '0'}), ('read_message', {'uid': '4294967296'}), ('list_messages', {'mailbox': 'INBOX\r\nA EXPUNGE'}), ('list_messages', {'limit': 51}), ('list_messages', {'offset': True})]:
            with self.subTest(args=args), self.assertRaises(mail.MailError):
                self.run_mail(op, args)
        self.assertEqual(self.sock.sent, [])
    def test_quoted_mailbox_and_credentials(self):
        self.run_mail('list_messages', {'mailbox': 'x" y\\z'})
        self.assertIn(b'EXAMINE "x\\" y\\\\z"', b''.join(self.sock.sent))
    def test_refuses_select_and_mutation(self):
        client = mail.BoundedIMAP()
        try:
            with self.assertRaises(client.error):
                client.select('INBOX')
            with self.assertRaises(client.error):
                client._command('STORE', '1', '+FLAGS', '(\\Seen)')
            with self.assertRaises(client.error):
                client._command('UID', 'STORE', '1')
        finally:
            client.shutdown()
    def test_size_and_mime_limits(self):
        with self.assertRaises(mail.MailError):
            mail._message(b'x' * (mail.MAX_LITERAL + 1))
        raw = b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n' + b''.join(b'--x\r\n\r\na\r\n' for _ in range(101)) + b'--x--\r\n'
        with self.assertRaises(mail.MailError):
            mail._message(raw)
    def test_response_byte_budget_fails_before_read(self):
        client = mail.BoundedIMAP()
        client.remaining = 2
        before = bytes(self.sock.pending)
        with self.assertRaises(client.abort):
            client.read(3)
        self.assertEqual(bytes(self.sock.pending), before)
        self.assertTrue(self.sock.closed)

    def test_response_line_count_limit(self):
        client = mail.BoundedIMAP()
        client.lines = mail.MAX_LINES
        with self.assertRaises(client.abort):
            client.readline()
        self.assertTrue(self.sock.closed)

    def test_deadline(self):
        client = mail.BoundedIMAP()
        try:
            client.deadline = time.monotonic() - 1
            with self.assertRaises(client.abort):
                client.read(1)
            self.assertTrue(self.sock.closed)
        finally:
            client.shutdown()

class MCPTests(unittest.TestCase):
    def ready(self):
        session = server.Session()
        self.assertIn('result', session.dispatch({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}))
        session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return session
    def test_stdio_discovery_without_credentials(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'list_folders', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'},
        ]
        env = {'PYTHONPATH': os.path.abspath('src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        proc = subprocess.run([sys.executable, '-B', '-m', 'icloud_mail_readonly.server'], input=''.join(json.dumps(r)+'\n' for r in requests), text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, '')
        responses = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual([t['name'] for t in responses[1]['result']['tools']], ['list_folders', 'list_messages', 'read_message', 'list_calendars', 'list_events', 'get_event', 'create_event', 'update_event', 'delete_event'])
        self.assertTrue(responses[2]['result']['isError'])
        self.assertIn('credentials', responses[2]['result']['content'][0]['text'])

    def test_stdio_rejects_invalid_lifecycle(self):
        params = {'protocolVersion': 'future', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': None, 'method': 'initialize', 'params': params},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {**params, 'capabilities': None}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'initialize', 'params': params},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'read_message', 'arguments': {'uid': '4294967296'}}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/list'},
        ]
        env = {'PYTHONPATH': os.path.abspath('src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        proc = subprocess.run([sys.executable, '-B', '-m', 'icloud_mail_readonly.server'], input=''.join(json.dumps(r) + '\n' for r in requests), text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual((proc.returncode, proc.stderr), (0, ''))
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, None, 2, 3, 4, 5, 6])
        self.assertEqual([responses[i]['error']['code'] for i in (0, 1, 2, 4, 5)], [-32002, -32600, -32602, -32002, -32602])
        self.assertEqual(responses[3]['result']['protocolVersion'], server.LATEST_PROTOCOL)
        self.assertEqual(len(responses[-1]['result']['tools']), 9)

    def test_stdio_tool_call_with_synthetic_imap(self):
        program = '''
from unittest.mock import patch
from test_server import FakeSocket
from icloud_mail_readonly import mail, server
with patch.object(mail.BoundedIMAP, "_create_socket", return_value=FakeSocket()), patch("socket.create_connection", side_effect=AssertionError("network prohibited")):
    server.main()
'''
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'read_message', 'arguments': {'uid': '42'}}},
        ]
        env = {'PYTHONPATH': os.path.abspath('src') + os.pathsep + os.path.abspath('tests'), 'PYTHONDONTWRITEBYTECODE': '1', 'ICLOUD_EMAIL': 'fake@example.invalid', 'ICLOUD_APP_PASSWORD': 'fake-password'}
        proc = subprocess.run([sys.executable, '-B', '-c', program], input=''.join(json.dumps(r) + '\n' for r in requests), text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, '')
        result = json.loads(proc.stdout.splitlines()[-1])['result']
        self.assertFalse(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['text'], 'Hello fixture!')

    def test_console_script_metadata(self):
        import tomllib
        with open('pyproject.toml', 'rb') as f:
            metadata = tomllib.load(f)
        self.assertEqual(metadata['project']['scripts']['icloud-mail-readonly-mcp'], 'icloud_mail_readonly.server:main')
        self.assertEqual(metadata['project']['dependencies'], [])

    def test_never_disclose_credentials_or_backend_exception(self):
        with mock.patch.dict(os.environ, {'ICLOUD_EMAIL': 'fake@example.invalid', 'ICLOUD_APP_PASSWORD': 'fake-secret'}, clear=True), mock.patch.object(mail, 'BoundedIMAP', side_effect=Exception('fake-secret')):
            response = self.ready().dispatch({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {'name': 'list_folders'}})
        self.assertNotIn('fake-secret', json.dumps(response))
        self.assertTrue(response['result']['isError'])
    def test_invalid_calls_and_notifications(self):
        session = self.ready()
        self.assertIsNone(session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        bad = session.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'list_folders', 'arguments': {'host': 'evil.invalid'}}})
        self.assertEqual(bad['error']['code'], -32602)

    def test_initialize_lifecycle_and_errors(self):
        params = {'protocolVersion': 'unknown', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}
        init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': params}
        call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
        session = server.Session()
        self.assertEqual(session.dispatch(call)['error']['code'], -32002)
        self.assertIsNone(session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertEqual(session.dispatch(call)['error']['code'], -32002)
        for invalid in [None, True, [], {}]:
            with self.subTest(id=invalid):
                self.assertEqual(session.dispatch({**init, 'id': invalid})['error']['code'], -32600)
        self.assertIsNone(session.dispatch({k: v for k, v in init.items() if k != 'id'}))
        for key, value in [('protocolVersion', None), ('protocolVersion', []), ('capabilities', None), ('capabilities', []), ('clientInfo', None), ('clientInfo', {}), ('clientInfo', {'name': 'x', 'version': 1})]:
            with self.subTest(key=key, value=value):
                self.assertEqual(session.dispatch({**init, 'params': {**params, key: value}})['error']['code'], -32602)
        for key in params:
            self.assertEqual(session.dispatch({**init, 'params': {k: v for k, v in params.items() if k != key}})['error']['code'], -32602)
        self.assertEqual(session.dispatch(init)['result']['protocolVersion'], server.LATEST_PROTOCOL)
        self.assertEqual(session.dispatch(call)['error']['code'], -32002)
        self.assertEqual(session.dispatch(init)['error']['code'], -32600)
        session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertIn('tools', session.dispatch(call)['result'])
        self.assertEqual(session.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'unknown'})['error']['code'], -32601)

    def test_tool_schema_matches_validation_and_never_connects(self):
        session = self.ready()
        invalid = [('list_messages', {'mailbox': ''}), ('list_messages', {'mailbox': 'a\n'}), ('list_messages', {'limit': True}), ('list_messages', {'limit': 1.5}), ('list_messages', {'offset': -1}), ('read_message', {}), ('read_message', {'uid': '0'}), ('read_message', {'uid': '4294967296'}), ('read_message', {'uid': '42\n'}), ('list_folders', {'extra': 1})]
        with mock.patch.object(mail, 'run', side_effect=AssertionError('must not call mail')) as run:
            for name, args in invalid:
                with self.subTest(name=name, args=args):
                    response = session.dispatch({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
                    self.assertEqual(response['error']['code'], -32602)
            run.assert_not_called()
        try:
            import jsonschema
        except ImportError:
            self.skipTest('optional jsonschema not installed')
        schemas = {t['name']: t['inputSchema'] for t in server.TOOLS}
        for schema in schemas.values():
            jsonschema.Draft202012Validator.check_schema(schema)
        for name, args in invalid:
            with self.subTest(schema=name, args=args):
                self.assertFalse(jsonschema.Draft202012Validator(schemas[name]).is_valid(args))
        for uid in ('1', '999999999', '1000000000', '4000000000', '4294967295'):
            self.assertTrue(jsonschema.Draft202012Validator(schemas['read_message']).is_valid({'uid': uid}))

    def test_standard_mcp_sdk_stdio_lifecycle(self):
        try:
            import mcp
        except ImportError:
            self.skipTest('optional MCP SDK not installed')
        import asyncio
        from mcp.client.stdio import stdio_client
        program = '''
from unittest.mock import patch
from test_server import FakeSocket
from icloud_mail_readonly import mail, server
with patch.object(mail.BoundedIMAP, "_create_socket", return_value=FakeSocket()), patch("socket.create_connection", side_effect=AssertionError("network prohibited")):
    server.main()
'''
        env = {'PYTHONPATH': os.path.abspath('src') + os.pathsep + os.path.abspath('tests'), 'PYTHONDONTWRITEBYTECODE': '1', 'ICLOUD_EMAIL': 'fake@example.invalid', 'ICLOUD_APP_PASSWORD': 'fake-password'}
        async def exercise():
            params = mcp.StdioServerParameters(command=sys.executable, args=['-B', '-c', program], env=env)
            async with stdio_client(params) as (reader, writer):
                async with mcp.ClientSession(reader, writer) as client:
                    initialized = await client.initialize()
                    version = initialized.model_dump(by_alias=True)['protocolVersion']
                    self.assertIn(version, server.PROTOCOLS)
                    tools = await client.list_tools()
                    self.assertEqual(len(tools.tools), 9)
                    result = await client.call_tool('read_message', {'uid': '42'})
                    self.assertFalse(result.model_dump(by_alias=True)['isError'])
                    self.assertEqual(json.loads(result.content[0].text)['text'], 'Hello fixture!')
        with mock.patch.dict(os.environ, {}, clear=True):
            asyncio.run(exercise())

class CalendarDispatchTests(unittest.TestCase):
    def call(self, name, arguments):
        session = server.Session()
        server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': server.LATEST_PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}, session)
        server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, session)
        return server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, session)

    def test_calendar_tools_route_to_calendar_module(self):
        args = {'calendar_id': 'Y2Fs', 'start': '2027-01-01T00:00:00Z', 'end': '2027-01-02T00:00:00Z'}
        with mock.patch.object(server.calendar, 'run', return_value={'events': []}) as run, mock.patch.object(server.mail, 'run', side_effect=AssertionError('mail must not run')), mock.patch.dict(os.environ, {'ICLOUD_EMAIL': 'fake@example.invalid', 'ICLOUD_APP_PASSWORD': 'fake-password'}):
            response = self.call('list_events', args)
        self.assertFalse(response['result']['isError'])
        run.assert_called_once_with('list_events', args, 'fake@example.invalid', 'fake-password')

    def test_calendar_schema_rejects_weak_etags_extra_keys_and_long_text(self):
        base = {'calendar_id': 'Y2Fs', 'event_id': 'ZXZlbnQ', 'summary': 'x', 'start': '2027-01-01T10:00:00Z', 'end': '2027-01-01T11:00:00Z'}
        with mock.patch.dict(os.environ, {'ICLOUD_EMAIL': 'fake@example.invalid', 'ICLOUD_APP_PASSWORD': 'fake-password'}):
            weak = self.call('delete_event', {'calendar_id': 'Y2Fs', 'event_id': 'ZXZlbnQ', 'etag': 'W/"v1"'})
            extra = self.call('create_event', {**base, 'recurrence': 'FREQ=DAILY'})
            long_text = self.call('create_event', {**base, 'summary': 'x' * 4097})
        for response in (weak, extra, long_text):
            self.assertEqual(response['error']['code'], -32602)
        create = {k: v for k, v in base.items() if k != 'event_id'}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(self.call('create_event', create)['result']['isError'])


if __name__ == '__main__':
    unittest.main()
