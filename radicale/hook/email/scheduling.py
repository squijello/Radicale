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

"""Scheduling decisions for the email hook.

This is not a full CalDAV Scheduling implementation (RFC 6638):
no inbox/outbox, no REPLY processing, no calendar-auto-schedule.

It decides, from the old and new events, which per-recipient
iTIP REQUEST/CANCEL messages a change requires, and builds
those messages as text/calendar parts listing only the
recipient.

Supported: single-VEVENT events only. A single VEVENT may
define a recurring series with RRULE or RDATE. Multi-VEVENT objects are
rejected with no messages; per-instance (RECURRENCE-ID)
changes are not supported.

Delivery is best-effort SMTP: this module plans messages only,
it does not queue, retry, or track delivery. An SMTP failure
loses the message, and a repeated save plans nothing.
"""

import functools
import json
import re
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import unquote_to_bytes, urlsplit

import vobject
from dateutil.tz import tzutc

__all__ = [
    "AttendeeInfo",
    "EventInfo",
    "PlannedMessage",
    "build_itip_ics",
    "build_itip_message",
    "check_ownership",
    "owner_addresses",
    "parse_owner_map",
    "resolve_owner_identities",
    "username_email",
    "extract_email",
    "parse_event",
    "plan_messages",
    "prepare_itip_upload",
]

MAX_SEQUENCE = 2147483647
_LOCAL_PART = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_UNSUPPORTED_IDENTITY_PARAMETERS = frozenset(("DELEGATED-FROM", "DELEGATED-TO", "SENT-BY", "MEMBER"))


