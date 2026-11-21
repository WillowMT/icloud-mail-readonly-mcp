import base64
import unittest
from unittest import mock

from icloud_mail_readonly import calendar


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.host = 'caldav.icloud.com'
        self.principal = '/principals/u/'
        self.home = '/calendars/u/'
        self.calendar = '/calendars/u/work/'
        self.event = '/calendars/u/work/one.ics'

    def request(self, method, target, headers=None, body=b''):
        self.calls.append((method, target, headers or {}, body))
        if target == '/' and method == 'PROPFIND':
            return 207, {}, b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/principals/u/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'''
        if target == self.principal:
            return 207, {}, b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop><c:calendar-home-set><d:href>/calendars/u/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>'''
        if target == self.home:
            return 207, {}, b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/calendars/u/work/</d:href><d:propstat><d:prop><d:displayname>Work</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>'''
        if target == self.calendar and method == 'REPORT':
            return 207, {}, (b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/calendars/u/work/one.ics</d:href><d:propstat><d:prop><d:getetag>"v1"</d:getetag><c:calendar-data>BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:one\r\nDTSTART:20270101T100000Z\r\nDTEND:20270101T110000Z\r\nSUMMARY:Hello\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n</c:calendar-data></d:prop></d:propstat></d:response><d:response><d:href>/calendars/u/work/two.ics</d:href><d:propstat><d:prop><d:getetag>"v1"</d:getetag><c:calendar-data>BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:two\r\nRRULE:FREQ=DAILY\r\nDTSTART:20270101T100000Z\r\nDTEND:20270101T110000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>''')
        if target == self.event and method == 'GET':
            return 200, {'etag': '"v1"'}, b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:one\r\nDTSTART:20270101T100000Z\r\nDTEND:20270101T110000Z\r\nSUMMARY:Hello\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        if target == self.event and method in ('PUT', 'DELETE'):
            return (204 if method == 'DELETE' else 201), {'etag': '"v2"'}, b''
        if method == 'PUT' and target.startswith(self.calendar) and target.endswith('.ics') and target != self.event:
            return 201, {'etag': '"v2"'}, b''
        raise AssertionError((method, target))


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = calendar.CalendarClient('fake@example.invalid', 'fake-app-password', transport=self.transport)

    def test_calendar_and_event_workflow_uses_constrained_paths_and_etags(self):
        calendars = self.client.list_calendars()
        self.assertEqual(calendars['calendars'][0]['name'], 'Work')
        cid = calendars['calendars'][0]['calendar_id']
        events = self.client.list_events(cid, '2027-01-01T00:00:00Z', '2027-01-02T00:00:00Z')
        self.assertEqual(events['events'][0]['summary'], 'Hello')
        eid = events['events'][0]['event_id']
        got = self.client.get_event(cid, eid)
        self.assertEqual(got['etag'], '"v1"')
        created = self.client.create_event(cid, {'summary': 'A\r\nInjected', 'start': '2027-01-03T10:00:00Z', 'end': '2027-01-03T11:00:00Z'})
        self.assertEqual(created['etag'], '"v2"')
        self.client.update_event(cid, eid, '"v1"', {'summary': 'Updated', 'start': '2027-01-01T10:00:00Z', 'end': '2027-01-01T11:00:00Z'})
        self.client.delete_event(cid, eid, '"v1"')
        puts = [x for x in self.transport.calls if x[0] == 'PUT']
        self.assertEqual(puts[0][2]['If-None-Match'], '*')
        self.assertNotIn(b'\r\nInjected\r\n', puts[0][3])
        self.assertEqual(puts[1][2]['If-Match'], '"v1"')
        self.assertEqual([x for x in self.transport.calls if x[0] == 'DELETE'][0][2]['If-Match'], '"v1"')

    def test_rejects_hostile_hrefs_identifiers_weak_etags_and_complex_events(self):
        with self.assertRaises(calendar.CalendarError):
            self.client._href('https://evil.invalid/steal')
        with self.assertRaises(calendar.CalendarError):
            self.client.list_events('Li4v', '2027-01-01T00:00:00Z', '2027-01-02T00:00:00Z')
        with self.assertRaises(calendar.CalendarError):
            self.client.delete_event(calendar.encode_id('/calendars/u/work/'), calendar.encode_id('/calendars/u/work/one.ics'), 'W/"v1"')
        bad = b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        with self.assertRaises(calendar.CalendarError):
            calendar.parse_ics(bad)

    def test_transport_refuses_redirects_and_untrusted_shards_before_following(self):
        with self.assertRaises(calendar.CalendarError):
            calendar.validate_endpoint('https://evil.invalid/x')
        with self.assertRaises(calendar.CalendarError):
            calendar.validate_endpoint('http://caldav.icloud.com/')
        self.assertEqual(calendar.validate_endpoint('https://p12-caldav.icloud.com/x'), ('p12-caldav.icloud.com', '/x'))

    def test_tzid_and_all_day_events_are_converted_to_utc(self):
        tzid = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:z\r\n'
                b'DTSTART;TZID=Asia/Bangkok:20270101T170000\r\nDTEND;TZID=Asia/Bangkok:20270101T180000\r\n'
                b'SUMMARY:Meet\\, chat\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        event = calendar.parse_ics(tzid)
        self.assertEqual(event['start'], '2027-01-01T10:00:00Z')
        self.assertEqual(event['end'], '2027-01-01T11:00:00Z')
        self.assertEqual(event['summary'], 'Meet, chat')
        self.assertNotIn('all_day', event)
        allday = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:d\r\n'
                  b'DTSTART;VALUE=DATE:20270105\r\nDTEND;VALUE=DATE:20270106\r\nSUMMARY:Off\r\n'
                  b'END:VEVENT\r\nEND:VCALENDAR\r\n')
        self.assertTrue(calendar.parse_ics(allday)['all_day'])

    def test_floating_and_unknown_timezones_are_rejected(self):
        floating = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:f\r\n'
                    b'DTSTART:20270101T100000\r\nDTEND:20270101T110000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        with self.assertRaises(calendar.CalendarError):
            calendar.parse_ics(floating)
        unknown = floating.replace(b'DTSTART:', b'DTSTART;TZID=Nowhere/Nope:')
        with self.assertRaises(calendar.CalendarError):
            calendar.parse_ics(unknown)

    def test_list_events_skips_unsupported_items_and_reports_count(self):
        result = self.client.list_events(calendar.encode_id('/calendars/u/work/'), '2027-01-01T00:00:00Z', '2027-01-02T00:00:00Z')
        self.assertEqual(len(result['events']), 2)
        self.assertNotIn('skipped', result)

    def test_recurring_event_is_shown_once_and_marked(self):
        ics = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:r\r\nRRULE:FREQ=WEEKLY\r\n'
               b'DTSTART:20270101T100000Z\r\nDTEND:20270101T110000Z\r\nSEQUENCE:3\r\nTRANSP:OPAQUE\r\n'
               b'STATUS:CONFIRMED\r\nORGANIZER;CN=Willow:mailto:w@example.invalid\r\n'
               b'ATTENDEE;CN=A:mailto:a@example.invalid\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        event = calendar.parse_ics(ics)
        self.assertEqual(event['summary'], 'Standup')
        self.assertTrue(event['recurring'])
        self.assertEqual(set(event), {'uid', 'start', 'end', 'summary', 'recurring'})


class ShardDiscoveryTests(unittest.TestCase):
    def test_discovery_adopts_allowlisted_shard_host(self):
        class Transport:
            def request(self, method, target, headers=None, body=b''):
                if target == '/':
                    return 207, {}, b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>https://p12-caldav.icloud.com/1/principal/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'
                if target == '/1/principal/':
                    return 207, {}, b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/1/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>https://p12-caldav.icloud.com/1/calendars/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>'
                raise AssertionError((method, target))
        client = calendar.CalendarClient('u', 'p', Transport())
        client._discover()
        self.assertEqual(client.host, 'p12-caldav.icloud.com')
        self.assertEqual(client.home, '/1/calendars/')

    def test_discovery_rejects_untrusted_absolute_href(self):
        class Transport:
            def request(self, method, target, headers=None, body=b''):
                return 207, {}, b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>https://evil.invalid/p/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'
        with self.assertRaises(calendar.CalendarError):
            calendar.CalendarClient('u', 'p', Transport())._discover()


if __name__ == '__main__':
    unittest.main()
