"""Google Calendar OAuth hand-off: stream guards, browser launch, timeouts."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from efos_app import calendar_service
from efos_app.errors import ExternalServiceError


def _client() -> calendar_service.GoogleOAuthClient:
    return calendar_service.GoogleOAuthClient(
        SimpleNamespace(google_client_id="client-id", google_client_secret="secret")
    )


def test_oauth_timeout_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("EFOS_GOOGLE_OAUTH_TIMEOUT", raising=False)
    assert calendar_service.oauth_timeout() == calendar_service.DEFAULT_OAUTH_TIMEOUT
    monkeypatch.setenv("EFOS_GOOGLE_OAUTH_TIMEOUT", "5")
    assert calendar_service.oauth_timeout() == 15.0
    monkeypatch.setenv("EFOS_GOOGLE_OAUTH_TIMEOUT", "1000")
    assert calendar_service.oauth_timeout() == 600.0
    monkeypatch.setenv("EFOS_GOOGLE_OAUTH_TIMEOUT", "not-a-number")
    assert calendar_service.oauth_timeout() == calendar_service.DEFAULT_OAUTH_TIMEOUT


def test_windowed_builds_without_stdout_or_stderr_can_authorize(monkeypatch):
    # PyInstaller --windowed and pythonw start with both streams set to None;
    # http.server writes to stderr, so the guard must restore usable sinks.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with calendar_service.guarded_standard_streams():
        assert sys.stdout is not None
        assert sys.stderr is not None
        print("console write is safe")
        sys.stderr.write("stderr write is safe\n")
    assert sys.stdout is None
    assert sys.stderr is None


def test_authorization_url_falls_back_when_browser_launcher_refuses(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(calendar_service.webbrowser, "open", lambda *a, **k: False)
    monkeypatch.setattr(
        calendar_service.webbrowser, "open_new_tab", lambda *a, **k: False
    )
    monkeypatch.setattr(calendar_service, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        calendar_service.os,
        "startfile",
        lambda url: opened.append(url),
        raising=False,
    )
    assert calendar_service.open_authorization_url("https://example.test/auth")
    assert opened == ["https://example.test/auth"]


def test_authorize_interactive_reports_missing_oauth_configuration():
    client = calendar_service.GoogleOAuthClient(
        SimpleNamespace(google_client_id="", google_client_secret="")
    )
    assert client.configured is False
    with pytest.raises(ExternalServiceError) as info:
        client.authorize_interactive()
    assert "EFOS_GOOGLE_CLIENT_ID" in str(info.value)


def test_local_server_flow_times_out_with_an_actionable_message(monkeypatch):
    class FakeServer:
        def __init__(self, address, handler):
            self.received = None
            self.timeout = None
            self.closed = False

        def handle_request(self) -> None:
            return None

        def server_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(calendar_service, "_CallbackServer", FakeServer)
    monkeypatch.setattr(calendar_service, "oauth_timeout", lambda: 0.0)
    monkeypatch.setattr(calendar_service, "open_authorization_url", lambda url: False)
    with pytest.raises(ExternalServiceError) as info:
        _client()._run_local_server_flow()
    message = str(info.value)
    assert "timed out" in message
    assert "Connect Google" in message
    assert "https://accounts.google.com" in message


def test_local_server_flow_reports_denied_consent(monkeypatch):
    class FakeServer:
        def __init__(self, address, handler):
            self.received = {
                "error": "access_denied",
                "error_description": "The user denied the request",
            }
            self.timeout = None
            self.closed = False

        def handle_request(self) -> None:
            return None

        def server_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(calendar_service, "_CallbackServer", FakeServer)
    monkeypatch.setattr(calendar_service, "open_authorization_url", lambda url: True)
    with pytest.raises(ExternalServiceError) as info:
        _client()._run_local_server_flow()
    assert "denied" in str(info.value)
    assert "The user denied the request" in str(info.value)
