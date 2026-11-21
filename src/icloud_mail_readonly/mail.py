"""Bounded read-only IMAP operations. Never log server responses or credentials."""
import email
import imaplib
import re
import socket
import ssl
import time
from email import policy

HOST = "imap.mail.me.com"
PORT = 993
MAX_LITERAL = 262144
MAX_WIRE = 2 * 1024 * 1024
MAX_LINES = 3000
MAX_LINE = 8192
READ_CHUNK = 4096
TIMEOUT = 20

class MailError(Exception):
    pass

class BoundedIMAP(imaplib.IMAP4_SSL):
    """Bound all socket reads and validate literal sizes before delivering them."""

    @property
    def debug(self):
        return 0

    @debug.setter
    def debug(self, value):
        pass  # imaplib's constructor assigns the process-global Debug value.

    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT
        self.remaining = MAX_WIRE
        self.lines = 0
        self.buffer = bytearray()
        super().__init__(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)

    def _log(self, line):
        pass

    def _mesg(self, message, secs=None):
        pass

    def _fail(self):
        try:
            self.shutdown()
        except Exception:
            pass
        raise self.abort("IMAP response limit exceeded")

    def _check_deadline(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            self._fail()
        return left

    def _receive(self, size):
        left = self._check_deadline()
        if size <= 0 or self.remaining <= 0:
            self._fail()
        self.sock.settimeout(left)
        chunk = self.sock.recv(min(size, READ_CHUNK, self.remaining))
        if not chunk:
            raise self.abort("unexpected EOF")
        self.remaining -= len(chunk)
        self.buffer.extend(chunk)
        self._check_deadline()

    def read(self, size):
        self._check_deadline()
        if size < 0 or size > MAX_LITERAL or size > self.remaining + len(self.buffer):
            self._fail()
        while len(self.buffer) < size:
            self._receive(size - len(self.buffer))
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def readline(self):
        self.lines += 1
        if self.lines > MAX_LINES:
            self._fail()
        while True:
            self._check_deadline()
            end = self.buffer.find(b'\n', 0, MAX_LINE)
            if end >= 0:
                line = bytes(self.buffer[:end + 1])
                del self.buffer[:end + 1]
                return line
            if len(self.buffer) >= MAX_LINE:
                self._fail()
            self._receive(MAX_LINE - len(self.buffer))

    def _command(self, name, *args):
        if name not in {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "FETCH", "UID"}:
            raise self.error("command forbidden")
        if name == "UID" and (not args or args[0] != "FETCH"):
            raise self.error("UID command forbidden")
        return super()._command(name, *args)

    def select(self, mailbox='INBOX', readonly=False):
        if not readonly:
            raise self.error("writable selection forbidden")
        return super().select(mailbox, readonly=True)


def mailbox_arg(value):
    # IMAP modified UTF-7 is ASCII; callers use the name returned by LIST.
    if not isinstance(value, str) or not 1 <= len(value) <= 255:
        raise MailError("Invalid mailbox")
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise MailError("Invalid mailbox")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def uid_arg(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) > 4294967295:
        raise MailError("Invalid UID")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise MailError("Invalid pagination")
    return value


def ok(response):
    status, data = response
    if status != 'OK':
        raise MailError("IMAP request failed")
    return data


def _quoted_mailbox(raw):
    # LIST syntax: flags, delimiter and mailbox. Literal mailbox names are
    # deliberately rejected rather than guessed or converted incorrectly.
    m = re.fullmatch(rb'\([^\r\n]*?\) (?:NIL|"(?:[^"\\]|\\["\\])*") ("(?:[^"\\]|\\["\\])*"|[^ (){\r\n]+)', raw, re.I)
    if not m:
        raise MailError("Unsupported folder response")
    name = m.group(1)
    if name.startswith(b'"'):
        name = re.sub(rb'\\(["\\])', rb'\1', name[1:-1])
    try:
        result = name.decode('ascii')
    except UnicodeDecodeError:
        raise MailError("Unsupported folder name") from None
    mailbox_arg(result)
    return result


def _literal(data):
    found = [x[1] for x in data if isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], bytes)]
    if len(found) != 1:
        raise MailError("Unexpected message response")
    return found[0]


