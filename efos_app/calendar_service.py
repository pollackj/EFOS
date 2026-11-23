"""Google Calendar OAuth 2.0 (read-only) and incremental local cache.

The legacy public-API-key path is deleted. Access and refresh tokens are stored
only in the Windows Credential Manager; PostgreSQL holds account metadata and a
local event snapshot. Synchronization is one-way Google → EFOS, using an
incremental ``syncToken`` delta on boot and at runtime intervals. If a sync
fails, the previous snapshot is retained and the UI marks it stale.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
import requests
from .config import AppConfig, OAUTH_SECRET_TARGET
from .credentials import CredentialVault
from .errors import AuthenticationRequiredError, ExternalServiceError
from .models import CalendarItem, SyncState
from .repository import Repository

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)
LOOPBACK_PORTS = (8765, 8766, 8767, 8768)
DEFAULT_OAUTH_TIMEOUT = 90.0

def oauth_timeout() -> float:
    """Seconds to wait for the loopback callback (``EFOS_GOOGLE_OAUTH_TIMEOUT``)."""
    raw = os.environ.get("EFOS_GOOGLE_OAUTH_TIMEOUT", "")
    try: value = float(raw)
    except (TypeError, ValueError): return DEFAULT_OAUTH_TIMEOUT
    return max(15.0, min(600.0, value))

@contextmanager
def guarded_standard_streams():
    """Guarantee ``sys.stdout``/``sys.stderr`` exist during the OAuth hand-off.

    ``pythonw`` and PyInstaller ``--windowed`` builds start with no console, so
    both streams are ``None``. ``http.server`` writes to ``sys.stderr`` and some
    browser launchers touch ``sys.stdout``; a ``None`` stream raises inside the
    OAuth worker and leaves the Calendar button stuck on "Waiting for Google…".
    Throwaway sinks are installed for the duration and the real streams are
    restored afterwards.
    """
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    if sys.stdout is None: sys.stdout = io.StringIO()
    if sys.stderr is None: sys.stderr = io.StringIO()
    try: yield
    finally: sys.stdout, sys.stderr = saved_stdout, saved_stderr

def open_authorization_url(url: str) -> bool:
    """Open ``url`` in the user's default browser, with Windows fallbacks."""
    opened = False
    try: opened = bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception:  # pragma: no cover - platform/browser specific
        log.debug("webbrowser.open failed", exc_info=True)
    if not opened:
        try: opened = bool(webbrowser.open_new_tab(url))
        except Exception:  # pragma: no cover - platform/browser specific
            log.debug("webbrowser.open_new_tab failed", exc_info=True)
    if not opened and sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
            opened = True
        except OSError:
            try:
                subprocess.Popen(  # noqa: S603,S607
                    ["cmd", "/c", "start", "", url],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                opened = True
            except OSError: log.warning("No browser could be launched for the Google OAuth URL.")
    return opened

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        self.server.received = {key: values[0] for key, values in params.items()}
        body = (
            "<html><body style='font-family:Segoe UI;background:#0f131a;color:#e9edf5;"
            "padding:40px'><h2>EFOS</h2><p>Google Calendar authorization received. "
            "You can close this window and return to EFOS.</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

class _CallbackServer(HTTPServer):
    received: dict[str, str] | None = None

class GoogleOAuthClient:
    def __init__(self, config: AppConfig, vault: CredentialVault | None = None):
        self.config = config
        self.vault = vault or CredentialVault()

    @property
    def configured(self) -> bool: return bool(self.config.google_client_id and self.config.google_client_secret)

    def _load(self) -> dict[str, Any] | None:
        raw = self.vault.get_secret(OAUTH_SECRET_TARGET)
        if not raw: return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except ValueError: return None

    def _save(self, tokens: dict[str, Any]) -> None:
        self.vault.set(OAUTH_SECRET_TARGET, self.config.google_client_id[:40] or "google", json.dumps(tokens))

    def connected(self) -> bool:
        tokens = self._load()
        return bool(tokens and (tokens.get("refresh_token") or tokens.get("access_token")))

    def clear(self) -> None: self.vault.delete(OAUTH_SECRET_TARGET)

    def access_token(self) -> str:
        tokens = self._load()
        if not tokens: raise AuthenticationRequiredError("Google Calendar is not authorized. Connect it from the Calendar page.")
        expires_at = float(tokens.get("expires_at") or 0)
        if tokens.get("access_token") and expires_at - 60 > time.time(): return str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise AuthenticationRequiredError(
                "Google Calendar authorization expired and no refresh token is stored."
            )
        refreshed = self._token_request(
            {
                "client_id": self.config.google_client_id,
                "client_secret": self.config.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        tokens.update(
            {
                "access_token": refreshed.get("access_token"),
                "expires_at": time.time() + float(refreshed.get("expires_in") or 3500),
            }
        )
        if refreshed.get("refresh_token"): tokens["refresh_token"] = refreshed["refresh_token"]
        self._save(tokens)
        return str(tokens["access_token"])

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        try: response = requests.post(TOKEN_ENDPOINT, data=payload, timeout=20)
        except requests.RequestException as exc:
            raise ExternalServiceError(f"Google token endpoint is unreachable ({type(exc).__name__}).") from exc
        if response.status_code != 200:
            raise AuthenticationRequiredError(
                f"Google authorization failed (HTTP {response.status_code}). "
                "Reconnect Google Calendar."
            )
        try: return response.json()
        except ValueError as exc: raise ExternalServiceError("Google returned a malformed token payload.") from exc

    def _authorization(self, port: int) -> tuple[str, str, str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(24)
        redirect = f"http://127.0.0.1:{port}/"
        query = (
            f"?client_id={quote(self.config.google_client_id, safe='')}"
            f"&redirect_uri={quote(redirect, safe='')}"
            "&response_type=code"
            f"&scope={quote(' '.join(SCOPES), safe='')}"
            "&access_type=offline&prompt=consent&include_granted_scopes=true"
            f"&state={quote(state, safe='')}"
            f"&code_challenge={quote(challenge, safe='')}&code_challenge_method=S256"
        )
        return AUTH_ENDPOINT + query, state, verifier, redirect

    def authorize_interactive(self) -> dict[str, Any]:
        if not self.configured:
            raise ExternalServiceError(
                "Google OAuth client credentials are not configured. Set "
                "EFOS_GOOGLE_CLIENT_ID and EFOS_GOOGLE_CLIENT_SECRET."
            )
        # Mirrors InstalledAppFlow.run_local_server(open_browser=True,
        # timeout=...): the loopback server never writes to a missing console,
        # the authorization URL is opened explicitly with fallbacks, and the
        # hand-off has a hard deadline instead of blocking the worker forever.
        with guarded_standard_streams(): return self._run_local_server_flow()

    def _run_local_server_flow(self) -> dict[str, Any]:
        server: _CallbackServer | None = None
        last_error: Exception | None = None
        port = LOOPBACK_PORTS[0]
        for candidate in LOOPBACK_PORTS:
            try:
                server = _CallbackServer(("127.0.0.1", candidate), _OAuthCallbackHandler)
                port = candidate
                break
            except OSError as exc:
                last_error = exc
                continue
        if server is None:
            raise ExternalServiceError(
                f"Could not open the OAuth loopback listener ({last_error}). "
                "Close the program holding ports 8765–8768 and try again."
            )
        url, state, verifier, redirect = self._authorization(port)
        timeout = oauth_timeout()
        log.info("Google OAuth authorization URL: %s", url)
        if not open_authorization_url(url):
            log.warning(
                "The default browser did not accept the Google OAuth URL; the "
                "user can open it manually: %s",
                url,
            )
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and not server.received:
                # Short per-request timeout: the loop re-checks the deadline so
                # a missing browser can never block for the full wait.
                server.timeout = max(0.2, min(1.0, deadline - time.time()))
                try: server.handle_request()
                except OSError: break
        finally: server.server_close()
        received = server.received or {}
        if received.get("error"):
            detail = received.get("error_description") or received["error"]
            raise ExternalServiceError(f"Google authorization was denied ({detail}).")
        code = received.get("code")
        if not code:
            # No callback at all (timeout) or a callback without a code: this is
            # the actionable path the Calendar banner surfaces.
            raise ExternalServiceError(
                "Google authorization timed out after "
                f"{int(timeout)} seconds. Check your browser for the consent tab, "
                "press Connect Google to try again, or open this URL manually: "
                f"{url}"
            )
        if received.get("state") != state:
            raise ExternalServiceError(
                "Google authorization state did not match. Press Connect Google "
                "to start a fresh authorization."
            )
        tokens = self._token_request(
            {
                "client_id": self.config.google_client_id,
                "client_secret": self.config.google_client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
            }
        )
        if not tokens.get("refresh_token"):
            raise ExternalServiceError(
                "Google did not return a refresh token. Remove prior EFOS access in "
                "your Google account and reconnect."
            )
        tokens["expires_at"] = time.time() + float(tokens.get("expires_in") or 3500)
        self._save(tokens)
        return tokens

@dataclass(slots=True)
class SyncResult:
    state: SyncState
    imported: int = 0

class CalendarService:
    API_ROOT = CALENDAR_API

    def __init__(self, config: AppConfig, repo: Repository, vault: CredentialVault | None = None):
        self.config = config
        self.repo = repo
        self.vault = vault or CredentialVault()
        self.oauth = GoogleOAuthClient(config, self.vault)

    @property
    def account_id(self) -> str: return "google"

    # ------------------------------------------------------------- sync status
    def sync_state(self) -> SyncState:
        state = self.repo.calendar_sync_state()
        connected = self.oauth.connected()
        state.connected = connected
        if not connected:
            if state.status in ("ok", "connected") or state.last_sync_at:
                state.status = "stale"
                if not state.message: state.message = "Google Calendar authorization expired."
            elif state.status in ("never", "connected"): state.status = "disconnected"
        elif state.status == "ok" and not state.last_sync_at:
            state.status = "stale"
            state.message = "No completed synchronization yet."
        return state

    # ---------------------------------------------------------------- OAuth
    def connect(self) -> SyncState:
        tokens = self.oauth.authorize_interactive()
        calendar_id = self.config.google_calendar_id
        if not calendar_id: raise ExternalServiceError("EFOS_GOOGLE_CALENDAR_ID is required to synchronize a calendar.")
        email = calendar_id
        try:
            access = str(tokens.get("access_token"))
            response = requests.get(
                f"{self.API_ROOT}/calendars/{quote(calendar_id, safe='')}",
                headers={"Authorization": f"Bearer {access}"},
                timeout=20,
            )
            if response.status_code == 200:
                payload = response.json()
                email = payload.get("id") or calendar_id
        except requests.RequestException: pass
        self.repo.save_calendar_account(
            self.account_id,
            email,
            calendar_id,
            sync_token=None,
            status="connected",
            last_sync_at=None,
        )
        try: self.sync(force_full=True)
        except Exception: pass
        return self.sync_state()

    def disconnect(self) -> None:
        self.oauth.clear()
        account = self.repo.calendar_account()
        if account:
            self.repo.save_calendar_account(
                self.account_id,
                account.get("email") or "",
                account.get("calendar_id") or self.config.google_calendar_id,
                status="disconnected",
                error="Google Calendar was disconnected.",
            )

    # ---------------------------------------------------------------- sync
    def sync(self, *, force_full: bool = False) -> SyncState:
        if not self.config.google_calendar_id: return SyncState(status="disconnected", message="NO_CALENDAR_ID", connected=False)
        try: token = self.oauth.access_token()
        except AuthenticationRequiredError as exc: return SyncState(status="disconnected", message=str(exc), connected=False)

        account = self.repo.calendar_account()
        sync_token = None if force_full else (account or {}).get("sync_token")
        try:
            if sync_token:
                events, next_token, full = self._fetch_incremental(token, sync_token)
                if full:
                    assets = self._fetch_full(token)
                    events, next_token = assets
                    imported = self.repo.replace_calendar_events(self.account_id, self.config.google_calendar_id, events)
                else: imported = self.repo.upsert_calendar_events(self.account_id, self.config.google_calendar_id, events)
            else:
                events, next_token = self._fetch_full(token)
                imported = self.repo.replace_calendar_events(self.account_id, self.config.google_calendar_id, events)
        except AuthenticationRequiredError as exc:
            # Preserve the cache and mark the snapshot stale, exactly as for an
            # offline network failure.
            self.repo.save_calendar_account(
                self.account_id,
                (account or {}).get("email") or self.config.google_calendar_id,
                self.config.google_calendar_id,
                status="stale",
                error=str(exc)[:400],
            )
            state = self.repo.calendar_sync_state()
            state.connected = False
            return state
        except ExternalServiceError as exc:
            # Failsafe: retain the last successful snapshot and mark it stale.
            self.repo.save_calendar_account(
                self.account_id,
                (account or {}).get("email") or self.config.google_calendar_id,
                self.config.google_calendar_id,
                status="stale",
                error=str(exc)[:400],
            )
            state = self.repo.calendar_sync_state()
            state.connected = True
            return state

        now = datetime.now().replace(microsecond=0)
        self.repo.save_calendar_account(
            self.account_id,
            (account or {}).get("email") or self.config.google_calendar_id,
            self.config.google_calendar_id,
            sync_token=next_token,
            status="ok",
            error=None,
            last_sync_at=now,
        )
        state = self.repo.calendar_sync_state()
        state.connected = True
        return state

    def _fetch_full(self, token: str) -> tuple[list[dict[str, Any]], str | None]:
        start = datetime.now() - timedelta(days=365)
        end = datetime.now() + timedelta(days=730)
        return self._list_events(
            token,
            {
                "timeMin": _rfc3339(start),
                "timeMax": _rfc3339(end),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "2500",
                "showDeleted": "false",
            },
        )

    def _fetch_incremental(self, token: str, sync_token: str) -> tuple[list[dict[str, Any]], str | None, bool]:
        try:
            events, next_token = self._list_events(token, {"syncToken": sync_token, "maxResults": "2500", "showDeleted": "true"})
            return events, next_token, False
        except _SyncTokenExpired: return [], None, True

    def _list_events(self, token: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
        events: list[dict[str, Any]] = []
        next_token: str | None = None
        page_token: str | None = None
        calendar_id = quote(self.config.google_calendar_id, safe="")
        for _ in range(20):
            query = dict(params)
            if page_token: query["pageToken"] = page_token
            try:
                response = requests.get(
                    f"{self.API_ROOT}/calendars/{calendar_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    params=query,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise ExternalServiceError(f"Google Calendar is unreachable ({type(exc).__name__}).") from exc
            if response.status_code == 410: raise _SyncTokenExpired()
            if response.status_code in (401, 403):
                raise AuthenticationRequiredError(
                    "Google Calendar rejected the stored authorization. "
                    "Reconnect Google Calendar."
                )
            if response.status_code != 200: raise ExternalServiceError(f"Google Calendar returned HTTP {response.status_code}.")
            try: payload = response.json()
            except ValueError as exc: raise ExternalServiceError("Google Calendar returned malformed JSON.") from exc
            for item in payload.get("items", []):
                parsed = parse_google_event(item)
                if parsed: events.append(parsed)
            page_token = payload.get("nextPageToken")
            next_token = payload.get("nextSyncToken") or next_token
            if not page_token: break
        return events, next_token

class _SyncTokenExpired(Exception): pass

def _rfc3339(value: datetime) -> str:
    local = value.astimezone() if value.tzinfo is None else value.astimezone()
    return local.astimezone().isoformat()

def _to_local_naive(value: str, *, is_date: bool) -> tuple[datetime, bool]:
    """Map every incoming datetime into the local Windows system timezone.

    The schema stores ``timestamp without time zone``; DST is resolved by the
    OS at parse time, not by a fixed offset.
    """
    if is_date:
        parsed = date.fromisoformat(value)
        return datetime.combine(parsed, dt_time.min), True
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None: parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0), False

def parse_google_event(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("id"): return None
    start_data = item.get("start") or {}
    end_data = item.get("end") or {}
    try:
        if "dateTime" in start_data: start, all_day = _to_local_naive(start_data["dateTime"], is_date=False)
        elif "date" in start_data: start, all_day = _to_local_naive(start_data["date"], is_date=True)
        else: return None
        if "dateTime" in end_data: end, _ = _to_local_naive(end_data["dateTime"], is_date=False)
        elif "date" in end_data: end, _ = _to_local_naive(end_data["date"], is_date=True)
        else: end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except (ValueError, TypeError): return None
    if end <= start: return None
    return {
        "id": str(item["id"]),
        "summary": item.get("summary") or "(untitled calendar event)",
        "description": item.get("description") or "",
        "location": item.get("location") or "",
        "status": item.get("status") or "confirmed",
        "start_at": start,
        "end_at": end,
        "all_day": all_day,
    }

def collision_items(repo: Repository, start: datetime, end: datetime) -> list[CalendarItem]:
    """Offline collision detection against the persisted snapshot."""
    items = repo.calendar_events_between(start, end)
    return items
