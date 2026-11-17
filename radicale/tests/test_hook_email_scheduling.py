# This file is part of Radicale - CalDAV and CardDAV server
# Copyright © 2026-2026 Contributors to Radicale
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# If not, see <http://www.gnu.org/licenses/>.

"""Scheduling decisions, outgoing MIME and real calendar write integration."""

import base64
import logging
from datetime import datetime, timedelta
from typing import List, Tuple

import pytest
import vobject

from radicale.hook.email.scheduling import (build_itip_ics, build_itip_message,
                                            check_ownership, extract_email,
                                            plan_messages)
from radicale.tests import BaseTest

OWNER = ["jeff@example.com"]


def plan(old, new, deleted=False, owner=OWNER):
    return plan_messages(old, new, deleted=deleted, owner_identities=owner)


def _event(attendees, dtstart="20261113T200000Z", dtend="20261113T220000Z",
           sequence=1, organizer_agent=""):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN",
             "BEGIN:VEVENT", "UID:pizza-1", "DTSTAMP:20261030T120000Z",
             "DTSTART:%s" % dtstart, "DTEND:%s" % dtend,
             "SUMMARY:Pizza Party", "LOCATION:Home",
             "ORGANIZER;CN=Jeff%s:mailto:jeff@example.com" % organizer_agent]
    for name, email, extra in attendees:
        lines.append("ATTENDEE;CN=%s%s:mailto:%s" % (name, extra, email))
    lines.append("SEQUENCE:%d" % sequence)
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


ALICE = ("Alice", "alice@example.com", "")
BOB = ("Bob", "bob@example.com", "")
CAROL = ("Carol", "carol@example.com", "")


def _recipients(planned):
    return [(p.method, p.recipient_email, p.kind) for p in planned]


def _unsupported_identity_cases():
    cases = []
    fields = [("organizer", "ORGANIZER;CN=Jeff", "jeff@example.com"),
              ("attendee", "ATTENDEE;CN=Alice", "alice@example.com"),
              ("other-attendee", "ATTENDEE;CN=Bob", "bob@example.com")]
    for label, prefix, address in fields:
        original = prefix + ":mailto:" + address
        cases.append((label + "-bare", original, prefix + ":" + address))
        cases.append((label + "-query", original, original + "?subject=extra"))
        for parameter in ("VALUE=TEXT", "VALUE=CAL-ADDRESS,CAL-ADDRESS",
                          'DELEGATED-FROM="mailto:delegate@example.com"',
                          'DELEGATED-TO="mailto:delegate@example.com"',
                          'SENT-BY="mailto:delegate@example.com"',
                          'MEMBER="mailto:group@example.com"'):
            cases.append((label + "-" + parameter.split("=")[0], original,
                          prefix + ";" + parameter + ":mailto:" + address))
    attendee = "ATTENDEE;CN=Alice:mailto:alice@example.com"
    cases.extend([
        ("duplicate-attendee", attendee, attendee + "\n" + attendee),
        ("encoded-duplicate-attendee", attendee,
         attendee + "\nATTENDEE;CN=Alias:mailto:%61LICE@EXAMPLE.COM"),
        ("duplicate-organizer", fields[0][1] + ":mailto:" + fields[0][2],
         (fields[0][1] + ":mailto:" + fields[0][2] + "\n") * 2),
    ])
    return cases


class TestCalendarIdentityEligibility:
    @pytest.mark.parametrize("case,original,replacement", _unsupported_identity_cases())
    def test_unsupported_identity_makes_whole_itip_event_ineligible(self, case, original, replacement):
        from radicale.hook.email.scheduling import (parse_event,
                                                    prepare_itip_upload)

        source = _event([ALICE, BOB], sequence=5)
        unsupported = source.replace(original, replacement).replace(
            "SEQUENCE:5", "SEQUENCE:1").replace("LOCATION:Home", "LOCATION:Away")
        parsed = parse_event(unsupported)
        assert parsed is not None and parsed.uid == "pizza-1", case
        assert plan(None, unsupported) == []
        assert plan(source, unsupported) == []
        assert plan(unsupported, source) == []
        assert plan(unsupported, None, deleted=True) == []
        assert prepare_itip_upload(source, unsupported, OWNER) is None
        assert prepare_itip_upload(unsupported, source, OWNER) is None
        for method in ("REQUEST", "CANCEL"):
            with pytest.raises(ValueError, match="identity"):
                build_itip_ics(unsupported, method, "alice@example.com")

    def test_literal_percent_and_plus_are_distinct_attendees(self):
        source = _event([("Literal", "alice%252Btag@example.com", ";VALUE=cal-address"),
                         ("Plus", "alice%2Btag@example.com", ";VALUE=CAL-ADDRESS")])
        assert [message.recipient_email for message in plan(None, source)] == [
            "alice%2btag@example.com", "alice+tag@example.com"]


class TestCalendarEmailAddresses:
    @pytest.mark.parametrize("value,expected", [
        ("mailto:Alice%2BTag@EXAMPLE.COM", "alice+tag@example.com"),
        ("MAILTO:Alice+Tag@EXAMPLE.COM", "alice+tag@example.com"),
        ("mailto:alice%25tag@example.com", "alice%tag@example.com"),
        ("mailto:alice%252Btag@example.com", "alice%2btag@example.com"),
        ("mailto:alice%3Ftag@example.com", "alice?tag@example.com"),
        ("mailto:alice%23tag@example.com", "alice#tag@example.com"),
        ("mailto:%61lice%40example.com", "alice@example.com"),
        ("mailto:alice%2Ftag@example.com", "alice/tag@example.com"),
        (" alice%2Btag@example.com ", "alice%2btag@example.com"),
        ("alice%tag@example.com", "alice%tag@example.com"),
        ("Alice+Tag@Example.com", "alice+tag@example.com"),
    ])
    def test_mailto_decodes_once_and_bare_mailbox_percent_is_literal(self, value, expected):
        from radicale.hook.email import extract_email as legacy_extract_email

        assert extract_email(value) == expected
        assert legacy_extract_email(value) == expected

    @pytest.mark.parametrize("value", [
        None, "", "mailto:",
        "mailto:alice@example.com?subject=invitation", "mailto:alice@example.com?",
        "mailto:alice@example.com#fragment", "mailto:alice@example.com#",
        "mailto://alice@example.com", "prefix mailto:alice@example.com",
        "urn:mailto:alice@example.com", "https://example.com/mailto:alice@example.com",
        "mailto:mailto:alice@example.com", "mailto:alice%2@example.com",
        "mailto:alice%gg@example.com", "mailto:alice%20tag@example.com",
        "mailto:alice@example.com%20", "mailto:alice%0D%0Atag@example.com",
        " mailto:alice@example.com", "mailto:alice@example.com ",
        "mailto:álîce@example.com", "mailto:alice%FF@example.com",
        "mailto:alice@example..com", "mailto:.alice@example.com",
        "mailto:alice@example.com,other@example.com",
        "mailto:alice@example.com;other@example.com",
    ])
    def test_unsupported_or_malformed_calendar_address_is_not_a_recipient(self, value):
        from radicale.hook.email import extract_email as legacy_extract_email

        assert extract_email(value) is None
        assert legacy_extract_email(value) is None

    @pytest.mark.parametrize("address,uri", [
        ("jeff%2Btag@example.com", "mailto:jeff%252Btag@example.com"),
        ("jeff%tag@example.com", "mailto:jeff%25tag@example.com"),
        ("jeff+tag@example.com", "mailto:jeff%2Btag@example.com"),
        ("jeff?tag@example.com", "mailto:jeff%3Ftag@example.com"),
        ("jeff#tag@example.com", "mailto:jeff%23tag@example.com"),
    ])
    def test_owner_configuration_and_username_keep_bare_identity(self, address, uri):
        from radicale.hook.email.scheduling import (parse_owner_map,
                                                    resolve_owner_identities,
                                                    username_email)

        owner_map = parse_owner_map({"jeff": address})
        assert owner_map == {"jeff": address.lower()}
        assert username_email(address) == address.lower()
        owner = resolve_owner_identities("jeff", owner_map)
        assert owner == [address.lower()]
        source = _event([ALICE]).replace("mailto:jeff@example.com", uri)
        assert _recipients(plan(None, source, owner=owner)) == [
            ("REQUEST", "alice@example.com", "created")]

    @pytest.mark.parametrize("encoded,literal", [("alice%2Btag", "alice+tag"), ("%61lice", "alice")])
    def test_equivalent_mailto_spelling_does_not_reinvite_attendee(self, encoded, literal):
        source = _event([("Alice", encoded + "@example.com", "")])
        updated = source.replace("mailto:" + encoded + "@example.com",
                                 "mailto:" + literal + "@example.com")
        assert plan(source, updated) == []

    @pytest.mark.parametrize("original", [
        "MAILTO:JEFF@EXAMPLE.COM", "mailto:j%65ff@example.com"])
    def test_equivalent_organizer_uri_does_not_notify_or_advance_revision(self, original):
        from radicale.hook.email.scheduling import prepare_itip_upload

        source = _event([ALICE]).replace("mailto:jeff@example.com", original)
        assert _recipients(plan(None, source)) == [
            ("REQUEST", "alice@example.com", "created")]
        updated = source.replace(original, "mailto:jeff@example.com")
        assert plan(source, updated) == []
        assert prepare_itip_upload(source, updated, OWNER) is None


class TestPlanMessages:
    def test_adding_one_attendee_notifies_only_the_new_attendee(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB, CAROL])
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "carol@example.com", "added")]

    def test_removing_an_attendee_sends_cancel_only_to_them(self):
        old = _event([ALICE, BOB, CAROL])
        new = _event([ALICE, BOB])
        assert _recipients(plan(old, new)) == [
            ("CANCEL", "carol@example.com", "removed")]

    def test_changing_an_attendee_and_time_notifies_both_groups_once(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, CAROL], dtstart="20261113T210000Z")
        planned = _recipients(plan(old, new))
        assert ("REQUEST", "carol@example.com", "added") in planned
        assert ("CANCEL", "bob@example.com", "removed") in planned
        assert ("REQUEST", "alice@example.com", "updated") in planned
        assert len(planned) == 3

    def test_organizer_time_or_location_update_reaches_existing_attendees(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB], dtstart="20261113T210000Z")
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "alice@example.com", "updated"),
            ("REQUEST", "bob@example.com", "updated")]
        moved = _event([ALICE, BOB], dtstart="20261113T200000Z")
        moved = moved.replace("LOCATION:Home", "LOCATION:Elsewhere")
        assert [p.kind for p in plan(old, moved)] == ["updated", "updated"]

    def test_repeated_put_of_the_same_event_sends_nothing(self):
        event = _event([ALICE, BOB, CAROL])
        assert plan(event, event) == []
        # A client re-saving the event with a new DTSTAMP (and even a
        # bumped SEQUENCE, which clients do on attendee-only edits)
        # must not, by itself, re-notify anybody.
        resaved = event.replace("DTSTAMP:20261030T120000Z",
                                "DTSTAMP:20261030T130000Z")
        resaved = resaved.replace("SEQUENCE:1", "SEQUENCE:2")
        assert plan(event, resaved) == []

    def test_new_event_notifies_all_attendees_but_not_the_organizer(self):
        event = _event([ALICE, BOB, ("Jeff", "jeff@example.com", "")])
        assert _recipients(plan(None, event)) == [
            ("REQUEST", "alice@example.com", "created"),
            ("REQUEST", "bob@example.com", "created")]

    def test_event_deletion_cancels_for_all_attendees(self):
        old = _event([ALICE, BOB])
        assert _recipients(plan(old, None, deleted=True)) == [
            ("CANCEL", "alice@example.com", "deleted"),
            ("CANCEL", "bob@example.com", "deleted")]

    def test_status_cancellation_notifies_previous_eligible_attendees(self):
        client = ("Carol", "carol@example.com", ";SCHEDULE-AGENT=CLIENT")
        old = _event([ALICE, BOB, client])
        new = _event([BOB, client, ("Dan", "dan@example.com", "")])
        new = new.replace("END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
        assert _recipients(plan(old, new)) == [
            ("CANCEL", "alice@example.com", "cancelled"),
            ("CANCEL", "bob@example.com", "cancelled")]
        assert plan(old, new, owner=[]) == []

    @pytest.mark.parametrize("old_agent,new_agent,expected", [
        ("", ";SCHEDULE-AGENT=CLIENT", []),
        ("", ";SCHEDULE-AGENT=NONE", []),
        (";SCHEDULE-AGENT=CLIENT", ";SCHEDULE-AGENT=SERVER",
         [("CANCEL", "alice@example.com", "cancelled")]),
        (";SCHEDULE-AGENT=NONE", "",
         [("CANCEL", "alice@example.com", "cancelled")]),
    ])
    def test_status_cancellation_uses_retained_attendee_current_agent(
            self, old_agent, new_agent, expected):
        old = _event([("Alice", "alice@example.com", old_agent)])
        new = _event([("Alice", "alice@example.com", new_agent)]).replace(
            "END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
        assert _recipients(plan(old, new)) == expected

    def test_status_cancellation_uses_removed_attendee_previous_agent(self):
        old = _event([ALICE, ("Bob", "bob@example.com", ";SCHEDULE-AGENT=CLIENT")])
        new = _event([]).replace("END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
        assert _recipients(plan(old, new)) == [
            ("CANCEL", "alice@example.com", "cancelled")]

    def test_cancelled_event_does_not_send_new_invitations_or_repeat_cancel(self):
        cancelled = _event([ALICE, BOB]).replace(
            "END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
        assert plan(None, cancelled) == []
        assert plan(cancelled, cancelled) == []
        edited = cancelled.replace("SUMMARY:Pizza Party", "SUMMARY:Cancelled party")
        assert plan(cancelled, edited) == []
        assert plan(cancelled, None, deleted=True) == []

    def test_reactivated_event_requests_current_attendees_only(self):
        old = _event([ALICE, BOB]).replace(
            "END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
        new = _event([BOB, CAROL])
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "bob@example.com", "updated"),
            ("REQUEST", "carol@example.com", "added")]

    def test_schedule_agent_client_attendee_is_skipped(self):
        old = _event([ALICE])
        client_carol = ("Carol", "carol@example.com", ";SCHEDULE-AGENT=CLIENT")
        assert plan(old, _event([ALICE, client_carol])) == []

    def test_schedule_agent_client_organizer_does_not_suppress_attendees(self):
        # RFC 6638: the ORGANIZER flag governs messages to the
        # organizer, not REQUESTs to attendees. A client-owned
        # organizer flag must not suppress attendee delivery.
        old = _event([ALICE])
        new = _event([ALICE, CAROL], organizer_agent=";SCHEDULE-AGENT=CLIENT")
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "carol@example.com", "added")]

    def test_unparseable_previous_event_sends_nothing(self):
        # Fail closed: notifying everybody on a parse failure
        # recreates the blast this patch is meant to stop.
        new = _event([ALICE, BOB])
        assert plan("not an ics file", new) == []

    def test_attendee_copy_sends_nothing(self):
        # Organizer is Jeff, owner is Alice: this is Alice's
        # attendee copy, not Jeff's organizer event.
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB, CAROL])
        assert plan(old, new, owner=["alice@example.com"]) == []
        assert plan(None, new, owner=["alice@example.com"]) == []
        assert plan(old, None, deleted=True, owner=["alice@example.com"]) == []

    def test_no_owner_identity_sends_nothing(self):
        assert plan(None, _event([ALICE, BOB]), owner=[]) == []
        assert plan(None, _event([ALICE, BOB]), owner=None) == []

    def test_bare_username_owner_no_longer_matches(self):
        # Only a full email identity counts. A bare username
        # must not match by local-part, even for the right user.
        assert check_ownership("jeff@example.com", ["jeff"]) is False
        assert check_ownership("jeff@example.com", ["jeff@example.com"]) is True
        assert check_ownership("jeff@example.com", ["alice"]) is False
        assert check_ownership("jeff@example.com", ["jeff@other.example"]) is False

    def test_schedule_agent_none_attendee_is_skipped(self):
        old = _event([ALICE])
        none_carol = ("Carol", "carol@example.com", ";SCHEDULE-AGENT=NONE")
        assert plan(old, _event([ALICE, none_carol])) == []

    def test_recurrence_rule_change_triggers_update(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB])
        new = new.replace("LOCATION:Home",
                          "LOCATION:Home\nRRULE:FREQ=WEEKLY;COUNT=4")
        assert [p.kind for p in plan(old, new)] == ["updated", "updated"]

    def test_schedule_agent_params_are_stripped_from_itip(self):
        event = _event([ALICE, CAROL], organizer_agent=";SCHEDULE-AGENT=CLIENT")
        event = event.replace("ATTENDEE;CN=Carol:mailto:carol@example.com",
                              "ATTENDEE;CN=Carol;SCHEDULE-AGENT=SERVER:mailto:carol@example.com")
        itip = build_itip_ics(event, "REQUEST", "carol@example.com")
        assert "SCHEDULE-AGENT" not in itip
        assert "SCHEDULE-FORCE-SEND" not in itip