def _message(raw):
    if len(raw) > MAX_LITERAL:
        raise MailError("Message too large")
    msg = email.message_from_bytes(raw, policy=policy.default)
    headers = {key: str(msg.get(key, ''))[:1000] for key in ('Subject', 'From', 'To', 'Date', 'Message-ID')}
    parts = []
    stack = [(msg, 0)]
    text = ''
    count = 0
    while stack:
        part, depth = stack.pop()
        count += 1
        if count > 100 or depth > 12:
            raise MailError("Message MIME structure too complex")
        if part.is_multipart():
            children = part.get_payload()
            if not isinstance(children, list) or len(children) > 100:
                raise MailError("Message MIME structure too complex")
            stack.extend((child, depth + 1) for child in reversed(children))
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == 'attachment' or filename:
            if len(parts) < 30:
                parts.append({'filename': str(filename or '')[:255], 'content_type': part.get_content_type()[:100]})
        elif not text and part.get_content_type() == 'text/plain':
            payload = part.get_payload(decode=True) or b''
            try:
                text = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')[:20000]
            except (LookupError, TypeError):
                text = payload.decode('utf-8', errors='replace')[:20000]
    return {'headers': headers, 'text': text, 'attachments': parts}


def run(operation, args, username, password):
    # Validation occurs before any network activity. Credentials are never echoed.
    if not username or not password or any(not isinstance(v, str) or len(v) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in (username, password)):
        raise MailError("Missing or invalid iCloud credentials")
    folder = None
    if operation in ('list_messages', 'read_message'):
        folder = mailbox_arg(args.get('mailbox', 'INBOX'))
    if operation == 'list_messages':
        offset = integer(args.get('offset', 0), 0, 1000000)
        limit = integer(args.get('limit', 20), 1, 50)
    elif operation == 'read_message':
        uid = uid_arg(args.get('uid'))
    elif operation != 'list_folders':
        raise MailError("Unknown operation")
    client = None
    try:
        client = BoundedIMAP()
        # Pre-quote the username as older imaplib versions send it verbatim.
        quoted_user = '"' + username.replace('\\', '\\\\').replace('"', '\\"') + '"'
        ok(client.login(quoted_user, password))
        if operation == 'list_folders':
            data = ok(client.list('""', '"*"'))
            if len(data) > 200:
                raise MailError("Too many folders")
            if any(not isinstance(x, bytes) for x in data):
                raise MailError("Unsupported folder response")
            return {'folders': [_quoted_mailbox(x) for x in data]}
        data = ok(client.select(folder, readonly=True))
        if not data or not isinstance(data[0], bytes) or not data[0].isdigit():
            raise MailError("Invalid mailbox count")
        total = int(data[0])
        validity_data = client.response('UIDVALIDITY')[1]
        validity = validity_data[0].decode('ascii') if validity_data and isinstance(validity_data[0], bytes) and validity_data[0].isdigit() else None
        if operation == 'list_messages':
            end = max(0, total - offset)
            if end == 0:
                return {'messages': [], 'total': total, 'next_offset': None, 'uidvalidity': validity}
            start = max(1, end - limit + 1)
            data = ok(client.fetch(f'{start}:{end}', '(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])'))
            messages = []
            for item in data:
                if isinstance(item, tuple) and len(item) == 2:
                    meta, raw = item
                    match = re.search(rb'\bUID (\d+)\b', meta)
                    size = re.search(rb'\bRFC822.SIZE (\d+)\b', meta)
                    if not match or not size or len(raw) > 32768:
                        raise MailError("Invalid message listing")
                    msg = email.message_from_bytes(raw, policy=policy.default)
                    messages.append({'uid': match.group(1).decode('ascii'), 'size': int(size.group(1)), 'headers': {h: str(msg.get(h, ''))[:1000] for h in ('Subject', 'From', 'Date', 'Message-ID')}})
            if len(messages) > limit:
                raise MailError("Too many messages")
            messages.reverse()
            return {'messages': messages, 'total': total, 'next_offset': offset + len(messages) if start > 1 else None, 'uidvalidity': validity}
        data = ok(client.uid('FETCH', uid, '(RFC822.SIZE BODY.PEEK[]<0.262144>)'))
        raw = _literal(data)
        metadata = next((x[0] for x in data if isinstance(x, tuple)), b'')
        size = re.search(rb'\bRFC822.SIZE (\d+)\b', metadata)
        if not size or int(size.group(1)) > MAX_LITERAL or len(raw) != int(size.group(1)):
            raise MailError("Message exceeds size limit or is incomplete")
        return {'uid': uid, 'uidvalidity': validity, **_message(raw)}
    except MailError:
        raise
    except Exception:
        raise MailError("Mail request failed; check credentials, connection and server limits") from None
    finally:
        if client is not None:
            try:
                client.shutdown()  # Never CLOSE: it can expunge.
            except Exception:
                pass
