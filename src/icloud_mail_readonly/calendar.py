"""Bounded iCloud CalDAV operations with fixed Apple endpoints only."""
import base64
import http.client
import re
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

ROOT_HOST = 'caldav.icloud.com'
PORT = 443
TIMEOUT = 20
MAX_RESPONSE = 1024 * 1024
MAX_XML_NODES = 4000
MAX_CALENDARS = 100
MAX_EVENTS = 100
MAX_ICS = 65536
MAX_ICS_LINES = 1000
DAV = 'DAV:'
CALDAV = 'urn:ietf:params:xml:ns:caldav'
HOST_RE = re.compile(r'(?:caldav|p[0-9]{1,3}-caldav)\.icloud\.com\Z')
ETAG_RE = re.compile(r'"[^"\r\n]{1,512}"\Z')
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{1,1024}\Z')


class CalendarError(Exception):
    pass


def validate_endpoint(value):
    """Accept only HTTPS Apple CalDAV origins and clean absolute paths."""
    if not isinstance(value, str) or len(value) > 2048:
        raise CalendarError('Invalid calendar endpoint')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise CalendarError('Invalid calendar endpoint')
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        raise CalendarError('Invalid calendar endpoint') from None
    if not HOST_RE.fullmatch(host) or port not in (None, PORT) or parsed.query or parsed.fragment:
        raise CalendarError('Invalid calendar endpoint')
    return host, _clean_path(parsed.path or '/')


def _clean_path(path):
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 1536:
        raise CalendarError('Invalid calendar resource')
    try:
        decoded = unquote(path, encoding='utf-8', errors='strict')
    except UnicodeDecodeError:
        raise CalendarError('Invalid calendar resource') from None
    if any(ord(c) < 32 or c == '\\' for c in decoded):
        raise CalendarError('Invalid calendar resource')
    segments = decoded.split('/')
    if any(x in ('.', '..') for x in segments):
        raise CalendarError('Invalid calendar resource')
    return path


def _href_path(href, host):
    if not isinstance(href, str) or len(href) > 1536:
        raise CalendarError('Invalid server calendar resource')
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        other, path = validate_endpoint(href)
        if other != host:
            raise CalendarError('Server returned untrusted calendar resource')
        return path
    if parsed.query or parsed.fragment:
        raise CalendarError('Invalid server calendar resource')
    return _clean_path(parsed.path)


def encode_id(path):
    path = _clean_path(path)
    return base64.urlsafe_b64encode(path.encode('utf-8')).rstrip(b'=').decode('ascii')