class TestOrganizerAttendeeChanges:
    @pytest.mark.parametrize("previous_rsvp", ["", ";RSVP=FALSE", ";RSVP=false"])
    def test_enabling_rsvp_requests_only_that_attendees_reconfirmation(self, previous_rsvp):
        old = _event([("Alice", "alice@example.com", ";PARTSTAT=ACCEPTED" + previous_rsvp), BOB])
        new = _event([("Alice", "alice@example.com", ";PARTSTAT=ACCEPTED;RSVP=TRUE"), BOB])
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "alice@example.com", "reconfirmation")]
        assert plan(old, new, owner=["bob@example.com"]) == []
        assert plan(new, new) == []

    @pytest.mark.parametrize("previous_role,new_role", [
        ("", ";ROLE=OPT-PARTICIPANT"),
        (";ROLE=OPT-PARTICIPANT", ""),
        (";ROLE=REQ-PARTICIPANT", ";ROLE=CHAIR")])
    def test_role_change_updates_only_the_affected_attendee(self, previous_role, new_role):
        old = _event([("Alice", "alice@example.com", previous_role), BOB])
        new = _event([("Alice", "alice@example.com", new_role), BOB])
        assert _recipients(plan(old, new)) == [("REQUEST", "alice@example.com", "updated")]
        assert plan(old, new, owner=[]) == []

    @pytest.mark.parametrize("previous,new", [
        ("", ";ROLE=REQ-PARTICIPANT"),
        (";ROLE=REQ-PARTICIPANT", ";ROLE=req-participant"),
        (";RSVP=TRUE", ";RSVP=true"),
        ("", ";RSVP=FALSE")])
    def test_explicit_defaults_and_parameter_case_do_not_notify(self, previous, new):
        assert plan(_event([("Alice", "alice@example.com", previous)]),
                    _event([("Alice", "alice@example.com", new)])) == []

    @pytest.mark.parametrize("rsvp", [";RSVP=TRUE", ";RSVP=FALSE", ""])
    @pytest.mark.parametrize("partstat", ["ACCEPTED", "TENTATIVE", "DECLINED"])
    def test_response_state_changes_do_not_request_reconfirmation(self, rsvp, partstat):
        old = _event([("Alice", "alice@example.com", ";PARTSTAT=NEEDS-ACTION;RSVP=TRUE"), BOB])
        new = _event([("Alice", "alice@example.com", ";PARTSTAT=" + partstat + rsvp), BOB])
        assert plan(old, new) == []

    @pytest.mark.parametrize("agent", ["CLIENT", "NONE"])
    @pytest.mark.parametrize("change", [";RSVP=TRUE", ";ROLE=OPT-PARTICIPANT"])
    def test_attendee_edits_honor_current_schedule_agent(self, agent, change):
        old = _event([ALICE, BOB])
        new = _event([("Alice", "alice@example.com", ";SCHEDULE-AGENT=" + agent + change), BOB])
        assert plan(old, new) == []

    def test_simultaneous_details_role_and_rsvp_changes_do_not_duplicate_requests(self):
        old = _event([ALICE, BOB])
        new = _event([("Alice", "alice@example.com", ";RSVP=TRUE;ROLE=OPT-PARTICIPANT"), BOB])
        new = new.replace("LOCATION:Home", "LOCATION:Away")
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "alice@example.com", "updated"),
            ("REQUEST", "bob@example.com", "updated")]


class TestRecurrenceFingerprint:
    def _source(self, properties, all_day=False):
        source = _event([ALICE, BOB])
        if all_day:
            source = source.replace("DTSTART:20261113T200000Z", "DTSTART;VALUE=DATE:20261113")
            source = source.replace("DTEND:20261113T220000Z", "DTEND;VALUE=DATE:20261114")
        if "RRULE:" not in properties:
            properties = "RRULE:FREQ=WEEKLY;COUNT=12\n" + properties
        return source.replace("END:VEVENT", properties + "\nEND:VEVENT")

    @pytest.mark.parametrize("name", ["EXDATE", "RDATE"])
    @pytest.mark.parametrize("change", ["add", "remove", "replace"])
    def test_later_date_property_changes_notify_attendees(self, name, change):
        first = name + ":20261120T200000Z"
        second = name + ":20261127T200000Z"
        old = self._source(first + "\n" + second)
        if change == "add":
            new = self._source(first + "\n" + second + "\n" + name + ":20261204T200000Z")
        elif change == "remove":
            new = self._source(first)
        else:
            new = old.replace("20261127T200000Z", "20261204T200000Z")
        assert _recipients(plan(old, new)) == [
            ("REQUEST", "alice@example.com", "updated"),
            ("REQUEST", "bob@example.com", "updated")]

    @pytest.mark.parametrize("name", ["EXDATE", "RDATE"])
    @pytest.mark.parametrize("value_type", ["DATE", "DATE-TIME"])
    def test_date_grouping_order_and_duplicates_do_not_notify(self, name, value_type):
        suffix = "T200000Z" if value_type == "DATE-TIME" else ""
        first, second = "20261120" + suffix, "20261127" + suffix
        header = name + ";VALUE=" + value_type + ":"
        old = self._source(header + first + "\n" + header + second, value_type == "DATE")
        new = self._source(header + ",".join((second, first, second)), value_type == "DATE")
        assert plan(old, new) == []

    @pytest.mark.parametrize("name", ["EXDATE", "RDATE"])
    def test_equivalent_parameter_order_quoting_and_default_type_do_not_notify(self, name):
        old = self._source(
            name + ";TZID=Europe/Paris;X-LABEL=CaseSensitive:20261120T200000")
        new = self._source(
            name + ';X-LABEL=CaseSensitive;VALUE=DATE-TIME;TZID="Europe/Paris":20261120T200000')
        assert plan(old, new) == []

    @pytest.mark.parametrize("name", ["EXDATE", "RDATE"])
    @pytest.mark.parametrize("parameter,old_value,new_value", [
        ("TZID", "Europe/Paris", "Europe/Berlin"),
        ("X-LABEL", "Original", "Revised")])
    def test_later_date_property_parameter_change_notifies(
            self, name, parameter, old_value, new_value):
        # Paris and Berlin have the same offset on this date, but the
        # referenced timezone identifier must remain part of the comparison.
        old = self._source(
            name + ":20261120T200000Z\n" + name + ";" + parameter + "=" +
            old_value + ":20261127T200000")
        new = old.replace(parameter + "=" + old_value, parameter + "=" + new_value)
        assert [message.kind for message in plan(old, new)] == ["updated", "updated"]

    def test_rule_part_case_and_list_order_do_not_notify(self):
        old = self._source("RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO,WE")
        new = self._source("RRULE:byday=WE,MO;freq=WEEKLY;count=4")
        assert plan(old, new) == []
        assert [message.kind for message in plan(old, new.replace("count=4", "count=8"))] == [
            "updated", "updated"]


class TestItipMessage:
    def test_request_contains_only_the_recipient_and_the_organizer(self):
        new = _event([ALICE, BOB, CAROL], sequence=3)
        itip = build_itip_ics(new, "REQUEST", "carol@example.com")
        assert "METHOD:REQUEST" in itip
        assert "SEQUENCE:3" in itip
        assert itip.count("ATTENDEE") == 1
        assert "carol@example.com" in itip
        assert "alice@example.com" not in itip
        assert "bob@example.com" not in itip

    def test_cancel_is_built_from_the_old_event(self):
        old = _event([ALICE, BOB, CAROL])
        itip = build_itip_ics(old, "CANCEL", "carol@example.com")
        assert "METHOD:CANCEL" in itip
        assert "carol@example.com" in itip

    @pytest.mark.parametrize("cancel_all", [False, True])
    @pytest.mark.parametrize("recurring", [False, True])
    def test_cancel_scope_sets_status_and_omits_alarms(self, cancel_all, recurring):
        source = _event([ALICE, BOB])
        details = ("STATUS:CONFIRMED\nBEGIN:VALARM\nACTION:DISPLAY\n"
                   "DESCRIPTION:Reminder\nTRIGGER:-PT15M\nEND:VALARM\n")
        if recurring:
            details += "RRULE:FREQ=WEEKLY;COUNT=4\n"
        source = source.replace("END:VEVENT", details + "END:VEVENT")
        wire = vobject.readOne(build_itip_ics(
            source, "CANCEL", "alice@example.com", cancel_all=cancel_all))
        assert wire.method.value == "CANCEL"
        assert "valarm" not in wire.vevent.contents
        assert ("rrule" in wire.vevent.contents) == recurring
        assert len(wire.vevent.attendee_list) == 1
        if cancel_all:
            assert [line.value for line in wire.vevent.status_list] == ["CANCELLED"]
        else:
            assert "status" not in wire.vevent.contents

    def test_itip_email_carries_text_calendar_with_method(self):
        new = _event([ALICE, CAROL])
        itip = build_itip_ics(new, "REQUEST", "carol@example.com")
        message = build_itip_message(
            "jeff@example.com", "carol@example.com", "Added",
            "Hello Carol", itip, "REQUEST")
        assert 'method="REQUEST"' in message
        assert "text/calendar" in message
        # The calendar part is base64-encoded; decode it and check
        # the method survives the round trip.
        decoded = base64.b64decode(
            "".join(message.split("filename=\"event.ics\"")[1].split("--")[0].split())).decode()
        assert "METHOD:REQUEST" in decoded

    def test_reply_to_is_optional_and_keeps_global_sender(self):
        from email import message_from_string

        event = _event([ALICE, CAROL])
        itip = build_itip_ics(event, "REQUEST", "carol@example.com")
        original = build_itip_message("calendar@example.com", "carol@example.com",
                                      "Added", "Hello", itip, "REQUEST")
        assert message_from_string(original).get_all("Reply-To") is None
        with_reply = build_itip_message(
            "calendar@example.com", "carol@example.com", "Added", "Hello",
            itip, "REQUEST", reply_to_email="alice@example.com")
        parsed = message_from_string(with_reply)
        assert parsed.get_all("Reply-To") == ["alice@example.com"]
        assert parsed["From"] == "calendar@example.com"
        assert parsed["To"] == "carol@example.com"

    def test_reply_to_rejects_newlines(self):
        import pytest

        event = _event([ALICE, CAROL])
        itip = build_itip_ics(event, "REQUEST", "carol@example.com")
        for unsafe in ("alice@example.com\rBcc:evil@example.com",
                       "alice@example.com\nBcc:evil@example.com"):
            with pytest.raises(ValueError, match="CR or LF"):
                build_itip_message("calendar@example.com", "carol@example.com",
                                   "Added", "Hello", itip, "REQUEST",
                                   reply_to_email=unsafe)

    def test_cancel_increments_sequence_and_updates_dtstamp(self):
        old = _event([ALICE, BOB, CAROL], sequence=3)
        itip = build_itip_ics(old, "CANCEL", "carol@example.com")
        assert "METHOD:CANCEL" in itip
        assert "SEQUENCE:4" in itip
        assert "DTSTAMP:20261030T120000Z" not in itip

    def test_itip_builder_rejects_multi_vevent(self):
        # The builder enforces the planner's restriction
        # independently, instead of filtering overrides.
        import pytest
        base = _event([ALICE, BOB, CAROL])
        override = ("BEGIN:VEVENT\nUID:pizza-1\nDTSTAMP:20261030T120000Z\n"
                    "RECURRENCE-ID:20261120T200000Z\nDTSTART:20261120T200000Z\n"
                    "DTEND:20261120T220000Z\nSUMMARY:Pizza Party\n"
                    "ORGANIZER;CN=Jeff:mailto:jeff@example.com\n"
                    "ATTENDEE;CN=Alice:mailto:alice@example.com\n"
                    "ATTENDEE;CN=Bob:mailto:bob@example.com\n"
                    "ATTENDEE;CN=Carol:mailto:carol@example.com\n"
                    "SEQUENCE:1\nEND:VEVENT\n")
        event = base.replace("END:VCALENDAR", override + "END:VCALENDAR")
        with pytest.raises(ValueError):
            build_itip_ics(event, "REQUEST", "carol@example.com")


