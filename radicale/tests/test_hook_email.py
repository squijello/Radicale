# This file is part of Radicale - CalDAV and CardDAV server
# Copyright © 2025-2025 Peter Bieringer <pb@bieringer.de>
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
Radicale tests related to hook 'email'

"""

import logging
import os
import re
import smtplib
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from radicale.tests import BaseTest
from radicale.tests.helpers import get_file_content


class TestHooks(BaseTest):
    """Tests with hooks."""

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
        self.configure({"hook": {"type": "email",
                                 "dryrun": "True"}})

    def _future_date_timestamp(self) -> str:
        """Return a date timestamp for a future date."""
        future_date = datetime.now() + timedelta(days=1)
        return future_date.strftime("%Y%m%dT%H%M%S")

    def _future_date(self) -> str:
        """Return a date for a future date."""
        future_date = datetime.now() + timedelta(days=1)
        return future_date.strftime("%Y%m%d")

    def _past_date_timestamp(self) -> str:
        past_date = datetime.now() - timedelta(days=1)
        return past_date.strftime("%Y%m%dT%H%M%S")

    def _past_date(self) -> str:
        past_date = datetime.now() - timedelta(days=1)
        return past_date.strftime("%Y%m%d")

    def _replace_end_date_in_event(self, event: str, new_date: str) -> str:
        """Replace the end date in an event string."""
        return re.sub(r"DTEND;TZID=Europe/Paris:\d{8}T\d{6}",
                      f"DTEND;TZID=Europe/Paris:{new_date}", event)

    def _replace_end_onlydate_in_event(self, event: str, new_date: str) -> str:
        """Replace the end date in an event string."""
        return re.sub(r"DTEND;VALUE=DATE:\d{8}",
                      f"DTEND;VALUE=DATE:{new_date}", event)

    def test_add_event_with_future_end_date(self, caplog) -> None:
        caplog.set_level(logging.INFO)
        """Add an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._future_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, headers, answer = self.request("GET", path, check=200)
        assert "ETag" in headers
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert "VEVENT" in answer
        assert "Event" in answer
        assert "UID:event" in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'upsert'," in log]) == 1
        # Should NOT have a log saying that no email is sent (email won't actually be sent due to dryrun)
        assert len([log for log in logs if "New event detected, sending notifications to all attendees: event1" in log]) == 1

    def test_add_event_with_past_end_date(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        """Add an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._past_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, headers, answer = self.request("GET", path, check=200)
        assert "ETag" in headers
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert "VEVENT" in answer
        assert "Event" in answer
        assert "UID:event" in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'upsert'," in log]) == 1
        # Should have a log saying that no email is sent due to past end date
        assert len([log for log in logs if "Event end time is in the past, skipping notification for event: event1" in log]) == 1

    def test_delete_event_with_future_end_date(self, caplog) -> None:
        caplog.set_level(logging.INFO)
        """Delete an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._future_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, responses = self.delete(path)
        assert responses[path] == 200
        _, answer = self.get("/calendar.ics/")
        assert "VEVENT" not in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'delete'," in log]) == 1
        # Should NOT have a log saying that no email is sent (email won't actually be sent due to dryrun)
        assert len([log for log in logs if "New event detected, sending notifications to all attendees: event1" in log]) == 1

    def test_delete_event_with_past_end_date(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        """Delete an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._past_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, responses = self.delete(path)
        assert responses[path] == 200
        _, answer = self.get("/calendar.ics/")
        assert "VEVENT" not in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'delete'," in log]) == 1
        # Should have a log saying that no email is sent due to past end date
        assert len([log for log in logs if "Event end time is in the past, skipping notification for event: event1" in log]) == 1

    def test_delete_event_with_future_end_onlydate(self, caplog) -> None:
        caplog.set_level(logging.INFO)
        """Delete an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event_issue1917_1.ics")
        event = self._replace_end_onlydate_in_event(event, self._future_date())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, responses = self.delete(path)
        assert responses[path] == 200
        _, answer = self.get("/calendar.ics/")
        assert "VEVENT" not in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'delete'," in log]) == 1
        # Should NOT have a log saying that no email is sent (email won't actually be sent due to dryrun)
        assert len([log for log in logs if "New event detected, sending notifications to all attendees: event1" in log]) == 1

    def test_delete_event_with_past_onlyend_date(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        """Delete an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event_issue1917_1.ics")
        event = self._replace_end_onlydate_in_event(event, self._past_date())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, responses = self.delete(path)
        assert responses[path] == 200
        _, answer = self.get("/calendar.ics/")
        assert "VEVENT" not in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'delete'," in log]) == 1
        # Should have a log saying that no email is sent due to past end date
        assert len([log for log in logs if "Event end time is in the past, skipping notification for event: event1" in log]) == 1

    def test_add_event_with_future_mass1_end_date(self, caplog) -> None:
        self.configure({"hook": {"type": "email",
                                 "mass_email": "True",
                                 "dryrun": "True"}})

        caplog.set_level(logging.INFO)
        """Add an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._future_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, headers, answer = self.request("GET", path, check=200)
        assert "ETag" in headers
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert "VEVENT" in answer
        assert "Event" in answer
        assert "UID:event" in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'upsert'," in log]) == 1
        # Should NOT have a log saying that no email is sent (email won't actually be sent due to dryrun)
        assert len([log for log in logs if "New event detected, sending notifications to all attendees: event1" in log]) == 1
        assert len([log for log in logs if "Hello everyone" in log]) == 1

    def test_add_event_with_future_mass2_end_date(self, caplog) -> None:
        self.configure({"hook": {"type": "email",
                                 "mass_email": "True",
                                 "dryrun": "True"}})

        caplog.set_level(logging.INFO)
        """Add an event."""
        self.mkcalendar("/calendar.ics/")
        event = get_file_content("event1a1.ics")
        event = self._replace_end_date_in_event(event, self._future_date_timestamp())
        path = "/calendar.ics/event1.ics"
        self.put(path, event)
        _, headers, answer = self.request("GET", path, check=200)
        assert "ETag" in headers
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert "VEVENT" in answer
        assert "Event" in answer
        assert "UID:event" in answer

        logs = caplog.messages
        # Should have a log saying the notification item was received
        assert len([log for log in logs if "received notification_item: {'type': 'upsert'," in log]) == 1
        # Should NOT have a log saying that no email is sent (email won't actually be sent due to dryrun)
        assert len([log for log in logs if "New event detected, sending notifications to all attendees: event1" in log]) == 1
        assert len([log for log in logs if "Hello everyone" in log]) == 0

    def test_itip_owner_reply_to_config(self) -> None:
        from radicale.hook.email import Hook

        self.configure({"hook": {"type": "email", "dryrun": "True"}})
        hook = Hook(self.configuration)
        assert hook.email_config.itip_owner_reply_to is False

        self.configure({"hook": {"type": "email", "dryrun": "True",
                                 "itip_owner_reply_to": "True"}})
        hook = Hook(self.configuration)
        assert hook.email_config.itip_owner_reply_to is True

    def test_smtp_password_file(self) -> None:
        from radicale.hook.email import Hook

        secret = os.path.join(self.colpath, "smtp_password")
        self.configure({
            "hook": {
                "type": "email",
                "smtp_server": "localhost",
                "smtp_password": "inline-secret",
                "smtp_password_file": secret,
                "dryrun": "True",
            }
        })
        hook = Hook(self.configuration)
        assert hook.email_config.password == "inline-secret"
        with open(secret, "w") as f:
            f.write("file-secret\n")
        hook = Hook(self.configuration)
        assert hook.email_config.password == "file-secret"

    def test_smtp_port_config(self) -> None:
        # check for invalid port
        logging.info("Try invalid port: 0")
        try:
            self.configure({
                "hook": {
                    "type": "email",
                    "smtp_server": "localhost",
                    "smtp_port": 0,
                    "smtp_password": "inline-secret",
                    "dryrun": "True",
                }
            })
        except RuntimeError as e:
            logging.info("Exception: %s", str(e))
        else:
            raise

        # check for invalid port
        logging.info("Try invalid port: 65535")
        try:
            self.configure({
                "hook": {
                    "type": "email",
                    "smtp_server": "localhost",
                    "smtp_port": 65535,
                    "smtp_password": "inline-secret",
                    "dryrun": "True",
                }
            })
        except RuntimeError as e:
            logging.info("Exception: %s", str(e))
        else:
            raise

        # check for valid port
        logging.info("Try valid port")
        self.configure({
            "hook": {
                "type": "email",
                "smtp_server": "localhost",
                "smtp_port": 25,
                "smtp_password": "inline-secret",
                "dryrun": "True",
            }
        })

    def _install_fake_smtp(self, monkeypatch, boom=False):
        import radicale.hook.email as email_hook

        fail = boom

        class FakeSMTP:
            created = []
            messages = []

            def __init__(self, host=None, port=None, context=None, **kwargs):
                self.host = host
                self.port = port
                self.context = context
                self.timeout = kwargs.get("timeout")
                self.starttls_context = None
                self.closed = False
                FakeSMTP.created.append(self)

            def ehlo(self):
                return (250, b"ok")

            def starttls(self, context=None, **kwargs):
                self.starttls_context = context
                return (220, b"ready")

            def login(self, user=None, password=None):
                return (235, b"ok")

            def sendmail(self, from_addr, to_addrs, msg):
                if fail:
                    raise smtplib.SMTPException("send failed")
                FakeSMTP.messages.append((from_addr, list(to_addrs), msg))
                return {}

            def quit(self):
                return (221, b"bye")

            def close(self):
                self.closed = True

        FakeSMTP.created = []
        FakeSMTP.messages = []
        monkeypatch.setattr(email_hook.smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(email_hook.smtplib, "SMTP_SSL", FakeSMTP)
        return FakeSMTP

    def _email_hook(self, security="none", verify_mode="REQUIRED", dryrun="False"):
        self.configure({
            "hook": {
                "type": "email",
                "dryrun": dryrun,
                "smtp_server": "127.0.0.1",
                "smtp_port": "2525",
                "smtp_security": security,
                "smtp_ssl_verify_mode": verify_mode,
                "from_email": "calendar@example.com",
                "mass_email": "False",
            }})

    def _future_event(self, summary="Event") -> str:
        event = get_file_content("event1.ics")
        event = self._replace_end_date_in_event(event, self._future_date_timestamp())
        return event.replace("SUMMARY:Event", "SUMMARY:" + summary, 1)

    def test_send_includes_attendee_and_subject(self, monkeypatch) -> None:
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()
        self.mkcalendar("/calendar.ics/")
        self.put("/calendar.ics/event1.ics", self._future_event())
        assert fake.messages
        for _sender, recipients, message in fake.messages:
            assert "You have been added to an event" in message
        recipients = [address for _sender, addresses, _message in fake.messages for address in addresses]
        assert "janedoe@example.com" in recipients
        assert "johndoe@example.com" in recipients
        sent = cast(Any, self.application._hook).email_config._send_email(
            "subject line", "body", [type("A", (), {"email": "janedoe@example.com"})()])
        assert sent is True

    def test_smtp_exception_returns_false(self, monkeypatch) -> None:
        self._install_fake_smtp(monkeypatch, boom=True)
        self._email_hook()
        sent = cast(Any, self.application._hook).email_config._send_email(
            "subject line", "body", [type("A", (), {"email": "janedoe@example.com"})()])
        assert sent is False

    @pytest.mark.parametrize("security", ["none", "starttls", "tls"])
    def test_smtp_socket_timeout_and_cleanup(self, monkeypatch, security):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook(security=security)
        config = cast(Any, self.application._hook).email_config

        assert config._smtp_send("Subject: Test\n\nBody", ["alice@example.com"])
        assert len(fake.created) == 1
        assert fake.created[0].timeout == 30
        assert fake.created[0].closed

    def test_smtp_stalled_greeting_times_out_and_closes_socket(self, monkeypatch):
        import radicale.hook.email as email_hook

        self._email_hook()
        config = cast(Any, self.application._hook).email_config
        monkeypatch.setattr(email_hook, "SMTP_TIMEOUT_SECONDS", 0.05)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            config.host, config.port = listener.getsockname()

            def wait_for_client_close():
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    # No greeting: the SMTP client must time out and close.
                    assert connection.recv(1) == b""

            with ThreadPoolExecutor(max_workers=1) as executor:
                peer = executor.submit(wait_for_client_close)
                assert config._smtp_send(
                    "Subject: Test\n\nBody", ["alice@example.com"]) is False
                peer.result(timeout=3)

    @pytest.mark.parametrize("security, stage, error", [
        ("none", "connect", ConnectionRefusedError("connection refused")),
        ("starttls", "connect", TimeoutError("connection timed out")),
        ("tls", "connect", ssl.SSLError("TLS handshake failed")),
        ("starttls", "ehlo", OSError("network unavailable")),
        ("starttls", "starttls", ssl.SSLError("TLS upgrade failed")),
        ("none", "login", smtplib.SMTPAuthenticationError(535, b"rejected")),
        ("none", "sendmail", smtplib.SMTPDataError(451, b"try later")),
        ("none", "sendmail", ConnectionResetError("connection reset")),
        ("tls", "sendmail", TimeoutError("DATA timed out")),
    ])
    def test_smtp_transport_failure_returns_false_and_closes(
            self, monkeypatch, caplog, security, stage, error):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook(security=security)
        config = cast(Any, self.application._hook).email_config
        config.username, config.password = "sender", "secret"

        def fail(*args, **kwargs):
            raise error

        if stage == "connect":
            constructor = "SMTP_SSL" if security == "tls" else "SMTP"
            monkeypatch.setattr(smtplib, constructor, fail)
        else:
            monkeypatch.setattr(fake, stage, fail)

        assert config._smtp_send("Subject: Test\n\nBody", ["alice@example.com"]) is False
        assert not fake.messages
        assert len(fake.created) == (0 if stage == "connect" else 1)
        assert all(client.closed for client in fake.created)
        assert any("SMTP delivery failed" in message for message in caplog.messages)

    @pytest.mark.parametrize("error", [
        smtplib.SMTPServerDisconnected("QUIT disconnected"),
        OSError("QUIT connection reset"),
    ])
    def test_smtp_quit_failure_preserves_acceptance(self, monkeypatch, caplog, error):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()

        def fail_quit(client):
            raise error

        monkeypatch.setattr(fake, "quit", fail_quit)
        config = cast(Any, self.application._hook).email_config
        assert config._smtp_send("Subject: Test\n\nBody", ["alice@example.com"]) is True
        assert len(fake.messages) == 1
        assert fake.created[0].closed
        assert any("SMTP session shutdown failed" in message for message in caplog.messages)

    @pytest.mark.parametrize("delivery_fails", [False, True])
    def test_smtp_close_failure_does_not_mask_delivery_result(
            self, monkeypatch, caplog, delivery_fails):
        fake = self._install_fake_smtp(monkeypatch, boom=delivery_fails)
        self._email_hook()
        close_attempts = []

        def fail_close(client):
            close_attempts.append(client)
            raise OSError("close failed")

        monkeypatch.setattr(fake, "close", fail_close)
        config = cast(Any, self.application._hook).email_config
        assert config._smtp_send("Subject: Test\n\nBody", ["alice@example.com"]) is not delivery_fails
        assert close_attempts == fake.created
        assert len(close_attempts) == 1
        assert any("SMTP connection cleanup failed" in message for message in caplog.messages)

    def test_smtp_unexpected_error_propagates_after_cleanup(self, monkeypatch):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()

        def unexpected_error(*args, **kwargs):
            raise RuntimeError("programming error")

        monkeypatch.setattr(fake, "sendmail", unexpected_error)
        config = cast(Any, self.application._hook).email_config
        with pytest.raises(RuntimeError, match="programming error"):
            config._smtp_send("Subject: Test\n\nBody", ["alice@example.com"])
        assert len(fake.created) == 1
        assert fake.created[0].closed

    def test_smtp_partial_refusal_reports_failure_and_closes(self, monkeypatch, caplog):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()

        def partial_refusal(*args, **kwargs):
            return {"bob@example.com": (550, b"recipient rejected")}

        monkeypatch.setattr(fake, "sendmail", partial_refusal)
        config = cast(Any, self.application._hook).email_config
        assert config._smtp_send(
            "Subject: Test\n\nBody", ["alice@example.com", "bob@example.com"]) is False
        assert fake.created[0].closed
        assert any("Failed to send email to bob@example.com" in message
                   for message in caplog.messages)
        assert not any("Email sent successfully" in message for message in caplog.messages)

    @pytest.mark.parametrize("itip_enabled", [False, True])
    @pytest.mark.parametrize("operation", ["create", "update", "delete"])
    @pytest.mark.parametrize("stage", ["connect", "sendmail"])
    def test_smtp_failure_preserves_calendar_write_and_later_recipient(
            self, monkeypatch, caplog, itip_enabled, operation, stage):
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook(dryrun="True")
        self.configure({"auth": {"type": "none"}, "hook": {
            "itip_enabled": itip_enabled,
            "itip_owner_map": '{"jeff": "unclesam@example.com"}',
            "from_email": "unclesam@example.com"}})
        self.mkcalendar("/calendar.ics/", login="jeff:password")
        path = "/calendar.ics/event1.ics"
        source = self._future_event().replace("END:VEVENT", "SEQUENCE:0\nEND:VEVENT")
        if operation != "create":
            self.put(path, source, login="jeff:password")
        self.configure({"hook": {"dryrun": "False"}})
        attempts = []
        original_sendmail = fake.sendmail

        def connect(*args, **kwargs):
            attempts.append("connect")
            if len(attempts) == 1:
                raise ConnectionRefusedError("first connection refused")
            return fake(*args, **kwargs)

        def sendmail(client, from_addr, to_addrs, msg):
            attempts.append(list(to_addrs))
            if len(attempts) == 1:
                raise TimeoutError("first delivery timed out")
            return original_sendmail(client, from_addr, to_addrs, msg)

        if stage == "connect":
            monkeypatch.setattr(smtplib, "SMTP", connect)
        else:
            monkeypatch.setattr(fake, "sendmail", sendmail)

        if operation == "delete":
            self.request("DELETE", path, check=200, login="jeff:password")
            self.request("GET", path, check=404, login="jeff:password")
        else:
            source = source.replace("SUMMARY:Event", "SUMMARY:Saved event")
            self.put(path, source, check=201 if operation == "create" else 204,
                     login="jeff:password")
            _, _, stored = self.request("GET", path, check=200, login="jeff:password")
            assert "SUMMARY:Saved event" in stored
        assert len(attempts) == 2
        assert len(fake.messages) == 1
        assert fake.messages[0][1] == ["johndoe@example.com"]
        assert all(client.closed for client in fake.created)
        assert any("SMTP delivery failed" in message for message in caplog.messages)

    def test_empty_smtp_security_raises(self) -> None:
        self._email_hook(security="")
        with pytest.raises(ValueError, match="SMTP security type cannot be empty"):
            cast(Any, self.application._hook).email_config._send_email(
                "subject line", "body", [type("A", (), {"email": "janedoe@example.com"})()])

    def test_starttls_and_tls_verify_mode(self, monkeypatch) -> None:
        expected = {
            "REQUIRED": ssl.CERT_REQUIRED,
            "OPTIONAL": ssl.CERT_OPTIONAL,
            "NONE": ssl.CERT_NONE,
        }
        for security in ("starttls", "tls"):
            for mode, verify in expected.items():
                fake = self._install_fake_smtp(monkeypatch)
                self._email_hook(security=security, verify_mode=mode)
                self.mkcalendar("/calendar.ics/", check=None)
                self.put("/calendar.ics/event1.ics",
                         self._future_event(summary="Mode " + mode + " " + security),
                         check=None)
                assert fake.created
                contexts = []
                for client in fake.created:
                    if client.starttls_context is not None:
                        contexts.append(client.starttls_context)
                    if client.context is not None:
                        contexts.append(client.context)
                assert contexts
                assert all(context.verify_mode == verify for context in contexts)

    def test_event_update_uses_updated_template(self, monkeypatch) -> None:
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()
        self.mkcalendar("/calendar.ics/")
        self.put("/calendar.ics/event1.ics", self._future_event())
        fake.messages.clear()
        self.put("/calendar.ics/event1.ics", self._future_event(summary="Event moved"), check=None)
        assert fake.messages
        assert any("An event you are invited to has been updated" in message
                   for _sender, _recipients, message in fake.messages)
        recipients = [address for _sender, addresses, _message in fake.messages for address in addresses]
        assert "janedoe@example.com" in recipients

    def test_non_calendar_item_sends_nothing(self, monkeypatch) -> None:
        fake = self._install_fake_smtp(monkeypatch)
        self._email_hook()
        self.create_addressbook("/contacts.vcf/")
        self.put("/contacts.vcf/contact1.vcf", get_file_content("contact1.vcf"))
        assert fake.messages == []
        assert fake.created == []
