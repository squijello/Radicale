# This file is part of Radicale - CalDAV and CardDAV server
# Copyright © 2012-2017 Guillaume Ayoub
# Copyright © 2017-2019 Unrud <unrud@outlook.com>
# Copyright © 2024 Pieter Hijma <pieterhijma@users.noreply.github.com>
# Copyright © 2025 David Greaves <david@dgreaves.com>
# Copyright © 2025 Peter Bieringer <pb@bieringer.de>
# Copyright © 2026-2026 Contributors to Radicale
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Radicale.  If not, see <http://www.gnu.org/licenses/>.

"""
Radicale tests with expand requests.

"""

import os
from typing import ClassVar, List, Optional
from xml.etree import ElementTree

import pytest
import vobject

from radicale.log import logger
from radicale.tests import BaseTest
from radicale.tests.helpers import get_file_content

ONLY_DATES = True
CONTAINS_TIMES = False


class TestExpandRequests(BaseTest):
    """Tests with expand requests."""

    # Allow skipping sync-token tests, when not fully supported by the backend
    full_sync_token_support: ClassVar[bool] = True

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("""\
[permit delete collection]
user: .*
collection: test-permit-delete
permissions: RrWwD

[forbid delete collection]
user: .*
collection: test-forbid-delete
permissions: RrWwd

[permit overwrite collection]
user: .*
collection: test-permit-overwrite
permissions: RrWwO

[forbid overwrite collection]
user: .*
collection: test-forbid-overwrite
permissions: RrWwo

[allow all]
user: .*
collection: .*
permissions: RrWw""")
        self.configure({"rights": {"file": rights_file_path,
                                   "type": "from_file"}})

    def _req_without_expand(self,
                            expected_uid: str,
                            start: str,
                            end: str,
                            ) -> str:
        self.put("/calendar.ics/", get_file_content(f"{expected_uid}.ics"))
        return \
            f"""<?xml version="1.0" encoding="utf-8" ?>
            <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
                <D:prop>
                    <C:calendar-data>
                    </C:calendar-data>
                </D:prop>
                <C:filter>
                    <C:comp-filter name="VCALENDAR">
                        <C:comp-filter name="VEVENT">
                            <C:time-range start="{start}" end="{end}"/>
                        </C:comp-filter>
                    </C:comp-filter>
                </C:filter>
            </C:calendar-query>
            """

    def _req_with_expand(self,
                         expected_uid: str,
                         start: str,
                         end: str,
                         ) -> str:
        self.put("/calendar.ics/", get_file_content(f"{expected_uid}.ics"))
        return \
            f"""<?xml version="1.0" encoding="utf-8" ?>
            <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
                <D:prop>
                    <C:calendar-data>
                        <C:expand start="{start}" end="{end}"/>
                    </C:calendar-data>
                </D:prop>
                <C:filter>
                    <C:comp-filter name="VCALENDAR">
                        <C:comp-filter name="VEVENT">
                            <C:time-range start="{start}" end="{end}"/>
                        </C:comp-filter>
                    </C:comp-filter>
                </C:filter>
            </C:calendar-query>
            """

    def _test_expand(self,
                     expected_uid: str,
                     start: str,
                     end: str,
                     expected_recurrence_ids: List[str],
                     expected_start_times: List[str],
                     expected_end_times: List[str],
                     only_dates: bool,
                     nr_uids: int,
                     check: int = 207) -> None:
        _, responses = self.report("/calendar.ics/",
                                   self._req_without_expand(expected_uid, start, end))
        assert len(responses) == 1
        response_without_expand = responses[f'/calendar.ics/{expected_uid}.ics']
        assert isinstance(response_without_expand, dict)
        status, element = response_without_expand["C:calendar-data"]

        assert status == 200 and element.text

        assert "RRULE" in element.text
        if not only_dates:
            assert "BEGIN:VTIMEZONE" in element.text
        if nr_uids == 1:
            assert "RECURRENCE-ID" not in element.text

        uids: List[str] = []
        for line in element.text.split("\n"):
            if line.startswith("UID:"):
                uid = line[len("UID:"):]
                assert uid == expected_uid
                uids.append(uid)

        assert len(uids) == nr_uids

        _, responses = self.report("/calendar.ics/",
                                   self._req_with_expand(expected_uid, start, end), check=check)
        if check != 207:
            return

        assert len(responses) == 1

        response_with_expand = responses[f'/calendar.ics/{expected_uid}.ics']
        assert isinstance(response_with_expand, dict)
        status, element = response_with_expand["C:calendar-data"]

        logger.debug("lbt: element is %s",
                     ElementTree.tostring(element, encoding='unicode'))
        assert status == 200 and element.text
        assert "RRULE" not in element.text
        assert "BEGIN:VTIMEZONE" not in element.text

        uids = []
        recurrence_ids = []
        for line in element.text.split("\n"):
            if line.startswith("UID:"):
                assert line == f"UID:{expected_uid}"
                uids.append(line)

            if line.startswith("RECURRENCE-ID:"):
                if expected_recurrence_ids:
                    assert line in expected_recurrence_ids
                recurrence_ids.append(line)

            if line.startswith("DTSTART:"):
                if expected_start_times:
                    assert line in expected_start_times

            if line.startswith("DTEND:"):
                if expected_end_times:
                    assert line in expected_end_times

        if expected_recurrence_ids:
            assert len(uids) == len(expected_recurrence_ids)
            assert len(set(recurrence_ids)) == len(expected_recurrence_ids)

    def _test_expand_max(self,
                         expected_uid: str,
                         start: str,
                         end: str,
                         check: Optional[int] = None) -> None:
        _, responses = self.report("/calendar.ics/",
                                   self._req_without_expand(expected_uid, start, end))
        assert len(responses) == 1
        response_without_expand = responses[f'/calendar.ics/{expected_uid}.ics']
        assert isinstance(response_without_expand, dict)
        status, element = response_without_expand["C:calendar-data"]

        assert status == 200 and element.text

        assert "RRULE" in element.text

        status, _, _ = self.request(
            "REPORT", "/calendar.ics/",
            self._req_with_expand(expected_uid, start, end),
            check=check)

        assert status == 400

    def test_report_with_expand_property(self) -> None:
        """Test report with expand property"""
        self._test_expand(
            "event_daily_rrule",
            "20060103T000000Z",
            "20060105T000000Z",
            ["RECURRENCE-ID:20060103T170000Z", "RECURRENCE-ID:20060104T170000Z"],
            ["DTSTART:20060103T170000Z", "DTSTART:20060104T170000Z"],
            [],
            CONTAINS_TIMES,
            1
        )

    def test_report_with_expand_property_start_inside(self) -> None:
        """Test report with expand property start inside"""
        self._test_expand(
            "event_daily_rrule",
            "20060103T171500Z",
            "20060105T000000Z",
            ["RECURRENCE-ID:20060103T170000Z", "RECURRENCE-ID:20060104T170000Z"],
            ["DTSTART:20060103T170000Z", "DTSTART:20060104T170000Z"],
            [],
            CONTAINS_TIMES,
            1
        )

    def test_report_with_expand_property_just_inside(self) -> None:
        """Test report with expand property start and end inside event"""
        self._test_expand(
            "event_daily_rrule",
            "20060103T171500Z",
            "20060103T171501Z",
            ["RECURRENCE-ID:20060103T170000Z"],
            ["DTSTART:20060103T170000Z"],
            [],
            CONTAINS_TIMES,
            1
        )

    def test_report_with_expand_property_issue1812(self) -> None:
        """Test report with expand property for issue 1812"""
        self._test_expand(
            "event_issue1812",
            "20250127T183000Z",
            "20250127T183001Z",
            ["RECURRENCE-ID:20250127T180000Z"],
            ["DTSTART:20250127T180000Z"],
            ["DTEND:20250127T233000Z"],
            CONTAINS_TIMES,
            11
        )

    def test_report_with_expand_property_issue1812_DS(self) -> None:
        """Test report with expand property for issue 1812 - DS active"""
        self._test_expand(
            "event_issue1812",
            "20250627T183000Z",
            "20250627T183001Z",
            ["RECURRENCE-ID:20250627T170000Z"],
            ["DTSTART:20250627T170000Z"],
            ["DTEND:20250627T223000Z"],
            CONTAINS_TIMES,
            11
        )

    def test_report_with_expand_property_all_day_event(self) -> None:
        """Test report with expand property for all day events"""
        self._test_expand(
            "event_full_day_rrule",
            "20060103T000000Z",
            "20060105T000000Z",
            ["RECURRENCE-ID:20060103", "RECURRENCE-ID:20060104", "RECURRENCE-ID:20060105"],
            ["DTSTART:20060103", "DTSTART:20060104", "DTSTART:20060105"],
            ["DTEND:20060104", "DTEND:20060105", "DTEND:20060106"],
            ONLY_DATES,
            1
        )

    def test_report_with_expand_property_all_day_count_500_event_pass(self) -> None:
        """Test report with expand property for all day count 500 events"""
        self.configure({"reporting": {"max_expand_occurrence": 501}})
        self._test_expand(
            "event_full_day_rrule_count_500",
            "20060103T000000Z",
            "20080105T000000Z",
            [],
            [],
            [],
            ONLY_DATES,
            1
        )

    def test_report_with_expand_property_all_day_count_500_event_reject(self) -> None:
        """Test report with expand property for all day count 500 events"""
        self.configure({"reporting": {"max_expand_occurrence": 10}})
        self._test_expand(
            "event_full_day_rrule_count_500",
            "20060103T000000Z",
            "20080105T000000Z",
            [],
            [],
            [],
            ONLY_DATES,
            1,
            400
        )

    def test_report_with_expand_property_overridden(self) -> None:
        """Test report with expand property with overridden events"""
        self._test_expand(
            "event_daily_rrule_overridden",
            "20060103T000000Z",
            "20060105T000000Z",
            ["RECURRENCE-ID:20060103T170000Z", "RECURRENCE-ID:20060104T170000Z"],
            ["DTSTART:20060103T170000Z", "DTSTART:20060104T190000Z"],
            [],
            CONTAINS_TIMES,
            2
        )

    @staticmethod
    def _local_timezone_source(tzid, daylight, uid="local-zone"):
        zone = ["BEGIN:VTIMEZONE", "TZID:" + tzid,
                "BEGIN:STANDARD", "DTSTART:19701101T020000",
                "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
                "TZOFFSETFROM:" + ("-0400" if daylight else "-0500"),
                "TZOFFSETTO:-0500", "END:STANDARD"]
        if daylight:
            zone += ["BEGIN:DAYLIGHT", "DTSTART:19700308T020000",
                     "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
                     "TZOFFSETFROM:-0500", "TZOFFSETTO:-0400", "END:DAYLIGHT"]
        return "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN", *zone,
            "END:VTIMEZONE", "BEGIN:VEVENT", "UID:" + uid,
            "DTSTAMP:20261117T120000Z", "DTSTART;TZID=" + tzid + ":20270115T100000",
            "DTEND;TZID=" + tzid + ":20270115T110000",
            "RRULE:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=8", "END:VEVENT", "END:VCALENDAR", ""])

    @staticmethod
    def _local_timezone_query(uid=None, start="20270815T000000Z", end="20270816T000000Z", expand=True):
        data = '<C:calendar-data><C:expand start="%s" end="%s"/></C:calendar-data>' % (start, end)
        if not expand:
            data = "<C:calendar-data/>"
        uid_filter = ('<C:prop-filter name="UID"><C:text-match>%s</C:text-match></C:prop-filter>' % uid
                      if uid else "")
        return '''<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
          <D:prop><D:getetag/>%s</D:prop><C:filter><C:comp-filter name="VCALENDAR">
            <C:comp-filter name="VEVENT"><C:time-range start="%s" end="%s"/>%s</C:comp-filter>
          </C:comp-filter></C:filter></C:calendar-query>''' % (data, start, end, uid_filter)

    @staticmethod
    def _local_timezone_starts(responses):
        starts = []
        for response in responses.values():
            assert isinstance(response, dict)
            status, data = response["C:calendar-data"]
            assert status == 200 and data.text
            starts.extend(event.dtstart.value.strftime("%Y%m%dT%H%M%SZ")
                          for event in vobject.readOne(data.text).vevent_list)
        return starts

    @pytest.mark.parametrize("mtime_cache", [False, True])
    def test_timezone_rule_edit_updates_live_expansion_and_plain_queries(self, mtime_cache):
        from datetime import datetime, timezone
        from io import StringIO
        from uuid import uuid4

        from dateutil.tz import tzical

        self.configure({"storage": {"use_mtime_and_size_for_item_cache": str(mtime_cache)}})
        self.mkcalendar("/test/")
        path = "/test/zone.ics"
        tzid = "Test/Local-" + uuid4().hex
        source = self._local_timezone_source(tzid, True)
        self.put(path, source)
        _, headers, _ = self.request("GET", path, check=200)
        _, responses = self.report("/test/", self._local_timezone_query("local-zone"))
        assert self._local_timezone_starts(responses) == ["20270815T140000Z"]
        updated = self._local_timezone_source(tzid, False)
        self.request("PUT", path, updated, check=204, http_if_match=headers["ETag"])
        _, headers_after, stored = self.request("GET", path, check=200)
        declared = tzical(StringIO(stored)).get(tzid)
        expected = datetime(2027, 8, 15, 10, tzinfo=declared).astimezone(timezone.utc)
        assert expected.strftime("%Y%m%dT%H%M%SZ") == "20270815T150000Z"
        assert headers_after["ETag"] != headers["ETag"]
        for _ in range(2):
            _, responses = self.report("/test/", self._local_timezone_query("local-zone"))
            assert self._local_timezone_starts(responses) == [expected.strftime("%Y%m%dT%H%M%SZ")]
            _, plain = self.report("/test/", self._local_timezone_query(
                start="20270815T153000Z", end="20270815T154500Z", expand=False))
            assert list(plain) == [path]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_timezone_definitions_are_independent_per_resource(self, reverse):
        from uuid import uuid4

        self.mkcalendar("/test/")
        tzid = "Test/Resources-" + uuid4().hex
        definitions = [("seasonal", True, "20270815T140000Z"), ("fixed", False, "20270815T150000Z")]
        if reverse:
            definitions.reverse()
        for uid, daylight, _ in definitions:
            self.put("/test/%s.ics" % uid, self._local_timezone_source(tzid, daylight, uid))
        for uid, _, expected in definitions + definitions[::-1]:
            _, responses = self.report("/test/", self._local_timezone_query(uid))
            assert self._local_timezone_starts(responses) == [expected]

    @pytest.mark.parametrize("entrypoint", ["stream", "lazy", "native"])
    def test_timezone_scope_survives_serialization_and_concurrent_parsing(self, entrypoint):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Concurrent-" + uuid4().hex
        sources = [self._local_timezone_source(tzid, daylight) for daylight in (True, False)]
        vobject.readOne(sources[0])  # Warm the dependency's first-seen registry.

        def check(index):
            source = sources[index % 2]
            if entrypoint == "stream":
                calendar, = radicale_item.read_components(source)
            elif entrypoint == "lazy":
                calendar = radicale_item.Item(collection_path="test", text=source).vobject_item
            else:
                calendar = radicale_item.Item(collection_path="test", vobject_item=vobject.readOne(source)).vobject_item
            calendar.serialize()
            zone = calendar.vevent.dtstart.value.tzinfo
            return datetime(2027, 8, 15, 10, tzinfo=zone).astimezone(timezone.utc).hour

        with ThreadPoolExecutor(max_workers=8) as pool:
            actual = list(pool.map(check, range(32)))
        assert actual == [14, 15] * 16
        first, second = radicale_item.read_components("".join(sources))
        assert first.vevent.dtstart.value.tzinfo is not second.vevent.dtstart.value.tzinfo

    @pytest.mark.parametrize("kind", ["rdate", "exdate", "override"])
    def test_timezone_scope_includes_date_lists_and_overrides(self, kind):
        from uuid import uuid4

        from radicale import item as radicale_item

        self.mkcalendar("/test/")
        tzid = "Test/DateLists-" + uuid4().hex
        radicale_item.read_components(self._local_timezone_source(tzid, True))
        source = self._local_timezone_source(tzid, False)
        if kind in ("rdate", "exdate"):
            source = source.replace("DTSTART;TZID=" + tzid + ":20270115T100000", "DTSTART:20270115T150000Z")
            source = source.replace("DTEND;TZID=" + tzid + ":20270115T110000", "DTEND:20270115T160000Z")
            day = "22" if kind == "rdate" else "15"
            source = source.replace("END:VEVENT", kind.upper() + ";TZID=" + tzid + ":202708" + day + "T100000\r\nEND:VEVENT")
        else:
            override = "\r\n".join([
                "BEGIN:VEVENT", "UID:local-zone", "DTSTAMP:20261117T120000Z",
                "RECURRENCE-ID;TZID=" + tzid + ":20270815T100000",
                "DTSTART;TZID=" + tzid + ":20270815T120000",
                "DTEND;TZID=" + tzid + ":20270815T130000", "END:VEVENT"])
            source = source.replace("END:VCALENDAR", override + "\r\nEND:VCALENDAR")
        self.put("/test/zone.ics", source)
        start, end = ("20270822T000000Z", "20270823T000000Z") if kind == "rdate" else ("20270815T000000Z", "20270816T000000Z")
        _, responses = self.report("/test/", self._local_timezone_query("local-zone", start, end))
        assert self._local_timezone_starts(responses) == {
            "rdate": ["20270822T150000Z"], "exdate": [], "override": ["20270815T170000Z"]}[kind]

    @pytest.mark.parametrize("kind", ["utc", "utc-with-tzid", "internal-param", "floating", "date"])
    def test_timezone_scope_preserves_utc_floating_and_date_values(self, kind):
        from datetime import date, datetime, timezone
        from io import StringIO
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Unzoned-" + uuid4().hex
        source = self._local_timezone_source(tzid, False)
        if kind in ("utc", "utc-with-tzid", "internal-param"):
            prefix = {"utc": "", "utc-with-tzid": ";TZID=" + tzid,
                      "internal-param": ";X-VOBJ-ORIGINAL-TZID=" + tzid}[kind]
            start = "DTSTART" + prefix + ":20270115T100000Z"
        elif kind == "date":
            start = "DTSTART;VALUE=DATE:20270115"
        else:
            start = "DTSTART:20270115T100000"
        source = source.replace("DTSTART;TZID=" + tzid + ":20270115T100000", start)
        calendar, = radicale_item.read_components(source)
        before = calendar.vevent.dtstart.value
        buffer = StringIO()
        calendar.serialize(buf=buffer, lineLength=40, validate=False)
        assert "BEGIN:VCALENDAR" in buffer.getvalue()
        assert calendar.vevent.dtstart.value == before
        if kind == "date":
            assert type(before) is date
        elif kind == "floating":
            assert before.tzinfo is None
        else:
            assert before == datetime(2027, 1, 15, 10, tzinfo=timezone.utc)

    @pytest.mark.parametrize("name", ["DTSTAMP", "CREATED", "LAST-MODIFIED"])
    def test_timezone_scope_keeps_force_utc_properties_in_utc(self, name):
        from datetime import datetime, timezone
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Stamp-" + uuid4().hex
        source = self._local_timezone_source(tzid, False)
        line = name + ";TZID=" + tzid + ":20261117T120000Z"
        if name == "DTSTAMP":
            source = source.replace("DTSTAMP:20261117T120000Z", line)
        else:
            source = source.replace("END:VEVENT", line + "\r\nEND:VEVENT")
        parsed = radicale_item.read_one(source)
        item = radicale_item.Item(collection_path="test", vobject_item=parsed)
        serialized = item.serialize()
        wire = vobject.readOne(serialized)
        stamp = getattr(wire.vevent, name.lower().replace("-", "_")).value
        assert stamp == datetime(2026, 11, 17, 12, tzinfo=timezone.utc)

    def test_timezone_scope_preserves_the_supplied_native_tree(self):
        from datetime import datetime
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Native-" + uuid4().hex
        vobject.readOne(self._local_timezone_source(tzid, True))
        supplied = vobject.readOne(self._local_timezone_source(tzid, False))
        zone = supplied.vtimezone.gettzinfo()
        supplied.vevent.dtstart.value = supplied.vevent.dtstart.value.replace(tzinfo=zone)
        supplied.vevent.dtend.value = supplied.vevent.dtend.value.replace(tzinfo=zone)
        item = radicale_item.Item(collection_path="test", vobject_item=supplied)
        item.serialize()
        assert supplied.vevent.dtstart.value.tzinfo is zone
        assert datetime(2027, 8, 15, tzinfo=zone).utcoffset().total_seconds() == -18000

    @pytest.mark.parametrize("reserved_id", [False, True])
    def test_timezone_rules_outside_library_utc_equivalence_window(self, reserved_id):
        from datetime import datetime, timezone
        from io import StringIO
        from uuid import uuid4

        from dateutil.tz import tzical

        from radicale import item as radicale_item

        tzid = "UTC" if reserved_id else "Test/Future-" + uuid4().hex
        source = self._local_timezone_source(tzid, False)
        source = source.replace("TZOFFSETFROM:-0500", "TZOFFSETFROM:+0000")
        source = source.replace("TZOFFSETTO:-0500", "TZOFFSETTO:+0000")
        source = source.replace("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n", "")
        transition = "\r\n".join(["BEGIN:STANDARD", "DTSTART:20250101T000000",
                                  "TZOFFSETFROM:+0000", "TZOFFSETTO:+0100", "END:STANDARD"])
        source = source.replace("END:VTIMEZONE", transition + "\r\nEND:VTIMEZONE")
        expected = datetime(2027, 8, 15, 10, tzinfo=tzical(StringIO(source)).get(tzid))
        assert expected.utcoffset().total_seconds() == 3600
        for parsed in (radicale_item.read_one(source), vobject.readOne(source)):
            item = radicale_item.Item(collection_path="test", vobject_item=parsed)
            text = item.serialize()
            assert "DTSTART;TZID=" + tzid + ":20270115T100000\r\n" in text
            assert item.vobject_item.vevent.dtstart.value.utcoffset().total_seconds() == 3600
        self.mkcalendar("/test/")
        self.put("/test/zone.ics", source)
        _, responses = self.report("/test/", self._local_timezone_query(
            start="20270815T093000Z", end="20270815T094500Z", expand=False))
        assert list(responses) == ["/test/zone.ics"]
        _, responses = self.report("/test/", self._local_timezone_query("local-zone"))
        assert self._local_timezone_starts(responses) == [expected.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")]

    @pytest.mark.parametrize("date_list", [False, True])
    def test_timezone_scope_handles_partially_native_components(self, date_list):
        from datetime import datetime
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Partial-" + uuid4().hex
        vobject.readOne(self._local_timezone_source(tzid, True))
        source = self._local_timezone_source(tzid, False)
        if date_list:
            source = source.replace("END:VEVENT", "RDATE;TZID=" + tzid + ":20270822T100000\r\nEND:VEVENT")
        partial = vobject.readOne(source, transform=False)
        partial.vevent.transformChildrenToNative()
        item = radicale_item.Item(collection_path="test", vobject_item=partial)
        calendar = item.vobject_item
        zone = calendar.vevent.dtstart.value.tzinfo
        assert datetime(2027, 8, 15, tzinfo=zone).utcoffset().total_seconds() == -18000
        if date_list:
            assert calendar.vevent.rdate.value[0].utcoffset().total_seconds() == -18000

    @pytest.mark.parametrize("scoped", [False, True])
    @pytest.mark.parametrize("other_offset", [None, 0, 2, "declared", "floating"])
    def test_timezone_scope_preserves_native_values_without_tzid_parameters(self, scoped, other_offset):
        from datetime import datetime, timedelta
        from uuid import uuid4

        from dateutil.tz import tzoffset, tzutc

        from radicale import item as radicale_item

        tzid = "Test/Implicit-" + uuid4().hex
        source = self._local_timezone_source(tzid, False)
        source = source.replace("TZOFFSETFROM:-0500", "TZOFFSETFROM:+0000")
        source = source.replace("TZOFFSETTO:-0500", "TZOFFSETTO:+0000")
        source = source.replace("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n", "")
        transition = "\r\n".join(["BEGIN:STANDARD", "DTSTART:20250101T000000",
                                  "TZOFFSETFROM:+0000", "TZOFFSETTO:+0100", "END:STANDARD"])
        source = source.replace("END:VTIMEZONE", transition + "\r\nEND:VTIMEZONE")
        calendar = radicale_item.read_one(source) if scoped else vobject.readOne(source, transform=False)
        zone = vobject.icalendar.TimezoneComponent.gettzinfo(calendar.vtimezone)
        if other_offset == "declared":
            other = vobject.readOne(self._local_timezone_source("Test/Other-" + uuid4().hex, False), transform=False)
            other.vtimezone.standard.tzoffsetfrom.value = "+0200"
            other.vtimezone.standard.tzoffsetto.value = "+0200"
            other_zone = vobject.icalendar.TimezoneComponent.gettzinfo(other.vtimezone)
            calendar.add(other.vtimezone)
        calendar.contents.pop("vevent")
        event = vobject.newFromBehavior("vevent")
        event.add("uid").value = "implicit-zone"
        event.add("dtstamp").value = datetime(2026, 11, 17, 12, tzinfo=tzutc())
        start = datetime(2027, 1, 15, 10, tzinfo=zone)
        event.add("dtstart").value = start
        event.add("dtend").value = start + timedelta(hours=1)
        if other_offset is not None:
            if other_offset == "floating":
                other_zone = None
            elif other_offset != "declared":
                other_zone = tzoffset(None, other_offset * 3600) if other_offset else tzutc()
            event.add("rdate").value = [start + timedelta(days=1), datetime(2027, 1, 17, 10, tzinfo=other_zone)]
        calendar.add(event)
        assert event.dtstart.params == {}
        item = radicale_item.Item(collection_path="test", vobject_item=calendar)
        assert item.vobject_item.vevent.dtstart.value == start
        assert item.vobject_item.vevent.dtstart.value.utcoffset() == timedelta(hours=1)
        text = item.serialize()
        assert "DTSTART;TZID=" + tzid + ":20270115T100000\r\n" in text
        if other_offset is not None:
            assert item.vobject_item.vevent.rdate.value == event.rdate.value
            expected_hour = 10 if other_offset == "floating" else (8 if other_offset == "declared" else 10 - other_offset)
            expected = "RDATE:20270116T090000Z,20270117T%02d0000%s\r\n" % (
                expected_hour, "" if other_offset == "floating" else "Z")
            assert expected in text.replace("\r\n ", "")
            assert radicale_item.read_one(text).vevent.rdate.value == event.rdate.value
        assert event.dtstart.value is start and event.dtstart.params == {}

    def test_timezone_scope_native_edits_override_stale_generated_metadata(self):
        from datetime import datetime, timedelta
        from uuid import uuid4

        from radicale import item as radicale_item

        first = "Test/Before-" + uuid4().hex
        second = "Test/After-" + uuid4().hex
        source = self._local_timezone_source(first, False)
        other = self._local_timezone_source(second, False)
        other = other.replace("TZOFFSETFROM:-0500", "TZOFFSETFROM:+0000")
        other = other.replace("TZOFFSETTO:-0500", "TZOFFSETTO:+0000")
        other = other.replace("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n", "")
        transition = "\r\n".join(["BEGIN:STANDARD", "DTSTART:20250101T000000",
                                  "TZOFFSETFROM:+0000", "TZOFFSETTO:+0200", "END:STANDARD"])
        zone_text = other[other.index("BEGIN:VTIMEZONE"):other.index("END:VTIMEZONE")]
        source = source.replace("BEGIN:VEVENT", zone_text + transition + "\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT")
        calendar = radicale_item.read_one(source)
        zone = vobject.icalendar.TimezoneComponent.gettzinfo(calendar.vtimezone_list[1])
        start = datetime(2027, 1, 15, 10, tzinfo=zone)
        calendar.vevent.dtstart.value = start
        calendar.vevent.dtend.value = start + timedelta(hours=1)
        assert calendar.vevent.dtstart.params["X-VOBJ-ORIGINAL-TZID"] == [first]
        text = calendar.serialize()
        assert "DTSTART;TZID=" + second + ":20270115T100000\r\n" in text
        assert radicale_item.read_one(text).vevent.dtstart.value == start
        assert calendar.vevent.dtstart.value is start

    @pytest.mark.parametrize("scoped", [False, True])
    def test_timezone_scope_native_edits_can_introduce_a_new_dst_zone(self, scoped):
        from datetime import datetime, timedelta
        from uuid import uuid4

        from radicale import item as radicale_item

        first = "Test/Old-" + uuid4().hex
        second = "Test/New-" + uuid4().hex
        source = self._local_timezone_source(first, False)
        calendar = radicale_item.read_one(source) if scoped else vobject.readOne(source)
        other = vobject.readOne(self._local_timezone_source(second, True), transform=False)
        zone = vobject.icalendar.TimezoneComponent.gettzinfo(other.vtimezone)
        start = datetime(2027, 1, 15, 10, tzinfo=zone)
        calendar.vevent.dtstart.value = start
        calendar.vevent.dtend.value = start + timedelta(hours=1)
        assert calendar.vevent.dtstart.params["X-VOBJ-ORIGINAL-TZID"] == [first]
        item = radicale_item.Item(collection_path="test", vobject_item=calendar)
        text = item.serialize()
        assert "DTSTART;TZID=" + second + ":20270115T100000\r\n" in text
        saved = radicale_item.read_one(text)
        assert saved.vevent.dtstart.value == start
        assert datetime(2027, 8, 15, tzinfo=saved.vevent.dtstart.value.tzinfo).utcoffset() == timedelta(hours=-4)
        assert calendar.vevent.dtstart.value is start

    @pytest.mark.parametrize("scoped", [False, True])
    @pytest.mark.parametrize("other_kind", ["named", "utc", "floating"])
    def test_timezone_scope_preserves_mixed_undeclared_native_date_lists(self, scoped, other_kind):
        from datetime import datetime
        from uuid import uuid4

        from dateutil.tz import tzutc

        from radicale import item as radicale_item

        first = "Test/Old-" + uuid4().hex
        source = self._local_timezone_source(first, False).replace(
            "END:VEVENT", "RDATE;TZID=" + first + ":20270822T100000\r\nEND:VEVENT")
        calendar = radicale_item.read_one(source) if scoped else vobject.readOne(source)
        sources = [self._local_timezone_source("Test/New-" + uuid4().hex, True),
                   self._local_timezone_source("Test/Other-" + uuid4().hex, False).replace("-0500", "-0600")]
        zones = [vobject.icalendar.TimezoneComponent.gettzinfo(vobject.readOne(source, transform=False).vtimezone)
                 for source in sources]
        if other_kind != "named":
            zones[1] = tzutc() if other_kind == "utc" else None
        values = [datetime(2027, 8, 22, 10, tzinfo=zones[0]), datetime(2027, 8, 23, 10, tzinfo=zones[1])]
        calendar.vevent.rdate.value = values
        assert (calendar.vevent.rdate.params.get("TZID") or
                calendar.vevent.rdate.params.get("X-VOBJ-ORIGINAL-TZID")) == [first]
        item = radicale_item.Item(collection_path="test", vobject_item=calendar)
        assert item.vobject_item.vevent.rdate.value == values
        text = item.serialize()
        expected = {"named": "160000Z", "utc": "100000Z", "floating": "100000"}[other_kind]
        assert "RDATE:20270822T140000Z,20270823T" + expected + "\r\n" in text
        assert radicale_item.read_one(text).vevent.rdate.value == values

    def test_timezone_scope_preserves_definitions_in_collection_imports(self):
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Bulk-" + uuid4().hex
        other_tzid = "Test/Other-" + uuid4().hex
        radicale_item.read_one(self._local_timezone_source(tzid, True))
        source = self._local_timezone_source(tzid, False, "first-zone")
        other = self._local_timezone_source(other_tzid, True, "second-zone")
        other_contents = other[other.index("BEGIN:VTIMEZONE"):other.index("END:VCALENDAR")]
        source = source.replace("END:VCALENDAR", other_contents + "END:VCALENDAR")
        self.put("/bulk/", source)
        _, responses = self.report("/bulk/", self._local_timezone_query("first-zone"))
        assert self._local_timezone_starts(responses) == ["20270815T150000Z"]
        _, responses = self.report("/bulk/", self._local_timezone_query("second-zone"))
        assert self._local_timezone_starts(responses) == ["20270815T140000Z"]
        for uid, expected_zone in [("first-zone", tzid), ("second-zone", other_tzid)]:
            _, _, stored = self.request("GET", "/bulk/" + uid + ".ics", check=200)
            calendar = vobject.readOne(stored)
            assert [zone.tzid.value for zone in calendar.vtimezone_list] == [expected_zone]

    @pytest.mark.parametrize("year", [1, 999])
    def test_timezone_serialization_pads_years_on_older_strftime_platforms(self, year):
        from datetime import datetime
        from uuid import uuid4

        from radicale import item as radicale_item

        class LegacyDatetime(datetime):
            def strftime(self, fmt):
                # Older supported Python/Linux delegates %Y to libc, which
                # may return an unpadded year. Reproduce that behavior here.
                return super().strftime(fmt.replace("%Y", str(self.year)))

        tzid = "Test/Early-" + uuid4().hex
        source = self._local_timezone_source(tzid, False).replace("20270115", "%04d0115" % year)
        calendar, = radicale_item.read_components(source)
        value = calendar.vevent.dtstart.value
        calendar.vevent.dtstart.value = LegacyDatetime(
            year, value.month, value.day, value.hour, value.minute, value.second, tzinfo=value.tzinfo)
        serialized = calendar.serialize()
        assert "DTSTART;TZID=" + tzid + ":%04d0115T100000\r\n" % year in serialized

    def test_timezone_scope_binds_native_period_values(self):
        from uuid import uuid4

        from radicale import item as radicale_item

        tzid = "Test/Periods-" + uuid4().hex
        vobject.readOne(self._local_timezone_source(tzid, True))
        source = self._local_timezone_source(tzid, False).replace(
            "END:VEVENT", "RDATE;VALUE=PERIOD;TZID=" + tzid +
            ":20270815T100000/20270815T110000,20270822T100000/PT1H\r\nEND:VEVENT")
        calendar, = radicale_item.read_components(source)
        # This checks native binding only; it does not extend vobject's
        # documented limits on PERIOD serialization and server operations.
        values = calendar.vevent.rdate.value
        assert values[0][0].utcoffset().total_seconds() == -18000
        assert values[0][1].utcoffset().total_seconds() == -18000
        assert values[1][0].utcoffset().total_seconds() == -18000
        assert values[1][1].total_seconds() == 3600

    def test_timezone_scope_rejects_duplicate_definitions_before_storage(self):
        from uuid import uuid4

        self.mkcalendar("/test/")
        tzid = "Test/Ambiguous-" + uuid4().hex
        source = self._local_timezone_source(tzid, True)
        fixed = self._local_timezone_source(tzid, False)
        zone = fixed[fixed.index("BEGIN:VTIMEZONE"):fixed.index("END:VTIMEZONE") + len("END:VTIMEZONE")]
        source = source.replace("BEGIN:VEVENT", zone + "\r\nBEGIN:VEVENT")
        self.put("/test/ambiguous.ics", source, check=400)
        self.get("/test/ambiguous.ics", check=404)

    @pytest.mark.parametrize("mtime_cache", [False, True])
    @pytest.mark.parametrize("folded", [None, "before-colon", "after-space"])
    def test_timezone_upgrade_rebuilds_legacy_ranges_without_changing_calendar(self, mtime_cache, folded):
        import pickle
        from hashlib import sha256
        from pathlib import Path
        from uuid import uuid4

        from radicale import item as radicale_item
        from radicale import utils

        self.configure({"storage": {"use_mtime_and_size_for_item_cache": str(mtime_cache)}})
        self.mkcalendar("/test/")
        if folded == "before-colon":
            tzid = "Test/" + "A" * 25 + uuid4().hex
        elif folded == "after-space":
            tzid = "Test/" + "A" * 64 + " " + uuid4().hex[:10]
        else:
            tzid = "Test/Upgrade-" + uuid4().hex
        radicale_item.read_one(self._local_timezone_source(tzid, True))
        self.put("/test/zone.ics", self._local_timezone_source(tzid, False))
        _, headers, body = self.request("GET", "/test/zone.ics", check=200)
        data = Path(self.colpath) / "collection-root" / "test" / "zone.ics"
        cache = data.parent / ".Radicale.cache" / "item" / data.name
        raw, info = data.read_bytes(), data.stat()
        if folded == "before-colon":
            # The normal 75-column serializer folds this valid parameter just
            # before the colon. The Outlook same-line workaround must not eat
            # the continuation space when an old cache forces reparsing.
            assert ("DTSTART;TZID=" + tzid + "\r\n :").encode() in raw
        elif folded == "after-space":
            assert ("TZID:" + tzid[:70] + "\r\n " + tzid[70:]).encode() in raw
        legacy = ("radicale=3.3.1;vobject=%s;" % utils.package_version("vobject")).encode()
        legacy_hash = (legacy.decode() + "size=%s;mtime=%s" % (info.st_size, info.st_mtime_ns)
                       if mtime_cache else sha256(legacy + raw).hexdigest())
        content = list(pickle.loads(cache.read_bytes()))
        content[0], content[-2], content[-1] = legacy_hash, 0, 1
        cache.write_bytes(pickle.dumps(tuple(content)))
        self.configure({})  # New application, same storage and old cache files.
        _, after_headers, after = self.request("GET", "/test/zone.ics", check=200)
        _, responses = self.report("/test/", self._local_timezone_query(
            start="20270815T153000Z", end="20270815T154500Z", expand=False))
        assert list(responses) == ["/test/zone.ics"]
        assert after == body and after_headers["ETag"] == headers["ETag"]
        assert data.read_bytes() == raw and data.stat().st_mtime_ns == info.st_mtime_ns
        assert pickle.loads(cache.read_bytes())[0] != legacy_hash

    def test_report_with_expand_property_timezone(self):
        self._test_expand(
            "event_weekly_rrule",
            "20060320T000000Z",
            "20060414T000000Z",
            [
                "RECURRENCE-ID:20060321T200000Z",
                "RECURRENCE-ID:20060328T200000Z",
                "RECURRENCE-ID:20060404T190000Z",
                "RECURRENCE-ID:20060411T190000Z",
            ],
            [
                "DTSTART:20060321T200000Z",
                "DTSTART:20060328T200000Z",
                "DTSTART:20060404T190000Z",
                "DTSTART:20060411T190000Z",
            ],
            [],
            CONTAINS_TIMES,
            1
        )

    def test_report_with_expand_property_max_occur(self) -> None:
        """Test report with expand property too many vevents"""
        self.configure({"reporting": {"max_expand_occurrence": 100}})
        self._test_expand_max(
            "event_daily_rrule_forever",
            "20060103T000000Z",
            "20060501T000000Z",
            check=400
        )

    def test_report_with_max_occur(self) -> None:
        """Test report with too many vevents"""
        self.configure({"reporting": {"max_expand_occurrence": 10}})

        uid = "event_multiple_too_many"
        start = "20130901T000000Z"
        end = "20130902T000000Z"
        check = 400

        status, responses = self.report("/calendar.ics/",
                                        self._req_without_expand(uid, start, end),
                                        check=check)
        assert len(responses) == 0
        assert status == check

    def test_report_with_max_expand_occurrence_unbounded_rrule(self) -> None:
        """Test that the plain calendar-query time-range filter respects
        max_expand_occurrence for unbounded RRULEs (PR #2354).

        The event recurs daily forever from 2006, the queried week is in
        November 2026. With the cap set to 10, only the first 10
        occurrences (January 2006) are enumerated, so the event must not
        match the queried week.
        """
        self.configure({"reporting": {"max_expand_occurrence": 10}})

        uid = "event_daily_rrule_forever"
        start = "20261115T000000Z"
        end = "20261122T000000Z"

        status, responses = self.report("/calendar.ics/",
                                        self._req_without_expand(uid, start, end))
        assert status == 207
        assert len(responses) == 0

    def test_report_with_max_expand_occurrence_unbounded_rrule_unlimited(self) -> None:
        """Control for the test above: with the cap disabled (0), the
        same unbounded event matches the queried week."""
        self.configure({"reporting": {"max_expand_occurrence": 0}})

        uid = "event_daily_rrule_forever"
        start = "20261115T000000Z"
        end = "20261122T000000Z"

        status, responses = self.report("/calendar.ics/",
                                        self._req_without_expand(uid, start, end))
        assert status == 207
        assert len(responses) == 1

    def test_report_vcalendar_all_components(self) -> None:
        """Test calendar-query with comp-filter VCALENDAR, returns all components."""
        self.mkcalendar("/test/")
        self.put("/test/calendar.ics", get_file_content("event_daily_rrule.ics"))
        self.put("/test/todo.ics", get_file_content("todo1.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data/>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR"/>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 2
        assert "/test/calendar.ics" in responses
        assert "/test/todo.ics" in responses

    def test_report_vevent_only(self) -> None:
        """Test calendar-query with comp-filter VEVENT, returns only VEVENT."""
        self.mkcalendar("/test/")
        self.put("/test/calendar.ics", get_file_content("event_daily_rrule.ics"))
        self.put("/test/todo.ics", get_file_content("todo1.ics"))

        start = "20060101T000000Z"
        end = "20060104T000000Z"

        request = f"""
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="{start}" end="{end}"/>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="{start}" end="{end}"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 1
        assert "/test/calendar.ics" in responses
        vevent_response = responses["/test/calendar.ics"]
        assert type(vevent_response) is dict
        status, element = vevent_response["C:calendar-data"]
        assert status == 200 and element.text
        assert "BEGIN:VEVENT" in element.text
        assert "UID:" in element.text
        assert "BEGIN:VTODO" not in element.text

    def test_report_time_range_no_vevent(self) -> None:
        """Test calendar-query with time-range filter, no matching VEVENT."""
        self.mkcalendar("/test/")
        self.put("/test/calendar.ics/", get_file_content("event_daily_rrule.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="20000101T000000Z" end="20000105T000000Z"/>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20000101T000000Z" end="20000105T000000Z"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 0

    def test_report_time_range_one_vevent(self) -> None:
        """Test calendar-query with time-range filter, matches one VEVENT."""
        self.mkcalendar("/test/")
        self.put("/test/calendar1.ics/", get_file_content("event_daily_rrule.ics"))
        self.put("/test/calendar2.ics/", get_file_content("event1.ics"))

        start = "20060101T000000Z"
        end = "20060104T000000Z"

        request = f"""
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="{start}" end="{end}"/>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="{start}" end="{end}"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 1
        response = responses["/test/calendar1.ics"]
        assert type(response) is dict
        status, element = response["C:calendar-data"]
        assert status == 200 and element.text
        assert "BEGIN:VEVENT" in element.text
        assert "RECURRENCE-ID:20060103T170000Z" in element.text
        assert "DTSTART:20060103T170000Z" in element.text

    def test_expand_non_recurring_event_with_tzid(self) -> None:
        """Expand time-range compares a non-recurring TZID event in UTC."""
        self.mkcalendar("/test/")
        self.put("/test/event1.ics/", get_file_content("event1.ics"))

        # event1 is 18:00-19:00 Europe/Paris (CEST), i.e. 16:00-17:00 UTC
        start = "20130901T153000Z"
        end = "20130901T173000Z"

        request = f"""
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="{start}" end="{end}"/>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="{start}" end="{end}"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        response = responses["/test/event1.ics"]
        assert type(response) is dict
        status, element = response["C:calendar-data"]
        assert status == 200 and element.text
        assert "UID:event1" in element.text

    def test_expand_report_for_recurring_and_non_recurring_events(self) -> None:
        """Test calendar-query with time-range filter, matches one VEVENT."""
        self.mkcalendar("/test/")
        self.put("/test/event.ics/", get_file_content("event_issue1812_2.ics"))
        self.put("/test/event2.ics/", get_file_content("event_issue1812_3.ics"))

        request = """
            <c:calendar-query xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:d="DAV:">
            <d:prop>
                <d:getetag/>
                <c:calendar-data>
                    <c:expand start="20250629T220000Z" end="20250803T220000Z"/>
                </c:calendar-data>
            </d:prop>
            <c:filter>
                <c:comp-filter name="VCALENDAR">
                    <c:comp-filter name="VEVENT">
                        <c:time-range start="20250629T220000Z" end="20250803T220000Z"/>
                    </c:comp-filter>
                </c:comp-filter>
            </c:filter>
            </c:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 2
        assert isinstance(responses, dict)
        assert "/test/event.ics" in responses
        assert "/test/event2.ics" in responses
        assert isinstance(responses["/test/event.ics"], dict)
        assert isinstance(responses["/test/event2.ics"], dict)

        assert "C:calendar-data" in responses["/test/event.ics"]
        status, event1_calendar_data = responses["/test/event.ics"]["C:calendar-data"]
        assert event1_calendar_data.text
        assert "UID:a07cfa8b-0ce6-4956-800d-c0bfe1f0730a" in event1_calendar_data.text
        assert "RECURRENCE-ID:20250716" in event1_calendar_data.text
        assert "RECURRENCE-ID:20250723" in event1_calendar_data.text
        assert "RECURRENCE-ID:20250730" in event1_calendar_data.text

        assert "C:calendar-data" in responses["/test/event2.ics"]
        status, event2_calendar_data = responses["/test/event2.ics"]["C:calendar-data"]
        assert event2_calendar_data.text
        assert "UID:c6be8b2c-3d72-453c-b698-4f25cdf1569e" in event2_calendar_data.text

    @staticmethod
    def _combined_expand_source(kind="utc", uid="combined-expand"):
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN",
                 "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:20261117T120000Z",
                 "DTSTART:20270815T100000Z", "DTEND:20270815T110000Z",
                 "RRULE:FREQ=DAILY;COUNT=2", "END:VEVENT", "END:VCALENDAR"]
        source = "\r\n".join(lines) + "\r\n"
        if kind == "all-day":
            source = source.replace("DTSTART:20270815T100000Z", "DTSTART;VALUE=DATE:20270815")
            source = source.replace("DTEND:20270815T110000Z", "DTEND;VALUE=DATE:20270816")
        elif kind == "timezone":
            source = source.replace("DTSTART:20270815T100000Z", "DTSTART;TZID=Europe/Paris:20270815T120000")
            source = source.replace("DTEND:20270815T110000Z", "DTEND;TZID=Europe/Paris:20270815T130000")
        elif kind == "override":
            override = "\r\n".join([
                "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:20261117T120000Z",
                "RECURRENCE-ID:20270816T100000Z", "DTSTART:20270816T120000Z",
                "DTEND:20270816T130000Z", "END:VEVENT"])
            source = source.replace("END:VCALENDAR", override + "\r\nEND:VCALENDAR")
        return source

    @staticmethod
    def _combined_expand_query(data_first=False, materialize=True, empty=False):
        start = "20270820T000000Z" if empty else "20270815T000000Z"
        end = "20270823T000000Z" if empty else "20270818T000000Z"
        data = '<C:calendar-data><C:expand start="%s" end="%s"/></C:calendar-data>' % (start, end)
        props = data + "<D:getetag/>" if data_first else "<D:getetag/>" + data
        uid_filter = ('<C:prop-filter name="UID"><C:text-match>combined-expand</C:text-match>'
                      '</C:prop-filter>') if materialize else ""
        return '''<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
          <D:prop>%s</D:prop><C:filter><C:comp-filter name="VCALENDAR">
            <C:comp-filter name="VEVENT">
              <C:time-range start="20270815T000000Z" end="20270818T000000Z"/>
              %s
            </C:comp-filter>
          </C:comp-filter></C:filter></C:calendar-query>''' % (props, uid_filter)

    @pytest.mark.parametrize("data_first", [False, True])
    @pytest.mark.parametrize("kind", ["utc", "all-day", "timezone", "override"])
    def test_report_combined_expand_properties_preserve_item_and_etag(self, monkeypatch, data_first, kind):
        from radicale.app import report as report_module

        self.mkcalendar("/test/")
        path = "/test/combined.ics"
        self.put(path, self._combined_expand_source(kind))
        _, before_headers, before = self.request("GET", path, check=200)
        original_expand = report_module._expand
        calls = []

        def expand(**kwargs):
            calls.append(kwargs["item"].href)
            return original_expand(**kwargs)

        monkeypatch.setattr(report_module, "_expand", expand)
        query = self._combined_expand_query(data_first)
        for attempt in range(2):
            _, responses = self.report("/test/", query)
            response = responses[path]
            assert isinstance(response, dict)
            status, etag = response["D:getetag"]
            assert status == 200 and etag.text == before_headers["ETag"]
            status, data = response["C:calendar-data"]
            assert status == 200 and data.text
            expanded = vobject.readOne(data.text)
            starts = [event.dtstart.value.strftime("%Y%m%d" if kind == "all-day" else "%Y%m%dT%H%M%SZ")
                      for event in expanded.vevent_list]
            expected = ["20270815T100000Z", "20270816T120000Z" if kind == "override" else "20270816T100000Z"]
            if kind == "all-day":
                expected = ["20270815", "20270816"]
            assert starts == expected
            assert len(calls) == attempt + 1
        _, after_headers, after = self.request("GET", path, check=200)
        assert after == before and after_headers["ETag"] == before_headers["ETag"]

    @pytest.mark.parametrize("kind", ["utc", "timezone", "override"])
    def test_expand_does_not_mutate_a_materialized_item(self, kind):
        from datetime import datetime, timezone

        from radicale.app.report import _expand
        from radicale.item import Item

        item = Item(collection_path="test", vobject_item=vobject.readOne(self._combined_expand_source(kind)))
        before = item.vobject_item.serialize()
        results = []
        for _ in range(2):
            element, count = _expand(ElementTree.Element("calendar-data"), item,
                                     datetime(2027, 8, 15, tzinfo=timezone.utc),
                                     datetime(2027, 8, 18, tzinfo=timezone.utc))
            assert count == 2
            results.append(element.text)
            assert item.vobject_item.serialize() == before
        assert results[0] == results[1]

    @pytest.mark.parametrize("data_first", [False, True])
    def test_report_combined_expand_counts_instances_once(self, data_first):
        self.configure({"reporting": {"max_expand_occurrence": "3"}})
        self.mkcalendar("/test/")
        self.put("/test/combined.ics", self._combined_expand_source())
        _, responses = self.report("/test/", self._combined_expand_query(data_first, materialize=False))
        assert list(responses) == ["/test/combined.ics"]

    @pytest.mark.parametrize("aggregate", [False, True])
    def test_report_combined_expand_keeps_actual_occurrence_limits(self, aggregate):
        self.configure({"reporting": {"max_expand_occurrence": "3"}})
        self.mkcalendar("/test/")
        source = self._combined_expand_source()
        if not aggregate:
            source = source.replace("COUNT=2", "COUNT=4")
        self.put("/test/combined.ics", source)
        if aggregate:
            self.put("/test/second.ics", self._combined_expand_source(uid="second-expand"))
        self.report("/test/", self._combined_expand_query(materialize=False), check=400)

    def test_report_combined_expand_caches_empty_results(self, monkeypatch):
        from radicale.app import report as report_module

        self.mkcalendar("/test/")
        self.put("/test/combined.ics", self._combined_expand_source())
        original_expand = report_module._expand
        calls = []

        def expand(**kwargs):
            calls.append(kwargs["item"].href)
            return original_expand(**kwargs)

        monkeypatch.setattr(report_module, "_expand", expand)
        _, responses = self.report("/test/", self._combined_expand_query(empty=True))
        assert not responses
        assert len(calls) == 1

    def test_report_getetag_expand_filter(self) -> None:
        """Test getetag with time-range filter and expand (example from #1880)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1880_1.ics", get_file_content("event_issue1880_1.ics"))
        self.put("/test/event_issue1880_2.ics", get_file_content("event_issue1880_2.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag>
                    <C:expand start="20250921T220000Z" end="20250928T220000Z"/>
                </D:getetag>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250921T220000Z" end="20250928T220000Z"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 2
        assert isinstance(responses["/test/event_issue1880_1.ics"], dict)
        assert isinstance(responses["/test/event_issue1880_2.ics"], dict)
        assert "D:getetag" in responses["/test/event_issue1880_1.ics"]
        assert "D:getetag" in responses["/test/event_issue1880_2.ics"]

    def test_report_getetag_expand_filter_positive1(self) -> None:
        """Test getetag with time-range filter and expand (not applicable), should return as matching filter range (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812_getetag.ics", get_file_content("event_issue1812_getetag.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag>
                    <C:expand start="20250706T220000Z" end="20250713T220000Z" />
                </D:getetag>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250716T220000Z" end="20250717T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 1
        assert isinstance(responses["/test/event_issue1812_getetag.ics"], dict)
        assert "D:getetag" in responses["/test/event_issue1812_getetag.ics"]

    def test_report_getetag_expand_filter_positive2(self) -> None:
        """Test getetag with time-range filter and expand, should return as matching filter range (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812.ics", get_file_content("event_issue1812.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag>
                   <C:expand start="20250706T220000Z" end="20250730T220000Z" />
                </D:getetag>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250716T220000Z" end="20250723T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 1
        assert isinstance(responses["/test/event_issue1812.ics"], dict)
        assert "D:getetag" in responses["/test/event_issue1812.ics"]

    def test_report_getetag_expand_filter_negative1(self) -> None:
        """Test getetag with time-range filter and expand, should not return anything (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812_getetag.ics", get_file_content("event_issue1812_getetag.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag>
                    <C:expand start="20250706T220000Z" end="20250713T220000Z" />
                </D:getetag>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250706T220000Z" end="20250713T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 0

    def test_report_getetag_expand_filter_negative2(self) -> None:
        """Test getetag with time-range filter and expand, should not return anything (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812_getetag.ics", get_file_content("event_issue1812_getetag.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag />
                <C:calendar-data>
                    <C:expand start="20240706T220000Z" end="20240713T220000Z" />
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250706T220000Z" end="20250713T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 0

    def test_report_getetag_expand_filter_negative3(self) -> None:
        """Test getetag with time-range filter and expand, should not return anything (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812_getetag.ics", get_file_content("event_issue1812_getetag.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="20240706T220000Z" end="20240713T220000Z" />
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20250706T220000Z" end="20250713T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 0

    def test_report_getetag_expand_filter_negative4(self) -> None:
        """Test getetag with time-range filter and expand, nothing returned as filter is not matching (example from #1812)."""
        self.mkcalendar("/test/")
        self.put("/test/event_issue1812.ics", get_file_content("event_issue1812.ics"))

        request = """
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag>
                   <C:expand start="20250706T220000Z" end="20250730T220000Z" />
                </D:getetag>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="20240716T220000Z" end="20240723T220000Z" />
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """
        status, responses = self.report("/test", request)
        assert status == 207
        assert len(responses) == 0

    def test_report_with_incompatible_recurrence_id_dt_types(self, caplog) -> None:
        """Test report with incompatible RECURRENCE-ID value types (RFC 5545 3.8.4.4)"""

        import logging
        caplog.set_level(logging.WARNING)

        event_id = "event_mixed_recurrence_id_dt_type"
        start = "20060103T000000Z"
        end = "20060105T000000Z"

        self.put("/calendar.ics/", get_file_content(f"{event_id}.ics"))

        request = f"""<?xml version="1.0" encoding="utf-8" ?>
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <C:calendar-data>
                    <C:expand start="{start}" end="{end}"/>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="{start}" end="{end}"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>
        """

        status, _, _ = self.request("REPORT", "/calendar.ics/", request)

        assert status == 400

        logs = caplog.messages
        error_logs = [log for log in logs if "incompatible RECURRENCE-ID value type" in log]
        assert len(error_logs) > 0
        assert event_id in error_logs[0]
        assert "base DTSTART is datetime" in error_logs[0]
        assert "override RECURRENCE-ID is date" in error_logs[0]

    def test_report_with_expand_property_all_day_event_overridden(self) -> None:
        self._test_expand(
            "event_full_day_rrule_overridden",
            "20060101T000000Z",
            "20060111T235959Z",
            ["RECURRENCE-ID:20060102", "RECURRENCE-ID:20060104", "RECURRENCE-ID:20060103"],
            ["DTSTART:20060102", "DTSTART:20060104", "DTSTART:20060110"],
            ["DTEND:20060103", "DTEND:20060105", "DTEND:20060111"],
            ONLY_DATES,
            2
        )

    def test_report_with_expand_property_strips_recurrence_properties(self) -> None:
        """Expanded instances must not retain recurrence properties.

        Regression test: the stored event has an RRULE and an RDATE but no
        EXDATE/EXRULE. Expanded VEVENT instances represent a single
        occurrence (identified by RECURRENCE-ID) and must not carry any
        recurrence-defining property (RRULE, RDATE, EXRULE, EXDATE).
        """
        uid = "event_rrule_rdate"
        start = "20060102T000000Z"
        end = "20060105T000000Z"

        # Baseline (no expand): the stored event still holds RRULE and RDATE.
        _, responses = self.report(
            "/calendar.ics/", self._req_without_expand(uid, start, end))
        response = responses[f"/calendar.ics/{uid}.ics"]
        assert isinstance(response, dict)
        status, element = response["C:calendar-data"]
        assert status == 200 and element.text
        assert "RRULE" in element.text
        assert "RDATE" in element.text

        # Expanded: individual instances must not carry recurrence properties.
        _, responses = self.report(
            "/calendar.ics/", self._req_with_expand(uid, start, end))
        response = responses[f"/calendar.ics/{uid}.ics"]
        assert isinstance(response, dict)
        status, element = response["C:calendar-data"]
        assert status == 200 and element.text
        assert "RECURRENCE-ID" in element.text
        assert "RRULE" not in element.text
        assert "RDATE" not in element.text