class TestHookWiring(BaseTest):
    """End-to-end: a PUT that adds one attendee, hook in dry-run."""

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        # Same rights setup as test_hook_email.py: without it,
        # anonymous requests are denied by the default owner_only
        # rights, and MKCALENDAR returns 401.
        import os
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("[allow all]\nuser: .*\ncollection: .*\npermissions: RrWw")
        self.configure({
            "rights": {"file": rights_file_path, "type": "from_file"}})
        self.configure({"auth": {"type": "none"}})
        self.configure({
            "hook": {"type": "email", "dryrun": "True",
                     "itip_enabled": "True",
                     "from_email": "jeff@example.com",
                     "itip_owner_map": '{"jeff": "jeff@example.com"}'}})

    def _future_event(self, attendees, start):
        return _event(
            attendees,
            dtstart=start.strftime("%Y%m%dT%H%M%SZ"),
            dtend=(start + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ"))

    def test_put_adding_one_attendee_dryrun_logs_only_the_new_attendee(self, caplog):
        caplog.set_level(logging.INFO)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        path = "/calendar.ics/event1.ics"
        # Use the same event time for both PUTs. Only attendees change.
        start = datetime.now() + timedelta(days=1)
        self.put(path, self._future_event([ALICE, BOB], start), login="jeff:password")
        # An update to an existing item returns 204, not 201.
        self.put(path, self._future_event([ALICE, BOB, CAROL], start),
                 check=204, login="jeff:password")
        dryrun_to = [log for log in caplog.messages
                     if "DRY-RUN send_scheduling_email / to_addresses" in log]
        # First PUT notifies Alice and Bob; the second notifies Carol only.
        assert dryrun_to == [
            "Hook 'email': DRY-RUN send_scheduling_email / to_addresses=['alice@example.com']",
            "Hook 'email': DRY-RUN send_scheduling_email / to_addresses=['bob@example.com']",
            "Hook 'email': DRY-RUN send_scheduling_email / to_addresses=['carol@example.com']",
        ]

    @pytest.mark.parametrize("operation", ["remove", "cancel", "delete"])
    @pytest.mark.parametrize("recurring", [False, True])
    def test_cancellation_wire_scope(self, monkeypatch, operation, recurring):
        from email import message_from_string, policy

        from radicale.hook.email import EmailConfig

        sent = []

        def capture_smtp(config, text, to_addresses):
            message = message_from_string(text, policy=policy.default)
            calendar = [part for part in message.walk()
                        if part.get_content_type() == "text/calendar"]
            assert len(calendar) == 1
            wire = vobject.readOne(calendar[0].get_payload(decode=True).decode("utf-8"))
            sent.append((to_addresses, calendar[0].get_param("method"), wire))
            return True

        monkeypatch.setattr(EmailConfig, "_smtp_send", capture_smtp)
        self.configure({"hook": {"dryrun": "False"}})
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        path = "/calendar.ics/event1.ics"
        source = self._future_event([ALICE, BOB], datetime.now() + timedelta(days=1))
        details = ("STATUS:CONFIRMED\nBEGIN:VALARM\nACTION:DISPLAY\n"
                   "DESCRIPTION:Reminder\nTRIGGER:-PT15M\nEND:VALARM\n")
        if recurring:
            details += "RRULE:FREQ=WEEKLY;COUNT=4\n"
        source = source.replace("END:VEVENT", details + "END:VEVENT")
        self.put(path, source, login="jeff:password")
        assert len(sent) == 2
        sent.clear()
        if operation == "remove":
            updated = source.replace("ATTENDEE;CN=Alice:mailto:alice@example.com\n", "")
            self.put(path, updated, check=204, login="jeff:password")
            assert [recipients for recipients, _, _ in sent] == [["alice@example.com"]]
        elif operation == "cancel":
            updated = source.replace("STATUS:CONFIRMED", "STATUS:CANCELLED")
            self.put(path, updated, check=204, login="jeff:password")
        else:
            self.delete(path, login="jeff:password")
        if operation != "remove":
            assert [recipients for recipients, _, _ in sent] == [
                ["alice@example.com"], ["bob@example.com"]]
        for _, method, wire in sent:
            assert method == wire.method.value == "CANCEL"
            assert "valarm" not in wire.vevent.contents
            assert ("rrule" in wire.vevent.contents) == recurring
            if operation == "remove":
                assert "status" not in wire.vevent.contents
            else:
                assert wire.vevent.status.value == "CANCELLED"
        if operation == "cancel":
            sent.clear()
            self.put(path, updated, check=204, login="jeff:password")
            assert sent == []


class TestReplyToEndToEnd(BaseTest):
    """Exercise notification dispatch through serialization and SMTP envelope."""

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        import os
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("[allow all]\nuser: .*\ncollection: .*\npermissions: RrWw")
        self.configure({"rights": {"file": rights_file_path, "type": "from_file"}})
        self.configure({"auth": {"type": "none"}})
        self.calls: List[Tuple[str, List[str], str]] = []

    def _configure(self, *, enabled=True, owner_map='{"jeff": "jeff@example.com"}',
                   username=False, itip=True):
        options = {"type": "email", "dryrun": "False",
                   "itip_enabled": str(itip), "itip_owner_map": owner_map,
                   "itip_organizer_is_username": str(username),
                   "from_email": "calendar@example.com", "smtp_security": "none"}
        if enabled is not None:
            options["itip_owner_reply_to"] = str(enabled)
        self.configure({"hook": options})

    def _install_smtp(self, monkeypatch):
        import smtplib
        calls = self.calls

        class SMTP:
            def sendmail(self, from_addr, to_addrs, msg):
                calls.append((from_addr, to_addrs, msg))
                return {}

            def quit(self):
                pass

            def close(self):
                pass
        monkeypatch.setattr(smtplib, "SMTP", lambda **kwargs: SMTP())

    def _future_event(self, attendees, start, *, sequence=1):
        return _event(attendees, dtstart=start.strftime("%Y%m%dT%H%M%SZ"),
                      dtend=(start + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ"),
                      sequence=sequence)

    def _event_for(self, attendees, organizer="jeff@example.com", sequence=1,
                   start="20261113T200000Z"):
        return _event(attendees, sequence=sequence, dtstart=start).replace(
            "mailto:jeff@example.com", "mailto:%s" % organizer)

    def _assert_call(self, index, *, reply_to, method, recipient, sequence):
        from email import message_from_string, policy

        from radicale.hook.email.scheduling import parse_event
        sender, recipients, raw = self.calls[index]
        msg = message_from_string(raw, policy=policy.default)
        assert msg.get_all("Reply-To") == ([reply_to] if reply_to else None)
        assert msg["From"] == "calendar@example.com"
        assert sender == "calendar@example.com"
        assert recipients == [recipient]
        calendar = next(part for part in msg.walk()
                        if part.get_content_type() == "text/calendar")
        payload = calendar.get_payload(decode=True).decode()
        assert calendar.get_param("method") == method
        assert "METHOD:%s" % method in payload
        parsed = parse_event(payload)
        assert parsed is not None and parsed.sequence == sequence

    def test_request_update_removed_cancel_and_delete_cancel(self, monkeypatch):
        self._configure(owner_map='{"jeff": "jeff@reply.example"}')
        self._install_smtp(monkeypatch)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        path = "/calendar.ics/event.ics"
        start = datetime.now() + timedelta(days=2)

        def event(attendees, seq=1):
            return self._future_event(attendees, start, sequence=seq).replace(
                "mailto:jeff@example.com", "mailto:jeff@reply.example")
        self.put(path, event([ALICE, BOB]), login="jeff:password")
        self._assert_call(0, reply_to="jeff@reply.example", method="REQUEST",
                          recipient="alice@example.com", sequence=1)
        self._assert_call(1, reply_to="jeff@reply.example", method="REQUEST",
                          recipient="bob@example.com", sequence=1)
        self.put(path, event([ALICE, BOB], 2).replace("LOCATION:Home", "LOCATION:Office"),
                 check=204, login="jeff:password")
        self._assert_call(2, reply_to="jeff@reply.example", method="REQUEST",
                          recipient="alice@example.com", sequence=2)
        self._assert_call(3, reply_to="jeff@reply.example", method="REQUEST",
                          recipient="bob@example.com", sequence=2)
        self.put(path, event([ALICE], 3).replace("LOCATION:Home", "LOCATION:Office"),
                 check=204, login="jeff:password")
        _, _, stored = self.request("GET", path, check=200, login="jeff:password")
        assert int(vobject.readOne(stored).vevent.sequence.value) == 3
        # Member cancellation uses the stored revision; deletion advances it.
        self._assert_call(4, reply_to="jeff@reply.example", method="CANCEL",
                          recipient="bob@example.com", sequence=3)
        self.delete(path, login="jeff:password")
        self._assert_call(5, reply_to="jeff@reply.example", method="CANCEL",
                          recipient="alice@example.com", sequence=4)

    @pytest.mark.parametrize("template,organizer,reply_to", [
        ("{1}+tag@example.com", "mailto:jeff%2Btag@example.com", "jeff+tag@example.com"),
        ("{1}%2Btag@example.com", "mailto:jeff%252Btag@example.com", "jeff%2btag@example.com"),
    ])
    def test_regex_owner_reply_to_uses_bare_mailbox_after_expansion(
            self, monkeypatch, template, organizer, reply_to):
        import json

        self._configure(owner_map=json.dumps({"(jeff)": template}))
        self._install_smtp(monkeypatch)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        path = "/calendar.ics/encoded-owner.ics"
        source = self._future_event([ALICE], datetime.now() + timedelta(days=2)).replace(
            "mailto:jeff@example.com", organizer)
        self.put(path, source, login="jeff:password")
        self._assert_call(0, reply_to=reply_to, method="REQUEST",
                          recipient="alice@example.com", sequence=1)
        # A different URI spelling for the same organizer is not an update.
        equivalent = source.replace(organizer, organizer.replace("jeff", "j%65ff"))
        self.put(path, equivalent, check=204, login="jeff:password")
        assert len(self.calls) == 1
        _, _, stored = self.request("GET", path, check=200, login="jeff:password")
        assert int(vobject.readOne(stored).vevent.sequence.value) == 1

    def test_invalid_first_map_expansion_omits_reply_to_without_fallback(self, monkeypatch):
        import json

        self._configure(owner_map=json.dumps({
            "(.+)": "{1}?subject=extra", ".*": "fallback@example.com"}), username=True)
        self._install_smtp(monkeypatch)
        login = "jeff@example.com:password"
        self.mkcalendar("/calendar.ics/", login=login)
        source = self._future_event([ALICE], datetime.now() + timedelta(days=2))
        self.put("/calendar.ics/invalid-map.ics", source, login=login)
        assert len(self.calls) == 1
        self._assert_call(0, reply_to=None, method="REQUEST",
                          recipient="alice@example.com", sequence=1)

    def test_omitted_and_false_flag_emit_no_reply_to(self, monkeypatch):
        from email import message_from_string, policy

        for enabled in (None, False):
            self.calls = []
            self._configure(enabled=enabled)
            self._install_smtp(monkeypatch)
            self.mkcalendar("/flag-%s.ics/" % enabled, login="jeff:password")
            start = datetime.now() + timedelta(days=2)
            self.put("/flag-%s.ics/event.ics" % enabled,
                     self._future_event([ALICE], start), login="jeff:password")
            assert len(self.calls) == 1
            sender, recipients, raw = self.calls[0]
            msg = message_from_string(raw, policy=policy.default)
            assert msg.get_all("Reply-To") is None
            assert msg["From"] == "calendar@example.com"
            assert sender == "calendar@example.com"
            assert recipients == ["alice@example.com"]

    def test_missing_and_unmapped_username_authorization_omit_reply_to(self, monkeypatch):
        self._configure(enabled=True, owner_map="", username=True)
        self._install_smtp(monkeypatch)
        self.mkcalendar("/calendar.ics/", login="jeff@example.com:password")
        start = datetime.now() + timedelta(days=2)
        self.put("/calendar.ics/event.ics", self._future_event([ALICE], start),
                 login="jeff@example.com:password")
        assert len(self.calls) == 1
        self._assert_call(0, reply_to=None, method="REQUEST",
                          recipient="alice@example.com", sequence=1)

    def test_missing_unmapped_mismatch_and_attendee_copy_are_suppressed(self, monkeypatch):
        self._configure(enabled=True, owner_map='{"jeff": "jeff@example.com"}')
        self._install_smtp(monkeypatch)
        start = datetime.now() + timedelta(days=2)
        event = self._future_event([ALICE], start)
        # Missing actor: anonymous PUT, with no map identity to authorize it.
        self.mkcalendar("/anonymous.ics/")
        self.put("/anonymous.ics/missing.ics", event)
        # Unmapped authenticated actor in their own collection.
        self.mkcalendar("/mallory.ics/", login="mallory:password")
        self.put("/mallory.ics/unmapped.ics", event, login="mallory:password")
        # Mapped actor but organizer mismatch.
        self.mkcalendar("/jeff.ics/", login="jeff:password")
        self.put("/jeff.ics/mismatch.ics", event.replace(
            "mailto:jeff@example.com", "mailto:other@example.com"),
            login="jeff:password")
        # Attendee-copy organizer must not be authorized by the attendee.
        self.mkcalendar("/alice.ics/", login="alice:password")
        self.put("/alice.ics/copy.ics", event, login="alice:password")
        assert self.calls == []

    def test_legacy_reply_to_remains_global_with_flag_off_and_on(self, monkeypatch):
        from email import message_from_string, policy

        for enabled in (False, True):
            self.calls = []
            self._configure(enabled=enabled, itip=False)
            self._install_smtp(monkeypatch)
            self.mkcalendar("/legacy-%s.ics/" % enabled, login="jeff:password")
            start = datetime.now() + timedelta(days=2)
            self.put("/legacy-%s.ics/event.ics" % enabled,
                     self._future_event([ALICE], start), login="jeff:password")
            assert len(self.calls) == 1
            sender, recipients, raw = self.calls[0]
            msg = message_from_string(raw, policy=policy.default)
            assert msg["From"] == "calendar@example.com"
            assert msg.get_all("Reply-To") == ["calendar@example.com"]
            assert sender == "calendar@example.com"
            assert recipients == ["alice@example.com"]

    def test_sequential_actors_do_not_leak_reply_address(self, monkeypatch):
        self._configure(owner_map='{"jeff": "jeff@example.com", "bob": "bob@example.com", "unmapped": "unused@example.com"}',
                        username=True)
        self._install_smtp(monkeypatch)
        start = datetime.now() + timedelta(days=2)
        for actor, organizer, reply in (("jeff", "jeff@example.com", "jeff@example.com"),
                                        ("bob", "bob@example.com", "bob@example.com")):
            self.mkcalendar("/%s.ics/" % actor, login=actor + ":password")
            actor_event = self._future_event([ALICE], start).replace(
                "mailto:jeff@example.com", "mailto:%s" % organizer)
            self.put("/%s.ics/event.ics" % actor, actor_event,
                     login=actor + ":password")
        # An authenticated email username owns the event, but is unmapped.
        self.mkcalendar("/nobody@example.com.ics/", login="nobody@example.com:password")
        unmapped_event = self._future_event([ALICE], start).replace(
            "mailto:jeff@example.com", "mailto:nobody@example.com")
        self.put("/nobody@example.com.ics/event.ics",
                 unmapped_event, login="nobody@example.com:password")
        assert len(self.calls) == 3
        self._assert_call(0, reply_to="jeff@example.com", method="REQUEST",
                          recipient="alice@example.com", sequence=1)
        self._assert_call(1, reply_to="bob@example.com", method="REQUEST",
                          recipient="alice@example.com", sequence=1)
        self._assert_call(2, reply_to=None, method="REQUEST",
                          recipient="alice@example.com", sequence=1)


class TestHookWiringLegacy(BaseTest):
    """With itip_enabled off, the hook uses the legacy path, not the planner."""

    @pytest.mark.parametrize("prefix", ["", "mailto:"])
    @pytest.mark.parametrize("itip", [False, True])
    def test_quoted_mailbox_remains_deliverable_in_legacy_mode(self, monkeypatch, prefix, itip):
        from radicale.hook.email import EmailConfig

        self.configure({"auth": {"type": "none"},
                        "hook": {"itip_enabled": str(itip), "dryrun": "False",
                                 "itip_owner_map": '{"jeff": "jeff@example.com"}'}})
        sent = []

        def capture(config, text, to_addresses):
            sent.append(to_addresses)
            return True

        monkeypatch.setattr(EmailConfig, "_smtp_send", capture)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        source = TestHookWiring._future_event(self, [ALICE], datetime.now() + timedelta(days=2))
        address = '"guest.name"@example.com'
        source = source.replace("mailto:alice@example.com", prefix + address)
        self.put("/calendar.ics/quoted.ics", source, login="jeff:password")
        assert sent == ([] if itip else [[address]])

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        import os
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("[allow all]\nuser: .*\ncollection: .*\npermissions: RrWw")
        self.configure({
            "rights": {"file": rights_file_path, "type": "from_file"}})
        self.configure({
            "hook": {"type": "email", "dryrun": "True",
                     "itip_enabled": "False",
                     "from_email": "jeff@example.com"}})

    def test_put_adding_one_attendee_uses_legacy_send_email(self, caplog):
        caplog.set_level(logging.INFO)
        self.mkcalendar("/calendar.ics/")
        path = "/calendar.ics/event1.ics"
        start = (datetime.now() + timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
        end = (datetime.now() + timedelta(days=1, hours=2)).strftime("%Y%m%dT%H%M%SZ")
        event = _event([ALICE, BOB], dtstart=start, dtend=end)
        self.put(path, event)
        legacy = [log for log in caplog.messages
                  if "DRY-RUN _send_email" in log]
        itip = [log for log in caplog.messages
                if "DRY-RUN send_scheduling_email" in log]
        assert legacy, "legacy _send_email should be used when itip_enabled is False"
        assert not itip


class TestOwnerMapAndRecurrence:
    """Explicit mapping, recurrence cases, config validation."""

    def test_parse_owner_map_valid(self):
        from radicale.hook.email.scheduling import parse_owner_map
        assert parse_owner_map('{"alice": "alice@example.com", " Bob ": "Bob@Example.com"}') == {
            "alice": "alice@example.com", "Bob": "bob@example.com"}
        assert parse_owner_map("{}") == {}
        assert parse_owner_map(None) == {}
        assert parse_owner_map({"Alice": "Alice@Example.com"}) == {
            "Alice": "alice@example.com"}

    def test_regex_owner_map_scenarios(self):
        from radicale.hook.email.scheduling import (parse_owner_map,
                                                    resolve_owner_identities)
        cases = [
            ({"(.+)@(.+)": "{1}@{2}"}, "User1@Domain.Example", "user1@domain.example"),
            ({"(user1|user2|user3)@(.+)": "{1}@{2}"}, "user2@domain.example", "user2@domain.example"),
            ({r"(user3|user4)@(otherdomain\.example)": "{1}@{2}"}, "user4@otherdomain.example", "user4@otherdomain.example"),
            ({"(.+)": "{1}@domain.example"}, "user1", "user1@domain.example"),
            ({"(user1|user2)": "{1}@domain.example", "(user3|user4)": "{1}@otherdomain.example"}, "user3", "user3@otherdomain.example"),
            ({"(.+)@(.+)": "{0}"}, "user1@domain.example", "user1@domain.example"),
            ({r"(\S+)": "{1}@domain.example"}, "user1", "user1@domain.example"),
            ({r"(\s+)": "whitespace@example.com", r"(\S+)": "{1}@domain.example"}, "user1", "user1@domain.example"),
        ]
        for rules, actor, expected in cases:
            assert resolve_owner_identities(actor, parse_owner_map(rules)) == [expected]

    def test_regex_owner_map_unicode_case_insensitive(self):
        from radicale.hook.email.scheduling import (mapped_owner_email,
                                                    owner_addresses,
                                                    resolve_owner_identities)

        # Lowercasing U+0130 expands it into two code points, preventing
        # fullmatch even though re.IGNORECASE matches it against "i".
        rules = {"i": "owner@example.com"}
        assert mapped_owner_email("İ", rules) == "owner@example.com"
        assert resolve_owner_identities("İ", rules) == ["owner@example.com"]
        assert owner_addresses("İ", '{"i": "owner@example.com"}') == ["owner@example.com"]

    def test_regex_owner_map_fullmatch_and_selection(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        rules = {"user1|user2": "selected@example.com"}
        for actor in ["prefixuser1", "user2suffix", "user3", "user1\nextra"]:
            assert resolve_owner_identities(actor, rules) == []
        assert resolve_owner_identities("user2", rules) == ["selected@example.com"]
        domain_rules = {r"(user3|user4)@(otherdomain\.example)": "{1}@{2}"}
        assert resolve_owner_identities("user3@otherdomainXexample", domain_rules) == []

    def test_regex_owner_map_first_match_wins(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        rules = {"alice": "special@example.com", "(.+)": "{1}@example.com"}
        assert resolve_owner_identities("alice", rules) == ["special@example.com"]
        assert resolve_owner_identities("bob", rules) == ["bob@example.com"]

    def test_regex_owner_map_invalid_rules(self):
        import pytest

        from radicale.hook.email.scheduling import parse_owner_map
        for pattern, template in [
                ("(", "a@example.com"), ("a", "{1}@example.com"),
                ("(a)", "{2}@example.com"), ("a", "{name}@example.com"),
                ("a", "{0.__class__}@example.com"), ("a", "{0!r}@example.com"),
                ("a", "{0:>10}@example.com"), ("a", "{0@example.com")]:
            with pytest.raises(ValueError, match="invalid itip_owner_map rule"):
                parse_owner_map({pattern: template})

    def test_regex_owner_map_invalid_address_fails_closed(self):
        from radicale.hook.email.scheduling import (parse_owner_map,
                                                    resolve_owner_identities)
        rules = parse_owner_map({"(.+)": "{1}@example.com"})
        for actor in ["a@b", "a b", "a\rb", "a\nb"]:
            assert resolve_owner_identities(actor, rules) == []
        # An invalid first match must not fall through to a broader rule.
        rules = {"(.+)": "{1}@bad@example.com", "alice": "safe@example.com"}
        assert resolve_owner_identities("alice", rules) == []

    @pytest.mark.parametrize("address", [
        "mailto:jeff@example.com", ".jeff@example.com", "jeff@example..com",
        "jeff@localhost", '"jeff"@example.com', "jeff@-example.com",
        "jeff@exam_ple.com", "jéff@example.com", "a" * 65 + "@example.com",
        "jeff\r\nextra@example.com",
    ])
    def test_owner_outputs_require_supported_bare_mailboxes(self, address):
        import json

        from radicale.hook.email.scheduling import (mapped_owner_email,
                                                    owner_addresses,
                                                    parse_owner_map,
                                                    resolve_owner_identities,
                                                    username_email)

        with pytest.raises(ValueError, match="invalid itip_owner_map rule"):
            parse_owner_map({"jeff": address})
        rules = parse_owner_map({r"([\s\S]+)": "{1}", ".*": "fallback@example.com"})
        assert mapped_owner_email(address, rules) is None
        assert username_email(address) is None
        assert resolve_owner_identities(address, rules, True) == []
        assert owner_addresses(address, json.dumps(rules), True) == []

    def test_regex_owner_map_shared_by_principal_and_reply_to(self):
        from types import SimpleNamespace

        from radicale.hook.email import Hook
        from radicale.hook.email.scheduling import (owner_addresses,
                                                    parse_owner_map)
        rules = '{"(alice|bob)": "{1}@example.com"}'
        hook = object.__new__(Hook)
        hook.email_config = SimpleNamespace(itip_owner_reply_to=True)
        hook.owner_map = parse_owner_map(rules)
        assert owner_addresses("Alice", rules) == ["alice@example.com"]
        assert hook._owner_reply_to(SimpleNamespace(actor="Alice")) == "alice@example.com"
        assert hook._owner_reply_to(SimpleNamespace(actor="mallory")) is None

    def test_parse_owner_map_malformed(self):
        import pytest

        from radicale.hook.email.scheduling import parse_owner_map
        for bad in ["{bad", "[]", "null", '{"alice": 3}', '{"alice": "no-at-sign"}', '{"": "a@x.com"}', '{"a": "a@x.com", " a ": "b@x.com"}', '{"a": "a@x.com", "a": "b@x.com"}']:
            with pytest.raises(ValueError):
                parse_owner_map(bad)

    def test_resolve_owner_identities_mapped_and_unmapped(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        m = {"alice": "alice@example.com"}
        assert resolve_owner_identities("alice", m) == ["alice@example.com"]
        assert resolve_owner_identities("someone-else", m) == []
        assert resolve_owner_identities(None, m) == []
        assert resolve_owner_identities("jeff", {}) == []  # map is mandatory
        assert resolve_owner_identities(None, {}) == []

    def test_mapped_owner_plans_and_unmapped_owner_plans_nothing(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB, CAROL])
        # The test event is organized by jeff@example.com, so the mapped
        # identity must be that address for ownership to be proved.
        mapped = resolve_owner_identities("jeff", {"jeff": "jeff@example.com"})
        assert _recipients(plan(old, new, owner=mapped)) == [("REQUEST", "carol@example.com", "added")]
        unmapped = resolve_owner_identities("mallory", {"jeff": "jeff@example.com"})
        assert plan(old, new, owner=unmapped) == []

    def test_uid_change_plans_nothing(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB, CAROL]).replace("UID:pizza-1", "UID:other-1")
        assert plan(old, new) == []

    def test_organizer_change_plans_nothing(self):
        old = _event([ALICE, BOB])
        new = _event([ALICE, BOB, CAROL]).replace("mailto:jeff@example.com", "mailto:other@example.com")
        assert plan(old, new) == []

    def test_multi_uid_calendar_plans_nothing(self):
        old = _event([ALICE, BOB])
        extra = ("BEGIN:VEVENT\nUID:second-1\nDTSTAMP:20261030T120000Z\n"
                 "DTSTART:20261114T200000Z\nDTEND:20261114T220000Z\nSUMMARY:Other\n"
                 "ORGANIZER:mailto:jeff@example.com\n"
                 "ATTENDEE;CN=Alice:mailto:alice@example.com\nSEQUENCE:1\nEND:VEVENT\n")
        new = _event([ALICE, BOB, CAROL]).replace("END:VCALENDAR", extra + "END:VCALENDAR")
        assert plan(old, new) == []

    def test_override_with_different_attendees_plans_nothing(self):
        old = _event([ALICE, BOB, CAROL])
        override = ("BEGIN:VEVENT\nUID:pizza-1\nDTSTAMP:20261030T120000Z\n"
                    "RECURRENCE-ID:20261120T200000Z\nDTSTART:20261120T200000Z\n"
                    "DTEND:20261120T220000Z\nSUMMARY:Pizza Party\n"
                    "ORGANIZER:mailto:jeff@example.com\n"
                    "ATTENDEE;CN=Alice:mailto:alice@example.com\nSEQUENCE:1\nEND:VEVENT\n")
        new = old.replace("END:VCALENDAR", override + "END:VCALENDAR")
        # Same master attendees, but the override drops Bob and Carol:
        # a per-instance change, explicitly unsupported.
        assert plan(old, new) == []

    def test_override_with_same_attendees_now_plans_nothing(self):
        # Multi-VEVENT objects are rejected entirely, even
        # when the override's attendee set matches the master.
        old = _event([ALICE, BOB])
        override = ("BEGIN:VEVENT\nUID:pizza-1\nDTSTAMP:20261030T120000Z\n"
                    "RECURRENCE-ID:20261120T200000Z\nDTSTART:20261120T200000Z\n"
                    "DTEND:20261120T220000Z\nSUMMARY:Pizza Party\n"
                    "ORGANIZER:mailto:jeff@example.com\n"
                    "ATTENDEE;CN=Alice:mailto:alice@example.com\n"
                    "ATTENDEE;CN=Bob:mailto:bob@example.com\nSEQUENCE:1\nEND:VEVENT\n")
        new = _event([ALICE, BOB], dtstart="20261113T210000Z").replace(
            "END:VCALENDAR", override + "END:VCALENDAR")
        assert plan(old, new) == []

    def test_unsupported_itip_method_raises(self):
        import pytest

        from radicale.hook.email.scheduling import build_itip_ics
        with pytest.raises(ValueError):
            build_itip_ics(_event([ALICE]), "PUBLISH", "alice@example.com")

    def test_mass_email_and_itip_is_config_error(self):
        import pytest

        from radicale.hook.email import EmailConfig, MessageTemplate
        cfg = EmailConfig(host="smtp.example.com", port=587, security="starttls",
                          ssl_verify_mode="REQUIRED", username="", password="",
                          from_email="jeff@example.com", send_mass_emails=True,
                          dryrun=True,
                          new_or_added_to_event_template=MessageTemplate(subject="x", body="y"),
                          deleted_or_removed_from_event_template=MessageTemplate(subject="x", body="y"),
                          updated_event_template=MessageTemplate(subject="x", body="y"),
                          itip_enabled=True, itip_owner_map='{"jeff": "jeff@example.com"}')
        with pytest.raises(ValueError):
            cfg.validate_itip_config()

    def test_itip_message_round_trips_through_stdlib_parser(self):
        # Check MIME method, calendar METHOD and recipient isolation after
        # serializing and parsing with the standard-library email parser.
        import email
        from email import policy
        new = _event([ALICE, CAROL])
        itip = build_itip_ics(new, "REQUEST", "carol@example.com")
        message = build_itip_message("jeff@example.com", "carol@example.com",
                                     "Added", "Hello Carol", itip, "REQUEST")
        parsed = email.message_from_string(message, policy=policy.default)
        parts = list(parsed.walk())
        cal = [p for p in parts if p.get_content_type() == "text/calendar"]
        assert len(cal) == 1
        assert cal[0].get_param("method") == "REQUEST"
        payload = cal[0].get_payload(decode=True).decode()
        assert "METHOD:REQUEST" in payload
        assert "carol@example.com" in payload
        assert "alice@example.com" not in payload


class TestMandatoryMapAndMasterParsing:
    """Mandatory map, master-only parsing, multi-VEVENT rejection."""

    def test_parse_event_selects_master_not_first_vevent(self):
        from radicale.hook.email.scheduling import parse_event
        override = ("BEGIN:VEVENT\nUID:pizza-1\nDTSTAMP:20261030T120000Z\n"
                    "RECURRENCE-ID:20261120T200000Z\nDTSTART:20261120T200000Z\n"
                    "DTEND:20261120T220000Z\nSUMMARY:Override\n"
                    "ORGANIZER:mailto:jeff@example.com\n"
                    "ATTENDEE;CN=Alice:mailto:alice@example.com\nSEQUENCE:1\nEND:VEVENT\n")
        # Override first, master second: parse_event must return the master.
        master = _event([ALICE, BOB])
        ics = master.replace("BEGIN:VEVENT", override + "BEGIN:VEVENT", 1)
        parsed = parse_event(ics)
        assert parsed is not None
        assert parsed.uid == "pizza-1"
        assert set(parsed.attendees) == {"alice@example.com", "bob@example.com"}

    def test_parse_event_no_master_returns_none(self):
        from radicale.hook.email.scheduling import parse_event
        override_only = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//EN\n"
                         "BEGIN:VEVENT\nUID:pizza-1\nDTSTAMP:20261030T120000Z\n"
                         "RECURRENCE-ID:20261120T200000Z\nDTSTART:20261120T200000Z\n"
                         "DTEND:20261120T220000Z\nSUMMARY:Override\n"
                         "ORGANIZER:mailto:jeff@example.com\n"
                         "ATTENDEE;CN=Alice:mailto:alice@example.com\nSEQUENCE:1\nEND:VEVENT\n"
                         "END:VCALENDAR\n")
        assert parse_event(override_only) is None

    def test_multi_vevent_created_event_plans_nothing(self):
        extra = ("BEGIN:VEVENT\nUID:second-1\nDTSTAMP:20261030T120000Z\n"
                 "DTSTART:20261114T200000Z\nDTEND:20261114T220000Z\nSUMMARY:Other\n"
                 "ORGANIZER:mailto:jeff@example.com\n"
                 "ATTENDEE;CN=Alice:mailto:alice@example.com\nSEQUENCE:1\nEND:VEVENT\n")
        new = _event([ALICE, BOB]).replace("END:VCALENDAR", extra + "END:VCALENDAR")
        assert plan(None, new) == []

    def test_itip_without_owner_map_is_config_error(self):
        import pytest

        from radicale.hook.email import EmailConfig, MessageTemplate
        cfg = EmailConfig(host="smtp.example.com", port=587, security="starttls",
                          ssl_verify_mode="REQUIRED", username="", password="",
                          from_email="jeff@example.com", send_mass_emails=False,
                          dryrun=True,
                          new_or_added_to_event_template=MessageTemplate(subject="x", body="y"),
                          deleted_or_removed_from_event_template=MessageTemplate(subject="x", body="y"),
                          updated_event_template=MessageTemplate(subject="x", body="y"),
                          itip_enabled=True, itip_owner_map="")
        with pytest.raises(ValueError):
            cfg.validate_itip_config()
        cfg.itip_owner_map = '{"jeff": "jeff@example.com"}'
        cfg.validate_itip_config()  # no error with an explicit map
        cfg.itip_owner_map = "{}"
        cfg.itip_organizer_is_username = True
        cfg.validate_itip_config()  # an empty JSON object is valid with username ownership

    def test_itip_disabled_without_map_is_allowed(self):
        from radicale.hook.email import EmailConfig, MessageTemplate
        cfg = EmailConfig(host="smtp.example.com", port=587, security="starttls",
                          ssl_verify_mode="REQUIRED", username="", password="",
                          from_email="jeff@example.com", send_mass_emails=False,
                          dryrun=True,
                          new_or_added_to_event_template=MessageTemplate(subject="x", body="y"),
                          deleted_or_removed_from_event_template=MessageTemplate(subject="x", body="y"),
                          updated_event_template=MessageTemplate(subject="x", body="y"),
                          itip_enabled=False, itip_owner_map="")
        cfg.validate_itip_config()  # legacy mode needs no map


class TestBuilderGuardAndCancelSequence:
    """No bare-username fallback, builder guard, CANCEL sequence."""

    def test_cancel_sequence_uses_explicit_sequence(self):
        old = _event([ALICE, BOB, CAROL], sequence=3)
        itip = build_itip_ics(old, "CANCEL", "carol@example.com", sequence=6)
        assert "SEQUENCE:6" in itip
        # Without an explicit sequence, the old behaviour remains: source + 1.
        itip = build_itip_ics(old, "CANCEL", "carol@example.com")
        assert "SEQUENCE:4" in itip

    @pytest.mark.parametrize("sequence,expected", [(None, 1), (7, 7)])
    def test_cancel_serializes_a_sequence_absent_from_source(self, sequence, expected):
        source = _event([ALICE], sequence=0).replace("SEQUENCE:0\n", "")
        wire = vobject.readOne(build_itip_ics(
            source, "CANCEL", "alice@example.com", sequence=sequence))
        assert wire.method.value == "CANCEL"
        assert len(wire.vevent.sequence_list) == 1
        assert int(wire.vevent.sequence.value) == expected

    def test_preparation_serializes_a_sequence_absent_from_upload(self):
        from radicale.hook.email.scheduling import prepare_itip_upload

        source = _event([ALICE], sequence=0).replace("SEQUENCE:0\n", "")
        assert prepare_itip_upload(source, source, OWNER) is None
        updated = source.replace("LOCATION:Home", "LOCATION:Away")
        prepared = prepare_itip_upload(source, updated, OWNER)
        assert prepared is not None
        stored = vobject.readOne(prepared)
        assert len(stored.vevent.sequence_list) == 1
        assert int(stored.vevent.sequence.value) == 1
        assert stored.vevent.location.value == "Away"

    @pytest.mark.parametrize("sequence", [-1, 2147483648])
    def test_builder_rejects_out_of_range_cancellation_revision(self, sequence):
        with pytest.raises(ValueError, match="SEQUENCE"):
            build_itip_ics(_event([ALICE]), "CANCEL", "alice@example.com",
                           sequence=sequence)

    def test_builder_rejects_exhausted_deletion_revision(self):
        with pytest.raises(ValueError, match="SEQUENCE"):
            build_itip_ics(_event([ALICE], sequence=2147483647),
                           "CANCEL", "alice@example.com")


class TestStoredSchedulingRevision(BaseTest):
    """Persist the revision used by actual outgoing scheduling messages."""

    def setup_method(self):
        TestHookWiring.setup_method(self)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        self.path = "/calendar.ics/revisions.ics"
        self.start = datetime.now() + timedelta(days=1)

    def _source(self, attendees, sequence):
        return TestHookWiring._future_event(self, attendees, self.start).replace(
            "SEQUENCE:1", "SEQUENCE:%d" % sequence)

    def _capture(self, monkeypatch):
        from email import message_from_string, policy

        from radicale.hook.email import EmailConfig

        sent = []

        def capture_smtp(config, text, to_addresses):
            message = message_from_string(text, policy=policy.default)
            calendar = [part for part in message.walk()
                        if part.get_content_type() == "text/calendar"]
            assert len(calendar) == 1
            sent.append(vobject.readOne(calendar[0].get_payload(decode=True).decode("utf-8")))
            return True

        monkeypatch.setattr(EmailConfig, "_smtp_send", capture_smtp)
        self.configure({"hook": {"dryrun": "False"}})
        return sent

    def _stored(self):
        _, headers, body = self.request("GET", self.path, check=200, login="jeff:password")
        from radicale.item import get_etag
        assert headers["ETag"] == get_etag(body)
        return headers["ETag"], vobject.readOne(body)

    @pytest.mark.parametrize("operation", ["details", "remove", "cancel", "delete"])
    def test_implicit_zero_revision_reaches_stored_and_wire_updates(self, monkeypatch, operation):
        sent = self._capture(monkeypatch)
        source = self._source([ALICE], 0).replace("SEQUENCE:0\n", "")
        self.put(self.path, source, login="jeff:password")
        original_etag, stored = self._stored()
        assert "sequence" not in stored.vevent.contents
        assert len(sent) == 1 and sent[0].method.value == "REQUEST"
        assert "sequence" not in sent[0].vevent.contents
        sent.clear()

        if operation == "delete":
            self.delete(self.path, login="jeff:password")
            self.get(self.path, check=404, login="jeff:password")
        else:
            if operation == "details":
                updated = source.replace("LOCATION:Home", "LOCATION:Away")
            elif operation == "remove":
                updated = self._source([], 0).replace("SEQUENCE:0\n", "")
            else:
                updated = source.replace("END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT")
            _, headers, _ = self.request(
                "PUT", self.path, updated, check=204,
                login="jeff:password", http_if_match=original_etag)
            assert "ETag" not in headers
            current_etag, stored = self._stored()
            assert current_etag != original_etag
            assert int(stored.vevent.sequence.value) == 1
        assert len(sent) == 1
        assert sent[0].method.value == ("REQUEST" if operation == "details" else "CANCEL")
        assert int(sent[0].vevent.sequence.value) == 1

        if operation in ("remove", "cancel"):
            sent.clear()
            self.put(self.path, source, check=204, login="jeff:password")
            _, stored = self._stored()
            assert int(stored.vevent.sequence.value) == 2
            assert len(sent) == 1 and sent[0].method.value == "REQUEST"
            assert int(sent[0].vevent.sequence.value) == 2

    @pytest.mark.parametrize("change,expected", [("none", 5), ("response", 5), ("details", 6)])
    def test_omitted_sequence_preserves_existing_revision_floor(self, monkeypatch, change, expected):
        sent = self._capture(monkeypatch)
        source = self._source([("Alice", "alice@example.com", ";PARTSTAT=NEEDS-ACTION")], 5)
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = self._stored()
        sent.clear()
        updated = source.replace("SEQUENCE:5\n", "")
        if change == "response":
            updated = updated.replace("PARTSTAT=NEEDS-ACTION", "PARTSTAT=ACCEPTED")
        elif change == "details":
            updated = updated.replace("LOCATION:Home", "LOCATION:Away")
        _, headers, _ = self.request("PUT", self.path, updated, check=204,
                                     login="jeff:password", http_if_match=old_etag)
        assert "ETag" not in headers
        new_etag, stored = self._stored()
        assert (new_etag == old_etag) == (change == "none")
        assert int(stored.vevent.sequence.value) == expected
        assert len(sent) == (1 if change == "details" else 0)
        if change == "response":
            assert stored.vevent.attendee.params["PARTSTAT"] == ["ACCEPTED"]
        if sent:
            assert int(sent[0].vevent.sequence.value) == expected

    @pytest.mark.parametrize("supplied,expected", [
        ((1, 1), (2, 3)), ((2, 2), (2, 3)), ((8, 13), (8, 13))])
    def test_remove_readd_persists_strictly_increasing_wire_revision(
            self, monkeypatch, supplied, expected):
        sent = self._capture(monkeypatch)
        self.put(self.path, self._source([ALICE], 1), login="jeff:password")
        previous_etag, _ = self._stored()
        for attendees, sequence, revision in zip(([], [ALICE]), supplied, expected):
            _, headers, _ = self.request(
                "PUT", self.path, self._source(attendees, sequence), check=204,
                login="jeff:password", http_if_match=previous_etag)
            if sequence != revision:
                assert "ETag" not in headers
            current_etag, stored = self._stored()
            assert current_etag != previous_etag
            assert int(stored.vevent.sequence.value) == revision
            assert int(sent[-1].vevent.sequence.value) == revision
            previous_etag = current_etag
        assert [message.method.value for message in sent] == ["REQUEST", "CANCEL", "REQUEST"]
        assert [int(message.vevent.sequence.value) for message in sent] == [1, *expected]

    @pytest.mark.parametrize("supplied,expected", [
        ((1, 1), (2, 3)), ((5, 9), (5, 9))])
    def test_cancel_reactivate_persists_transmitted_revision(
            self, monkeypatch, supplied, expected):
        sent = self._capture(monkeypatch)
        self.put(self.path, self._source([ALICE], 1), login="jeff:password")
        for status, sequence, revision in zip(("CANCELLED", "CONFIRMED"), supplied, expected):
            source = self._source([ALICE], sequence).replace(
                "END:VEVENT", "STATUS:%s\nEND:VEVENT" % status)
            self.put(self.path, source, check=204, login="jeff:password")
            _, stored = self._stored()
            assert int(stored.vevent.sequence.value) == revision
            assert int(sent[-1].vevent.sequence.value) == revision
        assert [message.method.value for message in sent] == ["REQUEST", "CANCEL", "REQUEST"]

    @pytest.mark.parametrize("supplied,expected", [(1, 5), (9, 9)])
    def test_response_only_save_preserves_revision_floor_without_mail(
            self, monkeypatch, supplied, expected):
        sent = self._capture(monkeypatch)
        old = self._source([("Alice", "alice@example.com", ";PARTSTAT=NEEDS-ACTION")], 5)
        self.put(self.path, old, login="jeff:password")
        sent.clear()
        updated = old.replace("PARTSTAT=NEEDS-ACTION", "PARTSTAT=ACCEPTED").replace(
            "SEQUENCE:5", "SEQUENCE:%d" % supplied)
        self.put(self.path, updated, check=204, login="jeff:password")
        _, stored = self._stored()
        assert int(stored.vevent.sequence.value) == expected
        assert stored.vevent.attendee.params["PARTSTAT"] == ["ACCEPTED"]
        assert sent == []

    def test_client_owned_edit_keeps_higher_supplied_revision(self, monkeypatch):
        sent = self._capture(monkeypatch)
        source = self._source([("Alice", "alice@example.com", ";SCHEDULE-AGENT=CLIENT")], 5)
        self.put(self.path, source, login="jeff:password")
        updated = source.replace("SEQUENCE:5", "SEQUENCE:9").replace("LOCATION:Home", "LOCATION:Away")
        self.put(self.path, updated, check=204, login="jeff:password")
        _, stored = self._stored()
        assert int(stored.vevent.sequence.value) == 9
        assert sent == []

    def test_stale_sequence_only_save_keeps_etag_and_sends_nothing(self, monkeypatch):
        sent = self._capture(monkeypatch)
        source = self._source([ALICE], 5)
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = self._stored()
        sent.clear()
        _, headers, _ = self.request("PUT", self.path, source.replace("SEQUENCE:5", "SEQUENCE:1"),
                                     check=204, login="jeff:password", http_if_match=old_etag)
        assert "ETag" not in headers
        new_etag, stored = self._stored()
        assert new_etag == old_etag
        assert int(stored.vevent.sequence.value) == 5
        assert sent == []

    @pytest.mark.parametrize("case", ["disabled", "none", "other-owner", "overrides"])
    def test_revision_normalization_is_scoped_to_owned_supported_itip_events(self, case):
        source = self._source([ALICE], 5)
        if case == "disabled":
            self.configure({"hook": {"itip_enabled": "False"}})
        elif case == "none":
            self.configure({"hook": {"type": "none"}})
        elif case == "other-owner":
            source = source.replace("mailto:jeff@example.com", "mailto:other@example.com")
        else:
            calendar = vobject.readOne(source)
            override = vobject.readOne(source).vevent
            override.add("recurrence-id").value = override.dtstart.value
            calendar.add(override)
            source = calendar.serialize()
        self.put(self.path, source, login="jeff:password")
        self.put(self.path, source.replace("SEQUENCE:5", "SEQUENCE:1"),
                 check=204, login="jeff:password")
        _, stored = self._stored()
        assert int(stored.vevent.sequence.value) == 1

    def test_exhausted_revision_rejects_edit_before_storage(self, monkeypatch):
        sent = self._capture(monkeypatch)
        source = self._source([ALICE], 2147483647)
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = self._stored()
        sent.clear()
        self.put(self.path, source.replace("LOCATION:Home", "LOCATION:Away"),
                 check=400, login="jeff:password")
        new_etag, stored = self._stored()
        assert new_etag == old_etag
        assert stored.vevent.location.value == "Home"
        assert sent == []

    def test_exhausted_revision_delete_sends_no_invalid_message(self, monkeypatch, caplog):
        sent = self._capture(monkeypatch)
        self.put(self.path, self._source([ALICE], 2147483647), login="jeff:password")
        sent.clear()
        self.delete(self.path, login="jeff:password")
        self.get(self.path, check=404, login="jeff:password")
        assert sent == []
        assert any("CANCEL SEQUENCE exceeds" in message for message in caplog.messages)

    def test_preparation_failure_does_not_commit_or_notify(self, monkeypatch):
        from radicale.hook.email import Hook

        sent = self._capture(monkeypatch)
        source = self._source([ALICE], 1)
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = self._stored()
        sent.clear()

        def fail_prepare(hook, notification):
            raise RuntimeError("test preparation failure")

        monkeypatch.setattr(Hook, "prepare_upload", fail_prepare)
        self.put(self.path, source.replace("LOCATION:Home", "LOCATION:Away"),
                 check=500, login="jeff:password")
        new_etag, stored = self._stored()
        assert new_etag == old_etag
        assert stored.vevent.location.value == "Home"
        assert sent == []

    def test_legacy_hook_without_preparation_method_still_works(self):
        from types import SimpleNamespace
        from typing import Any, cast

        notices = []
        self.application._hook = cast(Any, SimpleNamespace(enabled=True, notify=notices.append))
        self.put(self.path, self._source([ALICE], 1), login="jeff:password")
        assert len(notices) == 1

    def test_default_hook_preparation_preserves_input(self):
        from radicale.hook import (BaseHook, HookNotificationItem,
                                   HookNotificationItemTypes)

        notice = HookNotificationItem(HookNotificationItemTypes.UPSERT, self.path,
                                      new_content=self._source([ALICE], 1))
        assert BaseHook(self.configuration).prepare_upload(notice) is None

    @pytest.mark.parametrize("replacement", ["malformed", "different-uid", "too-large"])
    def test_invalid_preparation_is_rejected_before_storage(self, monkeypatch, replacement):
        from radicale.hook.email import Hook

        source = self._source([ALICE], 1)
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = self._stored()
        if replacement == "malformed":
            result = "not an iCalendar object"
            expected = 500
        elif replacement == "different-uid":
            result = source.replace("UID:pizza-1", "UID:other")
            expected = 400
        else:
            result = source.replace("SUMMARY:Pizza Party", "SUMMARY:" + "x" * 5000)
            self.configure({"server": {"max_resource_size": "4096"}})
            expected = 412
        monkeypatch.setattr(Hook, "prepare_upload", lambda hook, notice: result)
        self.put(self.path, source, check=expected, login="jeff:password")
        new_etag, stored = self._stored()
        assert new_etag == old_etag
        assert stored.vevent.uid.value == "pizza-1"

    def test_stale_etag_fails_before_preparation(self, monkeypatch):
        from radicale.hook.email import Hook

        self.put(self.path, self._source([ALICE], 1), login="jeff:password")

        def fail_if_prepared(hook, notice):
            raise AssertionError("preparation ran before If-Match validation")

        monkeypatch.setattr(Hook, "prepare_upload", fail_if_prepared)
        self.put(self.path, self._source([ALICE], 2), check=412,
                 login="jeff:password", http_if_match='"stale"')


class TestCalendarAddressWiring(BaseTest):
    def setup_method(self):
        TestStoredSchedulingRevision.setup_method(self)

    @pytest.mark.parametrize("itip_enabled", [False, True])
    @pytest.mark.parametrize("encoded,expected", [
        ("alice%2Btag", "alice+tag@example.com"),
        ("alice%252Btag", "alice%2btag@example.com"),
        ("alice%3Ftag", "alice?tag@example.com"),
        ("alice%23tag", "alice#tag@example.com"),
    ])
    def test_encoded_calendar_addresses_use_canonical_smtp_recipient(
            self, monkeypatch, itip_enabled, encoded, expected):
        from email import message_from_string, policy

        from radicale.hook.email import EmailConfig

        sent = []

        def capture_smtp(config, text, to_addresses):
            sent.append((to_addresses, message_from_string(text, policy=policy.default)))
            return True

        monkeypatch.setattr(EmailConfig, "_smtp_send", capture_smtp)
        self.configure({"hook": {
            "dryrun": "False", "itip_enabled": itip_enabled,
            "from_email": "jeff+tag@example.com",
            "itip_owner_map": '{"jeff": "jeff+tag@example.com"}'}})
        source = TestStoredSchedulingRevision._source(
            self, [("Alice", encoded + "@example.com", "")], 1).replace(
                "mailto:jeff@example.com", "MAILTO:JEFF%2BTag@EXAMPLE.COM")
        self.put(self.path, source, login="jeff:password")
        assert len(sent) == 1
        recipients, message = sent[0]
        assert recipients == [expected]
        if itip_enabled:
            assert str(message["To"]) == expected
            calendars = [part for part in message.walk()
                         if part.get_content_type() == "text/calendar"]
            assert len(calendars) == 1
            wire = vobject.readOne(calendars[0].get_payload(decode=True).decode())
            assert extract_email(wire.vevent.organizer.value) == "jeff+tag@example.com"
            assert extract_email(wire.vevent.attendee.value) == expected

    @pytest.mark.parametrize("case,original,replacement", [
        item for item in _unsupported_identity_cases()
        if item[0] in ("organizer-bare", "attendee-bare", "other-attendee-bare",
                       "other-attendee-query", "other-attendee-SENT-BY",
                       "duplicate-attendee", "encoded-duplicate-attendee")])
    def test_unsupported_identity_does_not_trigger_revisions_or_cancellation(
            self, monkeypatch, case, original, replacement):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        supported = TestStoredSchedulingRevision._source(self, [ALICE, BOB], 5)
        unsupported = supported.replace(original, replacement).replace(
            "SEQUENCE:5", "SEQUENCE:1").replace("LOCATION:Home", "LOCATION:Away")
        self.put(self.path, unsupported, login="jeff:password")
        assert sent == [], case
        self.put(self.path, supported, check=204, login="jeff:password")
        assert sent == [], case
        _, headers, _ = self.request("PUT", self.path, unsupported, check=204,
                                     login="jeff:password")
        assert "ETag" in headers
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == 1
        assert stored.vevent.location.value == "Away"
        assert sent == [], case
        self.delete(self.path, login="jeff:password")
        self.get(self.path, check=404, login="jeff:password")
        assert sent == [], case

    def test_legacy_notifications_keep_bare_calendar_mailboxes(self, monkeypatch):
        from radicale.hook.email import EmailConfig

        sent = []

        def capture_smtp(config, text, to_addresses):
            sent.extend(to_addresses)
            return True

        monkeypatch.setattr(EmailConfig, "_smtp_send", capture_smtp)
        self.configure({"hook": {"dryrun": "False", "itip_enabled": "False"}})
        source = TestStoredSchedulingRevision._source(self, [ALICE, BOB], 1).replace(
            "mailto:alice@example.com", "alice@example.com")
        self.put(self.path, source, login="jeff:password")
        assert sent == ["alice@example.com", "bob@example.com"]


class TestOrganizerAttendeeChangesWiring(BaseTest):
    def setup_method(self):
        TestStoredSchedulingRevision.setup_method(self)

    def _source(self, parameters, sequence=5):
        return TestStoredSchedulingRevision._source(
            self, [("Alice", "alice@example.com", parameters), BOB], sequence)

    @pytest.mark.parametrize("previous,supplied,expected", [
        (5, 3, 5), (5, 5, 5), (5, 9, 9), (2147483647, 2147483647, 2147483647)])
    def test_reconfirmation_preserves_revision_and_response_does_not_loop(
            self, monkeypatch, previous, supplied, expected):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        old = self._source(";PARTSTAT=ACCEPTED;RSVP=FALSE;ROLE=REQ-PARTICIPANT", previous)
        self.put(self.path, old, login="jeff:password")
        sent.clear()
        new = self._source(";PARTSTAT=ACCEPTED;RSVP=TRUE;ROLE=REQ-PARTICIPANT", supplied)
        self.put(self.path, new, check=204, login="jeff:password")
        assert len(sent) == 1
        assert sent[0].method.value == "REQUEST"
        assert sent[0].vevent.attendee.value == "mailto:alice@example.com"
        assert sent[0].vevent.attendee.params["RSVP"] == ["TRUE"]
        assert int(sent[0].vevent.sequence.value) == expected
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == expected
        sent.clear()
        # A later response changes PARTSTAT while RSVP remains TRUE.
        stored.vevent.attendee.params["PARTSTAT"] = ["DECLINED"]
        self.put(self.path, stored.serialize(), check=204, login="jeff:password")
        assert sent == []

    @pytest.mark.parametrize("change,supplied,expected,recipients", [
        ("role", 5, 6, ["alice@example.com"]),
        ("role", 9, 9, ["alice@example.com"]),
        ("role-and-rsvp", 5, 6, ["alice@example.com"]),
        ("location-and-rsvp", 5, 6, ["alice@example.com", "bob@example.com"])])
    def test_content_changes_advance_revision_with_one_request_per_recipient(
            self, monkeypatch, change, supplied, expected, recipients):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        self.put(self.path, self._source(";PARTSTAT=ACCEPTED;RSVP=FALSE"), login="jeff:password")
        sent.clear()
        parameters = ";PARTSTAT=ACCEPTED;RSVP=" + ("TRUE" if "rsvp" in change else "FALSE")
        if "role" in change:
            parameters += ";ROLE=OPT-PARTICIPANT"
        new = self._source(parameters, supplied)
        if "location" in change:
            new = new.replace("LOCATION:Home", "LOCATION:Away")
        self.put(self.path, new, check=204, login="jeff:password")
        assert [message.vevent.attendee.value for message in sent] == [
            "mailto:" + recipient for recipient in recipients]
        assert all(message.method.value == "REQUEST" for message in sent)
        assert all(int(message.vevent.sequence.value) == expected for message in sent)
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == expected
        sent.clear()
        stored.vevent.attendee.params["PARTSTAT"] = ["TENTATIVE"]
        self.put(self.path, stored.serialize(), check=204, login="jeff:password")
        assert sent == []

    def test_reconfirmation_and_another_attendees_role_share_new_revision(self, monkeypatch):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        self.put(self.path, self._source(";RSVP=FALSE"), login="jeff:password")
        sent.clear()
        new = self._source(";RSVP=TRUE").replace(
            "ATTENDEE;CN=Bob:", "ATTENDEE;CN=Bob;ROLE=OPT-PARTICIPANT:")
        self.put(self.path, new, check=204, login="jeff:password")
        assert [message.vevent.attendee.value for message in sent] == [
            "mailto:alice@example.com", "mailto:bob@example.com"]
        assert [int(message.vevent.sequence.value) for message in sent] == [6, 6]


class TestRecurringSchedulingPastGuard(BaseTest):
    """A master's first end does not prove that its series has finished."""

    def setup_method(self):
        TestHookWiring.setup_method(self)
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        self.path = "/calendar.ics/recurring.ics"
        self.start = datetime.now() - timedelta(days=14)

    def _source(self, kind, all_day):
        date_format = "%Y%m%d" if all_day else "%Y%m%dT%H%M%SZ"
        end = self.start + (timedelta(days=1) if all_day else timedelta(hours=2))
        source = _event([ALICE], dtstart=self.start.strftime(date_format),
                        dtend=end.strftime(date_format))
        if all_day:
            source = source.replace("DTSTART:", "DTSTART;VALUE=DATE:").replace(
                "DTEND:", "DTEND;VALUE=DATE:")
        recurrence = {
            "ongoing": "RRULE:FREQ=WEEKLY;COUNT=12",
            "ended-count": "RRULE:FREQ=WEEKLY;COUNT=2",
            "ended-until": ("RRULE:FREQ=WEEKLY;UNTIL=" +
                            (self.start + timedelta(days=7)).strftime(date_format)),
            "unbounded": "RRULE:FREQ=WEEKLY",
            "rdate": (("RDATE;VALUE=DATE:" if all_day else "RDATE:") +
                      (self.start + timedelta(days=21)).strftime(date_format)),
            "none": "",
        }[kind]
        return source.replace("END:VEVENT", recurrence + "\nEND:VEVENT")

    @pytest.mark.parametrize("kind", [
        "ongoing", "ended-count", "ended-until", "unbounded", "rdate"])
    @pytest.mark.parametrize("all_day", [False, True])
    @pytest.mark.parametrize("change", ["add-attendee", "location"])
    def test_series_edits_are_not_suppressed_by_first_end(
            self, monkeypatch, kind, all_day, change):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        source = self._source(kind, all_day)
        self.put(self.path, source, login="jeff:password")
        assert len(sent) == 1
        sent.clear()
        if change == "add-attendee":
            updated = source.replace(
                "END:VEVENT", "ATTENDEE;CN=Bob:mailto:bob@example.com\nEND:VEVENT")
            recipient = "bob@example.com"
        else:
            updated = source.replace("LOCATION:Home", "LOCATION:Elsewhere")
            recipient = "alice@example.com"
        self.put(self.path, updated, check=204, login="jeff:password")
        assert len(sent) == 1
        assert sent[0].method.value == "REQUEST"
        assert sent[0].vevent.attendee.value == "mailto:" + recipient
        property_name = "rdate" if kind == "rdate" else "rrule"
        assert property_name in sent[0].vevent.contents
        sent.clear()
        self.put(self.path, updated, check=204, login="jeff:password")
        assert sent == []

    @pytest.mark.parametrize("kind", ["ongoing", "rdate"])
    @pytest.mark.parametrize("all_day", [False, True])
    def test_removing_recurrence_still_updates_attendees(
            self, monkeypatch, kind, all_day):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        source = self._source(kind, all_day)
        self.put(self.path, source, login="jeff:password")
        sent.clear()
        updated = vobject.readOne(source)
        property_name = "rdate" if kind == "rdate" else "rrule"
        del updated.vevent.contents[property_name]
        self.put(self.path, updated.serialize(), check=204, login="jeff:password")
        assert len(sent) == 1
        assert sent[0].method.value == "REQUEST"
        assert sent[0].vevent.attendee.value == "mailto:alice@example.com"
        assert property_name not in sent[0].vevent.contents

    @pytest.mark.parametrize("itip,kind", [(True, "none"), (False, "ongoing")])
    @pytest.mark.parametrize("all_day", [False, True])
    def test_ordinary_and_legacy_past_event_guards_are_preserved(
            self, monkeypatch, itip, kind, all_day):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        self.configure({"hook": {"itip_enabled": str(itip)}})
        source = self._source(kind, all_day)
        self.put(self.path, source, login="jeff:password")
        updated = source.replace("LOCATION:Home", "LOCATION:Elsewhere")
        self.put(self.path, updated, check=204, login="jeff:password")
        assert sent == []


class TestRecurrenceFingerprintWiring(BaseTest):
    def setup_method(self):
        TestStoredSchedulingRevision.setup_method(self)

    @pytest.mark.parametrize("name", ["EXDATE", "RDATE"])
    def test_later_date_edit_is_sent_and_regrouping_does_not_bump_revision(self, monkeypatch, name):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        dates = [(self.start + timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
                 for days in (7, 14, 21)]
        recurrence = "RRULE:FREQ=WEEKLY;COUNT=12\n" + "\n".join(
            name + ":" + value for value in dates[:2])
        source = TestStoredSchedulingRevision._source(self, [ALICE], 1).replace(
            "END:VEVENT", recurrence + "\nEND:VEVENT")
        self.put(self.path, source, login="jeff:password")
        assert len(sent) == 1
        sent.clear()
        updated = source.replace(dates[1], dates[2])
        self.put(self.path, updated, check=204, login="jeff:password")
        assert len(sent) == 1
        assert sent[0].method.value == "REQUEST"
        transmitted = [date.strftime("%Y%m%dT%H%M%SZ")
                       for line in sent[0].vevent.contents[name.lower()] for date in line.value]
        assert transmitted == [dates[0], dates[2]]
        assert int(sent[0].vevent.sequence.value) == 2
        sent.clear()
        regrouped = updated.replace(
            name + ":" + dates[0] + "\n" + name + ":" + dates[2],
            name + ";VALUE=DATE-TIME:" + dates[2] + "," + dates[0])
        self.put(self.path, regrouped, check=204, login="jeff:password")
        assert sent == []
        _, saved = self.get(self.path, login="jeff:password")
        assert int(vobject.readOne(saved).vevent.sequence.value) == 2


def _timezone_event(tzid="Audit/Seasonal", daylight=True):
    zone = ["BEGIN:VTIMEZONE", "TZID:" + tzid,
            "BEGIN:STANDARD", "DTSTART:19701101T020000",
            "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
            "TZOFFSETFROM:" + ("-0400" if daylight else "-0500"),
            "TZOFFSETTO:-0500", "END:STANDARD"]
    if daylight:
        zone.extend(["BEGIN:DAYLIGHT", "DTSTART:19700308T020000",
                     "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
                     "TZOFFSETFROM:-0500", "TZOFFSETTO:-0400", "END:DAYLIGHT"])
    zone.append("END:VTIMEZONE")
    source = _event([ALICE], dtstart="20270115T100000", dtend="20270115T110000")
    source = source.replace("BEGIN:VEVENT", "\n".join(zone) + "\nBEGIN:VEVENT")
    source = source.replace("DTSTART:20270115", "DTSTART;TZID=" + tzid + ":20270115")
    source = source.replace("DTEND:20270115", "DTEND;TZID=" + tzid + ":20270115")
    return source.replace("END:VEVENT", "RRULE:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=8\nEND:VEVENT")


class TestTimezoneSchedulingWiring(BaseTest):
    def setup_method(self):
        TestStoredSchedulingRevision.setup_method(self)

    @pytest.mark.parametrize("rename", [False, True])
    def test_changed_future_offsets_persist_and_send_new_revision(self, monkeypatch, rename):
        from datetime import timezone
        from io import StringIO

        from dateutil.tz import tzical

        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        source = _timezone_event()
        updated = _timezone_event("Audit/Fixed" if rename else "Audit/Seasonal", False)
        # vobject caches TZIDs globally. Evaluate the supplied rules with
        # independent tzical instances, including when the identifier stays.
        old_zone = tzical(StringIO(source)).get()
        new_zone = tzical(StringIO(updated)).get()
        winter, summer = datetime(2027, 1, 15, 10), datetime(2027, 8, 15, 10)
        assert winter.replace(tzinfo=old_zone) == winter.replace(tzinfo=new_zone)
        expected = summer.replace(tzinfo=new_zone).astimezone(timezone.utc)
        assert summer.replace(tzinfo=old_zone).astimezone(timezone.utc) != expected
        self.put(self.path, source, login="jeff:password")
        old_etag, _ = TestStoredSchedulingRevision._stored(self)
        sent.clear()
        _, headers, _ = self.request("PUT", self.path, updated, check=204,
                                     login="jeff:password", http_if_match=old_etag)
        assert "ETag" not in headers
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == 2
        assert len(sent) == 1
        assert sent[0].method.value == "REQUEST"
        assert int(sent[0].vevent.sequence.value) == 2
        wire_zone = tzical(StringIO(sent[0].serialize())).get()
        assert summer.replace(tzinfo=wire_zone).astimezone(timezone.utc) == expected

    @pytest.mark.parametrize("name", ["RDATE", "EXDATE"])
    def test_date_list_referenced_offset_rules_change_revision(self, monkeypatch, name):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        # The event bounds are UTC; only the date list references the zone.
        source = _timezone_event().replace("DTSTART;TZID=Audit/Seasonal:20270115T100000",
                                           "DTSTART:20270115T150000Z")
        source = source.replace("DTEND;TZID=Audit/Seasonal:20270115T110000",
                                "DTEND:20270115T160000Z")
        source = source.replace("END:VEVENT", name + ";TZID=Audit/Seasonal:20270815T100000\nEND:VEVENT")
        updated = source.replace("TZOFFSETTO:-0400", "TZOFFSETTO:-0300")
        self.put(self.path, source, login="jeff:password")
        sent.clear()
        self.put(self.path, updated, check=204, login="jeff:password")
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == 2
        assert len(sent) == 1 and int(sent[0].vevent.sequence.value) == 2
        assert sent[0].vtimezone.daylight.tzoffsetto.value == "-0300"

    def test_equivalent_rules_and_unused_timezones_do_not_notify(self, monkeypatch):
        sent = TestStoredSchedulingRevision._capture(self, monkeypatch)
        source = _timezone_event()
        self.put(self.path, source, login="jeff:password")
        sent.clear()
        updated = source.replace("FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
                                 "BYDAY=2SU;BYMONTH=3;FREQ=YEARLY")
        unused = "\n".join(["BEGIN:VTIMEZONE", "TZID:Audit/Unused",
                            "BEGIN:STANDARD", "DTSTART:19700101T000000",
                            "TZOFFSETFROM:+0100", "TZOFFSETTO:+0100",
                            "END:STANDARD", "END:VTIMEZONE"])
        updated = updated.replace("BEGIN:VEVENT", unused + "\nBEGIN:VEVENT")
        self.put(self.path, updated, check=204, login="jeff:password")
        assert sent == []
        _, stored = TestStoredSchedulingRevision._stored(self)
        assert int(stored.vevent.sequence.value) == 1


def _email_config(**kwargs):
    from radicale.hook.email import EmailConfig, MessageTemplate
    options = dict(host="smtp.example.com", port=587, security="starttls",
                   ssl_verify_mode="REQUIRED", username="", password="",
                   from_email="jeff@example.com", send_mass_emails=False,
                   dryrun=True,
                   new_or_added_to_event_template=MessageTemplate(subject="x", body="y"),
                   deleted_or_removed_from_event_template=MessageTemplate(subject="x", body="y"),
                   updated_event_template=MessageTemplate(subject="x", body="y"),
                   itip_enabled=True, itip_owner_map="")
    options.update(kwargs)
    return EmailConfig(**options)


class TestOwnerReplyToWiring:
    def test_actor_lookup_is_normalized_and_isolated(self):
        from types import SimpleNamespace

        from radicale.hook.email import Hook

        hook = Hook.__new__(Hook)
        hook.email_config = _email_config(
            itip_owner_reply_to=True,
            itip_owner_map='{"alice": "alice@example.com", "bob": "bob@example.com"}')
        hook.owner_map = {"alice": "alice@example.com", "bob": "bob@example.com"}
        assert hook._owner_reply_to(SimpleNamespace(actor=" Alice ")) == "alice@example.com"
        assert hook._owner_reply_to(SimpleNamespace(actor="bob")) == "bob@example.com"
        assert hook._owner_reply_to(SimpleNamespace(actor="nobody")) is None
        hook.email_config.itip_owner_reply_to = False
        assert hook._owner_reply_to(SimpleNamespace(actor="alice")) is None

    def test_unsafe_reply_to_is_contained_before_smtp(self, monkeypatch):
        from radicale.hook.email.scheduling import PlannedMessage

        config = _email_config(dryrun=False)
        event_text = _event([ALICE, CAROL])
        event = __import__("radicale.hook.email", fromlist=["_read_event"])._read_event(event_text)
        sends = []
        monkeypatch.setattr(config, "_smtp_send", lambda **kwargs: sends.append(kwargs) or True)
        planned = PlannedMessage("REQUEST", "carol@example.com", "Carol", "created")
        assert not config.send_scheduling_email(
            planned, event, event_text, reply_to_email="alice@example.com\nBcc:evil@example.com")
        assert sends == []

    def test_scheduling_email_preserves_envelope_and_serialized_headers(self, monkeypatch):
        import smtplib
        from email import message_from_string

        from radicale.hook.email import _read_event
        from radicale.hook.email.scheduling import PlannedMessage

        config = _email_config(dryrun=False, security="none", from_email="calendar@example.com")
        event_text = _event([ALICE, CAROL])
        event = _read_event(event_text)
        calls = []

        class SMTP:
            def sendmail(self, from_addr, to_addrs, msg):
                calls.append((from_addr, to_addrs, msg))
                return {}

            def quit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(smtplib, "SMTP", lambda **kwargs: SMTP())
        planned = PlannedMessage("REQUEST", "carol@example.com", "Carol", "created")
        assert config.send_scheduling_email(
            planned, event, event_text, reply_to_email="alice@example.com")
        assert len(calls) == 1
        from_addr, to_addrs, raw = calls[0]
        parsed = message_from_string(raw)
        assert parsed.get_all("Reply-To") == ["alice@example.com"]
        assert parsed["From"] == "calendar@example.com"
        assert from_addr == "calendar@example.com"
        assert to_addrs == ["carol@example.com"]


class TestOrganizerIsUsername:
    """itip_organizer_is_username: a username that is an email owns it."""

    def test_email_username_is_an_owner_identity(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        assert resolve_owner_identities(
            "jeff@example.com", {}, organizer_is_username=True) == ["jeff@example.com"]
        # Compared case-insensitively, like the map.
        assert resolve_owner_identities(
            "Jeff@Example.COM", {}, organizer_is_username=True) == ["jeff@example.com"]

    def test_owner_addresses(self):
        from radicale.hook.email.scheduling import owner_addresses
        owner_map = '{"jeff@example.com": "jeff@calendar.example.com"}'
        assert owner_addresses("jeff@example.com", owner_map, True) == [
            "jeff@calendar.example.com", "jeff@example.com"]
        assert owner_addresses("Jeff@Example.com", owner_map, False) == [
            "jeff@calendar.example.com"]
        assert owner_addresses("bob@example.com", owner_map, False) == []
        # The hook refuses a malformed map at load; here it gives nothing.
        assert owner_addresses("jeff", "jeff", True) == []
        assert owner_addresses(None, owner_map, True) == []

    def test_username_email(self):
        from radicale.hook.email.scheduling import username_email
        assert username_email(" Jeff@Example.COM ") == "jeff@example.com"
        for actor in ["jeff", "jeff@", "@example.com", "a@b@example.com",
                      "jeff smith@example.com", "", None]:
            assert username_email(actor) is None, actor

    def test_non_email_username_gets_nothing(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        for actor in ["jeff", "jeff@", "@example.com", "a@b@example.com",
                      "jeff smith@example.com", "", None]:
            assert resolve_owner_identities(
                actor, {}, organizer_is_username=True) == [], actor

    def test_option_off_keeps_map_only(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        assert resolve_owner_identities("jeff@example.com", {}) == []
        assert resolve_owner_identities(
            "jeff@example.com", {"jeff@example.com": "other@example.com"}) == [
            "other@example.com"]

    def test_map_and_option_together(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        m = {"jeff": "jeff@example.com",
             "boss@example.com": "jeff@example.com",
             "alice@example.com": "alice@example.com"}
        # A non-email actor still gets its mapped address.
        assert resolve_owner_identities("jeff", m, True) == ["jeff@example.com"]
        # An email actor gets both its mapped address and itself.
        owner = resolve_owner_identities("boss@example.com", m, True)
        assert owner == ["jeff@example.com", "boss@example.com"]
        event = _event([ALICE, BOB])
        assert len(plan(None, event, owner=owner)) == 2
        # Same address in the map and as username: listed once.
        assert resolve_owner_identities("alice@example.com", m, True) == [
            "alice@example.com"]

    def test_mismatched_organizer_plans_nothing(self):
        from radicale.hook.email.scheduling import resolve_owner_identities
        owner = resolve_owner_identities("alice@example.com", {}, True)
        assert plan(None, _event([ALICE, BOB]), owner=owner) == []

    def test_empty_map_with_option_is_accepted(self):
        _email_config(itip_organizer_is_username=True).validate_itip_config()

    def test_empty_map_without_option_is_still_config_error(self):
        import pytest
        with pytest.raises(ValueError):
            _email_config(itip_organizer_is_username=False).validate_itip_config()

    def test_mass_email_with_option_is_still_config_error(self):
        import pytest
        with pytest.raises(ValueError):
            _email_config(itip_organizer_is_username=True,
                          send_mass_emails=True).validate_itip_config()


class TestHookWiringOrganizerIsUsername(BaseTest):
    """End-to-end: empty map, ownership from the username, dry-run."""

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        import os
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("[allow all]\nuser: .*\ncollection: .*\npermissions: RrWw")
        self.configure({
            "rights": {"file": rights_file_path, "type": "from_file"}})
        self.configure({"auth": {"type": "none"}})
        # No itip_owner_map: hook load must accept it with the option.
        self.configure({
            "hook": {"type": "email", "dryrun": "True",
                     "itip_enabled": "True",
                     "from_email": "calendar@example.com",
                     "itip_organizer_is_username": "True"}})

    def _put_new_event(self, login, caplog):
        caplog.set_level(logging.INFO)
        self.mkcalendar("/calendar.ics/", login=login)
        start = datetime.now() + timedelta(days=1)
        event = _event([ALICE, BOB], dtstart=start.strftime("%Y%m%dT%H%M%SZ"),
                       dtend=(start + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ"))
        self.put("/calendar.ics/event1.ics", event, login=login)
        return [log for log in caplog.messages
                if "DRY-RUN send_scheduling_email / to_addresses" in log]

    def test_email_username_matching_organizer_sends(self, caplog):
        assert self._put_new_event("Jeff@Example.com:password", caplog) == [
            "Hook 'email': DRY-RUN send_scheduling_email / to_addresses=['alice@example.com']",
            "Hook 'email': DRY-RUN send_scheduling_email / to_addresses=['bob@example.com']",
        ]

    def test_email_username_other_organizer_sends_nothing(self, caplog):
        # Organizer is jeff@example.com, the user is Alice.
        assert self._put_new_event("alice@example.com:password", caplog) == []

    def test_non_email_username_sends_nothing(self, caplog):
        assert self._put_new_event("jeff:password", caplog) == []


class TestCalendarUserAddressSet(BaseTest):
    """calendar-user-address-set lists mailto:<username> with the option."""

    PROPFIND = """<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <prop><C:calendar-user-address-set/></prop>
</propfind>"""

    def setup_method(self) -> None:
        BaseTest.setup_method(self)
        import os
        rights_file_path = os.path.join(self.colpath, "rights")
        with open(rights_file_path, "w") as f:
            f.write("[allow all]\nuser: .*\ncollection: .*\npermissions: RrWw")
        self.configure({
            "rights": {"file": rights_file_path, "type": "from_file"}})
        self.configure({"auth": {"type": "none"}})

    def _enable(self, enabled=True, owner_map=None):
        if owner_map is None:
            owner_map = "" if enabled else '{"jeff": "jeff@example.com"}'
        self.configure({
            "hook": {"type": "email", "dryrun": "True",
                     "itip_enabled": "True",
                     "from_email": "calendar@example.com",
                     "itip_owner_map": owner_map,
                     "itip_organizer_is_username": str(enabled)}})

    def _hrefs(self, path, user):
        _, responses = self.propfind(path, self.PROPFIND,
                                     login="%s:password" % user)
        response = responses[path.replace("@", "%40")]
        assert not isinstance(response, int)
        status, prop = response["C:calendar-user-address-set"]
        assert status == 200
        return [href.text for href in prop]

    def test_email_user_gets_mailto_and_path(self):
        self._enable()
        assert self._hrefs("/Jeff@Example.com/", "Jeff@Example.com") == [
            "mailto:jeff@example.com", "/Jeff%40Example.com/"]

    def test_option_off_gets_path_only(self):
        self._enable(False)
        assert self._hrefs("/jeff@example.com/", "jeff@example.com") == [
            "/jeff%40example.com/"]

    def test_mapped_address_listed_first(self):
        self._enable(owner_map='{"jeff@example.com": "jeff@calendar.example.com"}')
        assert self._hrefs("/jeff@example.com/", "jeff@example.com") == [
            "mailto:jeff@calendar.example.com", "mailto:jeff@example.com",
            "/jeff%40example.com/"]

    def test_mapped_address_without_option(self):
        self._enable(False, owner_map='{"jeff": "jeff@example.com"}')
        assert self._hrefs("/jeff/", "jeff") == [
            "mailto:jeff@example.com", "/jeff/"]

    @pytest.mark.parametrize("address,href", [
        ("jeff%2Btag@example.com", "mailto:jeff%252btag@example.com"),
        ("jeff%tag@example.com", "mailto:jeff%25tag@example.com"),
        ("jeff+tag@example.com", "mailto:jeff%2Btag@example.com"),
        ("jeff?tag@example.com", "mailto:jeff%3Ftag@example.com"),
        ("jeff#tag@example.com", "mailto:jeff%23tag@example.com"),
    ])
    def test_principal_mailto_href_preserves_bare_configured_mailbox(self, address, href):
        import json

        self._enable(False, owner_map=json.dumps({"jeff": address}))
        hrefs = self._hrefs("/jeff/", "jeff")
        assert hrefs == [href, "/jeff/"]
        assert extract_email(hrefs[0]) == address.lower()

    def test_unmapped_user_gets_path_only(self):
        self._enable(False, owner_map='{"bob": "bob@example.com"}')
        assert self._hrefs("/jeff/", "jeff") == ["/jeff/"]

    def test_other_users_mapped_address_not_listed(self):
        self._enable(False, owner_map='{"bob": "bob@example.com"}')
        # Create Bob's principal, then look at it as Jeff.
        self._hrefs("/bob/", "bob")
        assert self._hrefs("/bob/", "jeff") == ["/bob/"]

    def test_non_email_user_gets_path_only(self):
        self._enable()
        assert self._hrefs("/jeff/", "jeff") == ["/jeff/"]

    def test_other_users_principal_gets_no_mailto(self):
        self._enable()
        # Create Bob's principal, then look at it as Jeff.
        self._hrefs("/bob@example.com/", "bob@example.com")
        assert self._hrefs("/bob@example.com/", "jeff@example.com") == [
            "/bob%40example.com/"]