def decode_id(token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise CalendarError('Invalid calendar identifier')
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
        return _clean_path(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise CalendarError('Invalid calendar identifier') from None


def _within(path, parent, event=False):
    if not parent.endswith('/'):
        parent += '/'
    if not path.startswith(parent) or path == parent:
        raise CalendarError('Calendar identifier is out of scope')
    suffix = path[len(parent):]
    if event:
        if '/' in suffix or not suffix.endswith('.ics'):
            raise CalendarError('Calendar identifier is out of scope')
    else:
        if suffix.endswith('/'):
            suffix = suffix[:-1]
        if not suffix or '/' in suffix:
            raise CalendarError('Calendar identifier is out of scope')
    return path


def _strong_etag(value):
    if not isinstance(value, str) or not ETAG_RE.fullmatch(value):
        raise CalendarError('A strong ETag is required')
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise CalendarError('Invalid timestamp')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise CalendarError('Invalid timestamp') from None


def _ical_time(value):
    return _timestamp(value).strftime('%Y%m%dT%H%M%SZ')


def _public_time(value):
    if not re.fullmatch(r'\d{8}T\d{6}Z', value or ''):
        raise CalendarError('Unsupported event timezone or date format')
    try:
        return datetime.strptime(value, '%Y%m%dT%H%M%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise CalendarError('Invalid calendar event') from None


def _text(value, required=False):
    if value is None and not required:
        return ''
    if not isinstance(value, str) or (required and not value) or len(value) > 4096 or '\x00' in value:
        raise CalendarError('Invalid event text')
    return value


def _escape(value):
    return value.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _unescape(value):
    out, escaped = [], False
    for char in value:
        if escaped:
            out.append('\n' if char in 'nN' else char)
            escaped = False
        elif char == '\\':
            escaped = True
        else:
            out.append(char)
    if escaped:
        raise CalendarError('Invalid calendar text')
    return ''.join(out)


def _unfold(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ICS:
        raise CalendarError('Calendar data exceeds limits')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise CalendarError('Invalid calendar encoding') from None
    if '\x00' in text:
        raise CalendarError('Invalid calendar data')
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if len(lines) > MAX_ICS_LINES:
        raise CalendarError('Calendar data exceeds limits')
    out = []
    for line in lines:
        if len(line) > 4096:
            raise CalendarError('Calendar data exceeds limits')
        if line.startswith((' ', '\t')):
            if not out:
                raise CalendarError('Invalid calendar folding')
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def parse_ics(raw):
    """Parse one VEVENT for display; unknown metadata and VTIMEZONE are ignored."""
    lines = _unfold(raw)
    if not lines or lines[0] != 'BEGIN:VCALENDAR' or lines[-1] != 'END:VCALENDAR':
        raise CalendarError('Unsupported calendar data')
    if 'VERSION:2.0' not in lines[:20]:
        raise CalendarError('Unsupported calendar data')
    blocks, current = [], None
    for line in lines:
        if line == 'BEGIN:VEVENT':
            if current is not None:
                raise CalendarError('Unsupported calendar data')
            current = []
        elif line == 'END:VEVENT':
            if current is None:
                raise CalendarError('Unsupported calendar data')
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    if not blocks or current is not None:
        raise CalendarError('Unsupported calendar data')
    masters = [block for block in blocks if not any(line.upper().startswith(('RECURRENCE-ID', 'EXDATE')) for line in block)]
    event_lines = masters[0] if masters else blocks[0]
    event = {}
    ignored = {'ATTACH', 'X-APPLE-STRUCTURED-LOCATION', 'X-WR-CALNAME', 'METHOD', 'PRODID', 'CALSCALE', 'VERSION'}
    recurring = {'RRULE', 'RDATE', 'EXDATE', 'RECURRENCE-ID'}
    allowed = {'UID', 'DTSTART', 'DTEND', 'DURATION', 'SUMMARY', 'DESCRIPTION', 'LOCATION'}
    has_recurrence = len(blocks) > 1
    for line in event_lines:
        if ':' not in line:
            raise CalendarError('Invalid calendar event')
        key, value = line.split(':', 1)
        name, _, raw_params = key.partition(';')
        params = {}
        if raw_params:
            if name.upper() not in {'DTSTART', 'DTEND'}:
                continue
            for part in raw_params.split(';'):
                k, sep, v = part.partition('=')
                if not sep or k.upper() not in {'TZID', 'VALUE'} or k.upper() in params:
                    raise CalendarError('Unsupported calendar event')
                params[k.upper()] = v
        name = name.upper()
        if name in recurring and name not in allowed:
            has_recurrence = True
            continue
        if name in ignored and name not in allowed:
            continue
        if name not in allowed and (name.startswith('X-') or re.fullmatch(r'[A-Z][A-Z0-9-]{1,63}', name)):
            continue
        if name not in allowed or name in event:
            raise CalendarError('Unsupported calendar event')
        event[name] = (value, params) if name in {'DTSTART', 'DTEND'} else value
    if not {'UID', 'DTSTART'} <= set(event) or ('DTEND' in event and 'DURATION' in event) or not ({'DTEND', 'DURATION'} & set(event)):
        raise CalendarError('Invalid calendar event')
    start_dt, all_day = _ical_datetime(*event['DTSTART'])
    end_dt = _ical_datetime(*event['DTEND'])[0] if 'DTEND' in event else start_dt + _duration(event['DURATION'])
    if end_dt <= start_dt:
        raise CalendarError('Invalid calendar event')
    result = {'uid': event['UID'][:255], 'start': start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'end': end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'summary': _unescape(event.get('SUMMARY', ''))}
    if all_day:
        result['all_day'] = True
    if has_recurrence:
        result['recurring'] = True
    for key in ('DESCRIPTION', 'LOCATION'):
        if key in event:
            result[key.lower()] = _unescape(event[key])
    return result


def _ical_datetime(value, params):
    """Convert a supported iCalendar date or date-time to UTC."""
    value_type = params.get('VALUE', 'DATE-TIME')
    tzid = params.get('TZID')
    if value_type == 'DATE':
        if tzid or not re.fullmatch(r'\d{8}', value):
            raise CalendarError('Unsupported event date')
        try:
            return datetime.strptime(value, '%Y%m%d').replace(tzinfo=timezone.utc), True
        except ValueError:
            raise CalendarError('Invalid calendar event') from None
    if value_type != 'DATE-TIME' or not re.fullmatch(r'\d{8}T\d{6}Z?', value):
        raise CalendarError('Unsupported event date')
    if value.endswith('Z'):
        if tzid:
            raise CalendarError('Unsupported event timezone')
        try:
            return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc), False
        except ValueError:
            raise CalendarError('Invalid calendar event') from None
    if not tzid or not re.fullmatch(r'[A-Za-z0-9_+\-/]{1,64}', tzid):
        raise CalendarError('Unsupported floating event time')
    try:
        zone = ZoneInfo(tzid)
    except Exception:
        raise CalendarError('Unsupported event timezone') from None
    try:
        return datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=zone).astimezone(timezone.utc), False
    except ValueError:
        raise CalendarError('Invalid calendar event') from None


def _duration(value):
    match = re.fullmatch(r'P(?:(\d{1,4})W)?(?:(\d{1,4})D)?(?:T(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})S)?)?', value or '')
    if not match or not any(match.groups()):
        raise CalendarError('Unsupported event duration')
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    if delta <= timedelta(0) or delta > timedelta(days=400):
        raise CalendarError('Unsupported event duration')
    return delta


def serialize_ics(fields, uid=None):
    summary = _text(fields.get('summary'), required=True)
    start, end = _timestamp(fields.get('start')), _timestamp(fields.get('end'))
    if end <= start:
        raise CalendarError('Event end must be after start')
    uid = uid or str(uuid.uuid4())
    if not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', uid):
        raise CalendarError('Unsupported event UID')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//icloud-mail-readonly-mcp//EN', 'BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'), 'DTSTART:' + _ical_time(fields['start']), 'DTEND:' + _ical_time(fields['end']), 'SUMMARY:' + _escape(summary)]
    for name, key in (('DESCRIPTION', 'description'), ('LOCATION', 'location')):
        if key in fields:
            lines.append(name + ':' + _escape(_text(fields[key])))
    lines.extend(['END:VEVENT', 'END:VCALENDAR'])
    return ('\r\n'.join(lines) + '\r\n').encode('utf-8')


class CalDAVTransport:
    def __init__(self, username, password):
        if any(not isinstance(v, str) or not v or len(v) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in (username, password)):
            raise CalendarError('Missing or invalid iCloud credentials')
        self.auth = base64.b64encode((username + ':' + password).encode('ascii')).decode('ascii')

    def request(self, host, method, target, headers=None, body=b''):
        if host != host.lower() or not HOST_RE.fullmatch(host) or _clean_path(target) != target or method not in {'PROPFIND', 'REPORT', 'GET', 'PUT', 'DELETE'} or len(body) > MAX_ICS:
            raise CalendarError('Invalid calendar request')
        conn = http.client.HTTPSConnection(host, PORT, timeout=TIMEOUT, context=ssl.create_default_context())
        try:
            sent = {'Authorization': 'Basic ' + self.auth, 'User-Agent': 'icloud-mail-readonly-mcp/0.2', **(headers or {})}
            conn.request(method, target, body=body, headers=sent)
            response = conn.getresponse()
            chunks, total, deadline = [], 0, time.monotonic() + TIMEOUT
            while True:
                if time.monotonic() > deadline:
                    raise CalendarError('Calendar response timed out')
                chunk = response.read(min(8192, MAX_RESPONSE - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE:
                    raise CalendarError('Calendar response exceeds limits')
                chunks.append(chunk)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, b''.join(chunks)
        except CalendarError:
            raise
        except Exception:
            raise CalendarError('Calendar request failed; check credentials, connection and server limits') from None
        finally:
            conn.close()


class CalendarClient:
    def __init__(self, username, password, transport=None):
        self.transport = transport or CalDAVTransport(username, password)
        self.host = ROOT_HOST
        self.principal = None
        self.home = None

    def _request(self, method, path, headers=None, body=b'', discovery=False):
        path = _clean_path(path)
        status, response_headers, data = self.transport.request(self.host, method, path, headers, body) if isinstance(self.transport, CalDAVTransport) else self.transport.request(method, path, headers, body)
        if status in (301, 302, 307, 308):
            if not discovery or not isinstance(response_headers.get('location'), str):
                raise CalendarError('Calendar redirects are refused')
            host, target = validate_endpoint(response_headers['location'])
            if host == self.host:
                raise CalendarError('Calendar redirect refused')
            self.host = host
            status, response_headers, data = self.transport.request(self.host, method, target, headers, body) if isinstance(self.transport, CalDAVTransport) else self.transport.request(method, target, headers, body)
            if status in (301, 302, 307, 308):
                raise CalendarError('Calendar redirect refused')
        return status, response_headers, data

    def _xml(self, data):
        if len(data) > MAX_RESPONSE or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise CalendarError('Unsafe calendar XML response')
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise CalendarError('Invalid calendar XML response') from None
        if sum(1 for _ in root.iter()) > MAX_XML_NODES:
            raise CalendarError('Calendar XML response exceeds limits')
        return root

    def _href(self, href):
        return _href_path(href, self.host)

    def _discover_href(self, href):
        """Accept an allowlisted Apple href during discovery and adopt its shard host."""
        if not isinstance(href, str) or len(href) > 1536:
            raise CalendarError('Invalid server calendar resource')
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            host, path = validate_endpoint(href)
            self.host = host
            return path
        if parsed.query or parsed.fragment:
            raise CalendarError('Invalid server calendar resource')
        return _clean_path(parsed.path)

    def _discover(self):
        if self.home:
            return
        status, _, data = self._request('PROPFIND', '/', {'Depth': '0', 'Content-Type': 'application/xml'}, b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>', True)
        if status != 207:
            raise CalendarError('Calendar discovery failed')
        root = self._xml(data)
        href = root.findtext('.//{DAV:}current-user-principal/{DAV:}href')
        self.principal = self._discover_href(href)
        status, _, data = self._request('PROPFIND', self.principal, {'Depth': '0', 'Content-Type': 'application/xml'}, b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>', True)
        if status != 207:
            raise CalendarError('Calendar discovery failed')
        self.home = self._discover_href(self._xml(data).findtext('.//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href'))
        if not self.home.endswith('/'):
            self.home += '/'

    def list_calendars(self):
        self._discover()
        status, _, data = self._request('PROPFIND', self.home, {'Depth': '1', 'Content-Type': 'application/xml'}, b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>')
        if status != 207:
            raise CalendarError('Calendar listing failed')
        calendars = []
        for response in self._xml(data).findall('{DAV:}response'):
            resource = response.find('.//{DAV:}resourcetype')
            href = response.findtext('{DAV:}href')
            if resource is None or resource.find('{urn:ietf:params:xml:ns:caldav}calendar') is None or href is None:
                continue
            path = _within(self._href(href), self.home)
            name = response.findtext('.//{DAV:}displayname') or ''
            calendars.append({'calendar_id': encode_id(path), 'name': name[:512]})
        if len(calendars) > MAX_CALENDARS:
            raise CalendarError('Too many calendars')
        return {'calendars': calendars}

    def _calendar_path(self, calendar_id):
        self._discover()
        path = _within(decode_id(calendar_id), self.home)
        return path if path.endswith('/') else path + '/'

    def _event_path(self, calendar_path, event_id):
        return _within(decode_id(event_id), calendar_path, event=True)

    def list_events(self, calendar_id, start, end):
        calendar_path = self._calendar_path(calendar_id)
        if _timestamp(end) <= _timestamp(start):
            raise CalendarError('Event end must be after start')
        body = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="' + _ical_time(start) + '" end="' + _ical_time(end) + '"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>').encode()
        status, _, data = self._request('REPORT', calendar_path, {'Depth': '1', 'Content-Type': 'application/xml'}, body)
        if status != 207:
            raise CalendarError('Event listing failed')
        events, skipped, reasons = [], 0, {}
        for response in self._xml(data).findall('{DAV:}response'):
            href, etag = response.findtext('{DAV:}href'), response.findtext('.//{DAV:}getetag')
            raw = response.findtext('.//{urn:ietf:params:xml:ns:caldav}calendar-data')
            if href is None or etag is None or raw is None:
                continue
            path = self._event_path(calendar_path, encode_id(self._href(href)))
            try:
                event = parse_ics(raw.encode('utf-8'))
                etag = _strong_etag(etag)
            except CalendarError as exc:
                skipped += 1
                reasons[str(exc)] = reasons.get(str(exc), 0) + 1
                continue
            events.append({'event_id': encode_id(path), 'etag': etag, **event})
        if len(events) > MAX_EVENTS:
            raise CalendarError('Too many events')
        return {'events': events, **({'skipped': skipped, 'skipped_reasons': reasons} if skipped else {})}

    def get_event(self, calendar_id, event_id):
        path = self._event_path(self._calendar_path(calendar_id), event_id)
        status, headers, data = self._request('GET', path)
        if status != 200:
            raise CalendarError('Event fetch failed')
        return {'event_id': encode_id(path), 'etag': _strong_etag(headers.get('etag')), **parse_ics(data)}

    def create_event(self, calendar_id, fields):
        path = self._calendar_path(calendar_id) + str(uuid.uuid4()) + '.ics'
        status, headers, _ = self._request('PUT', path, {'Content-Type': 'text/calendar; charset=utf-8', 'If-None-Match': '*'}, serialize_ics(fields))
        if status not in (200, 201, 204):
            raise CalendarError('Event creation failed')
        return {'event_id': encode_id(path), 'etag': _strong_etag(headers.get('etag'))}

    def update_event(self, calendar_id, event_id, etag, fields):
        path = self._event_path(self._calendar_path(calendar_id), event_id)
        etag = _strong_etag(etag)
        status, headers, old = self._request('GET', path)
        if status != 200 or _strong_etag(headers.get('etag')) != etag:
            raise CalendarError('Event changed; refresh and retry')
        old_event = parse_ics(old)
        body = serialize_ics(fields, old_event['uid'])
        status, headers, _ = self._request('PUT', path, {'Content-Type': 'text/calendar; charset=utf-8', 'If-Match': etag}, body)
        if status not in (200, 201, 204):
            raise CalendarError('Event update failed; refresh and retry')
        return {'event_id': encode_id(path), 'etag': _strong_etag(headers.get('etag'))}

    def delete_event(self, calendar_id, event_id, etag):
        path = self._event_path(self._calendar_path(calendar_id), event_id)
        status, _, _ = self._request('DELETE', path, {'If-Match': _strong_etag(etag)})
        if status not in (200, 202, 204):
            raise CalendarError('Event deletion failed; refresh and retry')
        return {'deleted': True}


def run(operation, args, username, password):
    client = CalendarClient(username, password)
    if operation == 'list_calendars':
        return client.list_calendars()
    if operation == 'list_events':
        return client.list_events(args.get('calendar_id'), args.get('start'), args.get('end'))
    if operation == 'get_event':
        return client.get_event(args.get('calendar_id'), args.get('event_id'))
    if operation == 'create_event':
        return client.create_event(args.get('calendar_id'), args)
    if operation == 'update_event':
        return client.update_event(args.get('calendar_id'), args.get('event_id'), args.get('etag'), args)
    if operation == 'delete_event':
        return client.delete_event(args.get('calendar_id'), args.get('event_id'), args.get('etag'))
    raise CalendarError('Unknown operation')