def _normalize_mailbox(address: str) -> Optional[str]:
    """Validate a supported bare mailbox without decoding URI escapes."""
    if not address.isascii():
        return None
    address = address.lower()
    if len(address) > 254 or address.count("@") != 1:
        return None
    local, domain = address.split("@")
    if len(local) > 64 or not _LOCAL_PART.fullmatch(local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    return address


def extract_email(value: Optional[str]) -> Optional[str]:
    """Normalize an ASCII mailbox or complete mailto calendar-user URI.

    URI percent escapes are decoded exactly once. A bare mailbox already
    describes its identity, so percent characters there remain literal.
    Quoted local parts and internationalized addresses are not supported.
    """
    if not isinstance(value, str) or not value.isascii():
        return None
    address = value.strip()
    if address.lower().startswith("mailto:"):
        if address != value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            return None
        try:
            uri = urlsplit(value)
            if (uri.scheme.lower() != "mailto" or uri.netloc or "?" in value or "#" in value or
                    re.search(r"%(?![0-9a-fA-F]{2})", uri.path)):
                return None
            address = unquote_to_bytes(uri.path).decode("ascii")
        except (ValueError, UnicodeError):
            return None
    return _normalize_mailbox(address)


def _content_lines(component, name: str):
    lines = component.contents.get(name.lower(), [])
    return lines if isinstance(lines, (list, tuple)) else [lines]


def _supports_email_identities(event) -> bool:
    """Require complete mailto identities throughout a supported iTIP event."""
    if len(_content_lines(event, "organizer")) != 1:
        return False
    attendee_emails = set()
    for name in ("organizer", "attendee"):
        for line in _content_lines(event, name):
            if not isinstance(line, vobject.base.ContentLine):
                return False
            value = line.value
            if not isinstance(value, str) or not value.lower().startswith("mailto:"):
                return False
            address = extract_email(value)
            if address is None or _UNSUPPORTED_IDENTITY_PARAMETERS.intersection(line.params):
                return False
            if [str(value).upper() for value in line.params.get("VALUE", ["CAL-ADDRESS"])] != ["CAL-ADDRESS"]:
                return False
            if name == "attendee":
                if address in attendee_emails:
                    return False
                attendee_emails.add(address)
    return True


def _first_value(component, name: str):
    lines = _content_lines(component, name)
    return lines[0].value if lines else None


def _first_param(component, name: str, param: str):
    lines = _content_lines(component, name)
    if not lines:
        return None
    params = lines[0].params or {}
    values = params.get(param, [])
    return values[0] if values else None


def _param_of(line, param: str):
    params = getattr(line, "params", {}) or {}
    values = params.get(param, [])
    return values[0] if values else None


def _recurrence_value(value) -> Tuple:
    if isinstance(value, (list, tuple)):
        return tuple(_recurrence_value(part) for part in value)
    return type(value).__name__, str(value)


def _recurrence_properties(event, name: str) -> Tuple:
    """Compare every recurrence value and its parameters without expansion."""
    entries = set()
    for line in _content_lines(event, name):
        parameters = []
        for parameter, values in (line.params or {}).items():
            parameter = parameter.upper()
            normalized = tuple(str(value).upper() if parameter == "VALUE" else str(value)
                               for value in values)
            # DATE-TIME is the default for both date-list properties.
            if name in ("rdate", "exdate") and (parameter, normalized) == ("VALUE", ("DATE-TIME",)):
                continue
            parameters.append((parameter, normalized))
        params = tuple(sorted(parameters))
        values = line.value if isinstance(line.value, list) else [line.value]
        for value in values:
            if name == "rrule":
                # RECUR tokens are case-insensitive; rule parts and their
                # comma-separated choices have no significant ordering.
                parts = [part.partition("=") for part in str(value).upper().split(";")]
                normalized_value = tuple(sorted(
                    (key, separator, tuple(sorted(choices.split(","))))
                    for key, separator, choices in parts))
            else:
                normalized_value = _recurrence_value(value)
            # Repeated lines, value ordering and duplicate dates describe
            # the same recurrence set when their parameters agree.
            entries.add((params, normalized_value))
    return tuple(sorted(entries, key=repr))


@dataclass(frozen=True)
class AttendeeInfo:
    email: str
    name: Optional[str] = None
    schedule_agent: Optional[str] = None
    partstat: Optional[str] = None
    rsvp: bool = False
    role: str = "REQ-PARTICIPANT"


@dataclass
class EventInfo:
    uid: Optional[str]
    sequence: int
    organizer_email: Optional[str]
    organizer_schedule_agent: Optional[str]
    attendees: Dict[str, AttendeeInfo]
    # Hashable summary of selected scheduling fields. SEQUENCE and DTSTAMP
    # alone do not count as content changes; clients may bump them on
    # response-only saves. Attendee changes are considered by the planner.
    details: Tuple
    status: Optional[str] = None
    recurring: bool = False
    supported_identities: bool = True


def parse_event(ics: Optional[str]) -> Optional[EventInfo]:
    """Parse the master VEVENT of a calendar object, or None.

    The master is the first VEVENT without a RECURRENCE-ID. If
    every VEVENT has one, there is no master to diff, so return
    None rather than diffing an override as if it were the master.
    Unsupported organizer or attendee identities mark the whole event
    ineligible for scheduling; dropping one line could falsely infer an
    attendee removal. UID lookup can still inspect an ineligible event.
    """
    if not ics:
        return None
    try:
        calendar = vobject.readOne(ics)
    except Exception:
        return None
    vevents = getattr(calendar, "vevent_list", None)
    if not vevents:
        try:
            vevents = [calendar.vevent]
        except Exception:
            return None
    if not vevents:
        return None
    event = None
    for candidate in vevents:
        if _first_value(candidate, "recurrence-id") is None:
            event = candidate
            break
    if event is None:
        return None
    organizer_value = _first_value(event, "organizer")
    sequence_value = _first_value(event, "sequence")
    try:
        sequence = int(sequence_value) if sequence_value is not None else 0
    except (TypeError, ValueError):
        sequence = 0
    attendees: Dict[str, AttendeeInfo] = {}
    for line in _content_lines(event, "attendee"):
        email = extract_email(line.value)
        if email and email not in attendees:
            attendees[email] = AttendeeInfo(
                email=email,
                name=_param_of(line, "CN"),
                schedule_agent=_param_of(line, "SCHEDULE-AGENT"),
                partstat=_param_of(line, "PARTSTAT"),
                rsvp=(_param_of(line, "RSVP") or "").upper() == "TRUE",
                role=(_param_of(line, "ROLE") or "REQ-PARTICIPANT").upper(),
            )

    def raw(name: str):
        value = _first_value(event, name)
        return None if value is None else str(value)

    details = (
        raw("summary"),
        raw("description"),
        raw("location"),
        raw("dtstart"),
        raw("dtend"),
        raw("duration"),
        raw("status"),
        _recurrence_properties(event, "rrule"),
        _recurrence_properties(event, "rdate"),
        _recurrence_properties(event, "exdate"),
        raw("recurrence-id"),
        extract_email(organizer_value),
    )
    return EventInfo(
        uid=raw("uid"),
        sequence=sequence,
        organizer_email=extract_email(organizer_value),
        organizer_schedule_agent=_first_param(event, "organizer", "SCHEDULE-AGENT"),
        attendees=attendees,
        status=raw("status"),
        recurring=bool(_content_lines(event, "rrule") or _content_lines(event, "rdate")),
        supported_identities=_supports_email_identities(event),
        details=details,
    )


@dataclass(frozen=True)
class PlannedMessage:
    """One scheduling message the server should send, or none at all."""
    method: str  # "REQUEST" or "CANCEL"
    recipient_email: str
    recipient_name: Optional[str]
    # "created", "added", "removed", "updated", "reconfirmation",
    # "cancelled" or "deleted".
    kind: str


def parse_owner_map(value: Optional[Union[str, Dict[str, Any]]]) -> Dict[str, str]:
    """Parse and validate ordered username regex-to-email template rules."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        def reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
            result: Dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate key in itip_owner_map: %r" % key)
                result[key] = item
            return result

        try:
            value = json.loads(value, object_pairs_hook=reject_duplicate_keys)
        except (ValueError, TypeError) as e:
            raise ValueError("itip_owner_map must be a valid JSON object") from e
    if not isinstance(value, dict):
        raise ValueError("itip_owner_map must be a JSON object")
    result: Dict[str, str] = {}
    seen = set()
    for actor, email in value.items():
        if not isinstance(actor, str) or not isinstance(email, str):
            raise ValueError("itip_owner_map keys and values must be strings")
        normalized_actor = actor.strip()
        normalized_email = email.strip().lower()
        if not normalized_actor or not normalized_email:
            raise ValueError("malformed itip_owner_map entry: %r" % actor)
        if normalized_actor in seen:
            raise ValueError("duplicate actor in itip_owner_map: %r" % normalized_actor)
        seen.add(normalized_actor)
        _owner_rule(normalized_actor, normalized_email)
        result[normalized_actor] = normalized_email
    return result


@functools.lru_cache(maxsize=256)
def _owner_rule(pattern: str, template: str) -> re.Pattern:
    """Compile a rule and validate its numeric brace substitutions."""
    import string

    try:
        regex = re.compile(pattern, re.IGNORECASE)
        has_fields = False
        for _, field, spec, conversion in string.Formatter().parse(template):
            has_fields = has_fields or field is not None
            if field is not None and (
                    not field.isascii() or not field.isdecimal() or
                    int(field) > regex.groups or spec or conversion):
                raise ValueError("expected {0} through {%d}" % regex.groups)
        sample = template.format(*(["group"] * (regex.groups + 1)))
        if not has_fields and _normalize_mailbox(sample) is None:
            raise ValueError("expected a supported bare email address")
    except (re.error, ValueError, IndexError, KeyError) as e:
        raise ValueError("invalid itip_owner_map rule %r: %s" % (pattern, e)) from e
    return regex


def mapped_owner_email(actor: Optional[str],
                       owner_map: Optional[Dict[str, str]]) -> Optional[str]:
    """Expand the first full-string rule, failing closed on invalid output."""
    if not actor:
        return None
    actor = actor.strip()
    for pattern, template in (owner_map or {}).items():
        match = _owner_rule(pattern, template).fullmatch(actor)
        if match:
            address = template.format(match.group(0), *(
                group or "" for group in match.groups())).strip().lower()
            return _normalize_mailbox(address)
    return None


def username_email(actor: Optional[str]) -> Optional[str]:
    """The supported bare mailbox an actor's username is, or None."""
    if not actor:
        return None
    return _normalize_mailbox(actor.strip())


def resolve_owner_identities(actor: Optional[str],
                             owner_map: Optional[Dict[str, str]],
                             organizer_is_username: bool = False) -> List[str]:
    """Resolve the owner identities for one actor.

    The mapped email for the actor counts. With
    ``organizer_is_username``, an actor whose username is an
    email address also owns that address. Any other actor gets
    no identities. The SMTP ``from_email`` is never evidence of
    ownership.
    """
    if not actor:
        return []
    actor = actor.strip()
    identities: List[str] = []
    mapped = mapped_owner_email(actor, owner_map)
    if mapped:
        identities.append(mapped)
    if organizer_is_username:
        email = username_email(actor)
        if email and email not in identities:
            identities.append(email)
    return identities


@functools.lru_cache(maxsize=8)
def _cached_owner_map(text: str) -> Dict[str, str]:
    return parse_owner_map(text)


def owner_addresses(actor: Optional[str], owner_map_text: Optional[str],
                    organizer_is_username: bool = False) -> List[str]:
    """The organizer addresses an actor owns, for its principal.

    The same identities as resolve_owner_identities, from the
    ``itip_owner_map`` setting as written: the mapped address
    first, then the username with ``organizer_is_username``.
    A malformed map gives no mapped address here; the hook
    refuses to load with it.
    """
    try:
        owner_map = _cached_owner_map(owner_map_text or "")
    except ValueError:
        owner_map = {}
    return resolve_owner_identities(actor, owner_map, organizer_is_username)


def check_ownership(organizer_email: Optional[str],
                    owner_identities: Optional[Sequence[str]]) -> bool:
    """True only if an owner identity matches the organizer.

    Only a full email identity counts, matched exactly
    (case-insensitive). A bare username does not match. The
    SMTP ``from_email`` is never used here. No match means no
    messages may be sent: this is the guard against using an
    attendee copy to notify the organizer's other guests.
    """
    if not organizer_email or not owner_identities:
        return False
    organizer_email = organizer_email.strip().lower()
    for identity in owner_identities:
        if not identity:
            continue
        if identity.strip().lower() == organizer_email:
            return True
    return False


def _eligible(event: EventInfo, attendee: AttendeeInfo) -> bool:
    if attendee.email == event.organizer_email:
        return False
    # RFC 6638, section 3.2.1.1: only SERVER (the default, when the
    # parameter is absent) gets a scheduling message. CLIENT and
    # NONE both mean the server sends nothing to that attendee.
    agent = (attendee.schedule_agent or "").upper()
    return agent in ("", "SERVER")


def _vevent_count(ics: Optional[str]) -> int:
    """Number of VEVENTs in a calendar object, or 0 on parse failure."""
    if not ics:
        return 0
    try:
        calendar = vobject.readOne(ics)
    except Exception:
        return 0
    vevents = getattr(calendar, "vevent_list", None)
    if vevents:
        return len(vevents)
    try:
        calendar.vevent
        return 1
    except Exception:
        return 0


def plan_messages(old_ics: Optional[str], new_ics: Optional[str],
                  deleted: bool = False,
                  owner_identities: Optional[Sequence[str]] = None) -> List[PlannedMessage]:
    """Decide which scheduling messages an event change requires.

    ``old_ics`` is the event before the change (None for a new
    event), ``new_ics`` is the event after it. For
    ``deleted=True`` only ``old_ics`` is used and every eligible
    attendee of an active event gets a CANCEL. An empty list means
    nothing is sent: a repeated save, an unproved organizer, an unparseable
    previous event, unsupported identities, or a rejected multi-VEVENT object.
    The email hook logs unsupported uploads and ownership mismatches.
    """
    new_event = None if deleted else parse_event(new_ics)
    if deleted:
        old_event = parse_event(old_ics)
        if old_event is None or not old_event.supported_identities:
            return []
        if _vevent_count(old_ics) > 1:
            return []
        if not check_ownership(old_event.organizer_email, owner_identities):
            return []
        if (old_event.status or "").upper() == "CANCELLED":
            return []
        return [PlannedMessage("CANCEL", a.email, a.name, "deleted")
                for a in old_event.attendees.values()
                if _eligible(old_event, a)]
    if new_event is None or not new_event.supported_identities:
        return []
    if not check_ownership(new_event.organizer_email, owner_identities):
        return []
    if old_ics is None:
        if _vevent_count(new_ics) > 1:
            return []
        if (new_event.status or "").upper() == "CANCELLED":
            return []
        return [PlannedMessage("REQUEST", a.email, a.name, "created")
                for a in new_event.attendees.values()
                if _eligible(new_event, a)]
    old_event = parse_event(old_ics)
    if old_event is None or not old_event.supported_identities:
        # A usable prior identity set is needed to infer membership changes.
        return []
    # Do not infer changes across different events or organizers.
    if not old_event.uid or old_event.uid != new_event.uid:
        return []
    if old_event.organizer_email != new_event.organizer_email:
        return []
    # Multi-VEVENT objects are rejected, not approximated.
    if _vevent_count(old_ics) > 1 or _vevent_count(new_ics) > 1:
        return []
    old_cancelled = (old_event.status or "").upper() == "CANCELLED"
    if (new_event.status or "").upper() == "CANCELLED":
        if old_cancelled:
            return []
        # Honor retained attendees' current delivery directives. Removed
        # attendees use their previous directives; newly added attendees
        # never had an invitation to cancel.
        return [PlannedMessage("CANCEL", a.email, a.name, "cancelled")
                for a in old_event.attendees.values()
                if _eligible(new_event, new_event.attendees.get(a.email, a))]
    if old_cancelled:
        return [PlannedMessage("REQUEST", a.email, a.name,
                               "updated" if a.email in old_event.attendees else "added")
                for a in new_event.attendees.values() if _eligible(new_event, a)]
    planned: List[PlannedMessage] = []
    for email, attendee in new_event.attendees.items():
        if email not in old_event.attendees and _eligible(new_event, attendee):
            planned.append(PlannedMessage("REQUEST", email, attendee.name, "added"))
    for email, attendee in old_event.attendees.items():
        if email not in new_event.attendees and _eligible(old_event, attendee):
            planned.append(PlannedMessage("CANCEL", email, attendee.name, "removed"))
    details_changed = new_event.details != old_event.details
    for email, attendee in new_event.attendees.items():
        previous = old_event.attendees.get(email)
        if previous is None or not _eligible(new_event, attendee):
            continue
        if details_changed or attendee.role != previous.role:
            kind = "updated"
        elif attendee.rsvp and not previous.rsvp:
            kind = "reconfirmation"
        else:
            # PARTSTAT and response-side RSVP clearing do not invite again.
            continue
        planned.append(PlannedMessage("REQUEST", email, attendee.name, kind))
    return planned


def _strip_scheduling_params(line) -> None:
    """Remove CalDAV server directives from a property line, in place."""
    params = getattr(line, "params", None)
    if not params:
        return
    for key in ("SCHEDULE-AGENT", "SCHEDULE-FORCE-SEND"):
        params.pop(key, None)


def _event_sequence(event) -> int:
    """Read the non-negative iCalendar integer used for a scheduling revision."""
    lines = _content_lines(event, "sequence")
    if not lines:
        return 0
    if len(lines) != 1 or not re.fullmatch(r"\+?[0-9]+", str(lines[0].value)):
        raise ValueError("invalid scheduling SEQUENCE")
    value = int(lines[0].value)
    if value > MAX_SEQUENCE:
        raise ValueError("scheduling SEQUENCE exceeds iCalendar integer range")
    return value


def prepare_itip_upload(old_ics: Optional[str], new_ics: str,
                        owner_identities: Optional[Sequence[str]]) -> Optional[str]:
    """Persist a revision floor before sending messages for an owned event.

    A content or membership change advances beyond the stored revision.
    Reconfirmation, response-only and no-op saves preserve the greater
    stored/client revision. The stored value is then used on the wire,
    so later replies and reinvitations use
    the same revision space. Unsupported objects are left unchanged.
    """
    new_event = parse_event(new_ics)
    if (new_event is None or not new_event.supported_identities or
            _vevent_count(new_ics) != 1 or
            not check_ownership(new_event.organizer_email, owner_identities)):
        return None
    calendar = vobject.readOne(new_ics)
    new_sequence = _event_sequence(calendar.vevent)
    if old_ics is None:
        return None
    old_event = parse_event(old_ics)
    if (old_event is None or not old_event.supported_identities or
            _vevent_count(old_ics) != 1 or
            old_event.uid != new_event.uid or
            old_event.organizer_email != new_event.organizer_email):
        return None
    old_sequence = _event_sequence(vobject.readOne(old_ics).vevent)
    target = max(old_sequence, new_sequence)
    planned = plan_messages(old_ics, new_ics, owner_identities=owner_identities)
    # RFC 5546 section 3.2.2.7 keeps SEQUENCE for a pure RSVP re-request.
    if any(message.kind != "reconfirmation" for message in planned):
        target = max(old_sequence + 1, target)
    if target > MAX_SEQUENCE:
        raise ValueError("scheduling SEQUENCE is exhausted; create a meeting with a new UID")
    if target == new_sequence:
        return None
    sequence_lines = _content_lines(calendar.vevent, "sequence")
    # Newly added SEQUENCE properties use vobject's text serializer.
    if sequence_lines:
        sequence_lines[0].value = str(target)
    else:
        calendar.vevent.add("sequence").value = str(target)
    return calendar.serialize()


def build_itip_ics(source_ics: str, method: str, recipient_email: str,
                   sequence: Optional[int] = None, cancel_all: bool = True) -> str:
    """Build a per-recipient iTIP calendar object.

    ``source_ics`` is the new event for REQUEST and the old event
    for CANCEL (a removed attendee is not in the new event). The
    result has METHOD set, the organizer preserved, and exactly
    one ATTENDEE (the recipient). ``SCHEDULE-AGENT`` and
    ``SCHEDULE-FORCE-SEND`` are stripped from ORGANIZER and
    ATTENDEE lines.

    For CANCEL, ``cancel_all=True`` cancels the event and sets
    STATUS:CANCELLED. Set it to False when uninviting only the
    recipient; that cancellation omits STATUS. Both scopes omit
    VALARM components, as required by RFC 5546 section 3.2.5.

    A multi-VEVENT source raises ValueError, matching the
    planner's rejection; the builder enforces the restriction
    independently. Unsupported or ambiguous organizer/attendee identities
    also reject the whole source before any per-recipient filtering.

    Revision handling (RFC 5546, RFC 6638 section 3.2.5):
    DTSTAMP is set to generation time. For REQUEST, SEQUENCE is
    preserved from the source. For CANCEL, SEQUENCE is
    ``sequence`` if given, otherwise source SEQUENCE + 1. The
    hook supplies the persisted update revision for attendee
    removal or status cancellation. Deletion uses source + 1.
    """
    if method not in ("REQUEST", "CANCEL"):
        raise ValueError("unsupported iTIP method: %r" % method)
    if _vevent_count(source_ics) > 1:
        raise ValueError("multi-VEVENT calendar objects are rejected")
    calendar = vobject.readOne(source_ics)
    # vobject stores METHOD as a top-level property of VCALENDAR.
    if "method" in calendar.contents:
        calendar.contents["method"][0].value = method
    else:
        calendar.add("method").value = method
    vevents = getattr(calendar, "vevent_list", None) or [calendar.vevent]
    recipient = recipient_email.strip().lower()
    # vobject serializes DTSTAMP via dateutil's tzutc, not via
    # datetime.timezone.utc (which it cannot guess a TZID for).
    now = datetime.now(tzutc())
    found_recipient = False
    for event in vevents:
        if not _supports_email_identities(event):
            raise ValueError("unsupported or ambiguous scheduling identity")
        source_sequence = _event_sequence(event)
        # DTSTAMP: time this message is generated.
        dtstamp_lines = _content_lines(event, "dtstamp")
        if dtstamp_lines:
            dtstamp_lines[0].value = now
        # SEQUENCE: preserve for REQUEST; for CANCEL use the
        # explicit sequence if given, otherwise source + 1.
        if method == "CANCEL":
            event.contents.pop("status", None)
            event.contents.pop("valarm", None)
            if cancel_all:
                event.add("status").value = "CANCELLED"
            sequence_lines = _content_lines(event, "sequence")
            target = source_sequence + 1 if sequence is None else sequence
            if type(target) is not int or not 0 <= target <= MAX_SEQUENCE:
                raise ValueError("CANCEL SEQUENCE exceeds iCalendar integer range")
            if sequence_lines:
                sequence_lines[0].value = str(target)
            else:
                event.add("sequence").value = str(target)
        # Strip server directives from the organizer line.
        for line in _content_lines(event, "organizer"):
            _strip_scheduling_params(line)
        # Filter attendees in this VEVENT to the recipient only.
        attendee_lines = _content_lines(event, "attendee")
        if not attendee_lines:
            continue
        kept = []
        for line in attendee_lines:
            if extract_email(line.value) == recipient:
                _strip_scheduling_params(line)
                kept.append(line)
                found_recipient = True
        if kept:
            event.contents["attendee"] = kept
        else:
            # This VEVENT lists other attendees but not the
            # recipient: drop its attendee list rather than
            # leaking it to the recipient.
            event.contents.pop("attendee", None)
    if not found_recipient:
        # The recipient was not an attendee of the source event
        # (should not happen for a planned message, but never invent
        # a recipient line with invented parameters).
        raise ValueError("recipient %r is not an attendee of the event" % recipient_email)
    return calendar.serialize()


def build_itip_message(from_email: str, to_email: str, subject: str,
                       body_text: str, itip_ics: str, method: str,
                       reply_to_email: Optional[str] = None) -> str:
    """Build an iMIP message: plain text plus a text/calendar part.

    The calendar part carries ``method=REQUEST`` (or CANCEL) so a
    receiving client can process it as an update to the existing
    event instead of importing a second, duplicate event.
    """
    message = MIMEMultipart("mixed")
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    if reply_to_email:
        if "\r" in reply_to_email or "\n" in reply_to_email:
            raise ValueError("Reply-To must not contain CR or LF")
        message["Reply-To"] = reply_to_email
    message["Date"] = formatdate(localtime=True)
    message.attach(MIMEText(body_text, "plain"))
    calendar_part = MIMEText(itip_ics, "calendar", "utf-8")
    calendar_part.set_param("method", method)
    calendar_part.add_header("Content-Disposition",
                             'attachment; filename="event.ics"')
    message.attach(calendar_part)
    return message.as_string()
