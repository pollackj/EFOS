"""Shared fixtures: disposable PostgreSQL database and offscreen Qt."""
from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault(
    "EFOS_APP_DATA", str(Path(__file__).resolve().parents[1] / ".efos-test-data")
)

import psycopg2
import pytest
from dotenv import load_dotenv

from efos_app.config import AppConfig
from efos_app.service import EFOSService

TRUNCATE_SQL = """
TRUNCATE public.assistant_messages, public.assistant_threads,
         public.custom_queries, public.calendar_events, public.calendar_accounts,
         public.task_notes, public.project_notes, public.condition_notes,
         public.notes, public.task_recurrence_rules,
         public.task_preconditions, public.project_preconditions,
         public.task_prerequisites, public.project_prerequisites,
         public.tasks, public.projects, public.conditions, public.events
RESTART IDENTITY CASCADE
"""


@pytest.fixture(scope="session")
def test_database() -> str:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    password = os.environ["EFOS_DB_PASSWORD"]
    name = "efos_test_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(
        host="localhost", port=5432, user="postgres", password=password, dbname="postgres"
    )
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def service(test_database: str) -> EFOSService:
    base = AppConfig.load()
    config = replace(base, db_name=test_database)
    svc = EFOSService(config)
    svc.start()
    with svc.db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(TRUNCATE_SQL)
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from efos_app.ui.theme import apply_theme

    apply_theme(app, "dark")
    yield app


@pytest.fixture(autouse=True)
def google_offline(monkeypatch):
    """Keep the suite off Google's network and out of the browser.

    A developer workstation can hold real Google OAuth tokens in the Windows
    Credential Manager, and the Calendar page now auto-syncs stored credentials
    when they are stale. UI tests must stay deterministic and offline, so the
    token/connection probes are stubbed for every test. The OAuth hand-off
    itself is covered directly in ``tests/test_calendar_service.py``.
    """
    from efos_app.calendar_service import GoogleOAuthClient
    from efos_app.errors import AuthenticationRequiredError

    monkeypatch.setattr(GoogleOAuthClient, "connected", lambda self: False)

    def _denied(self):
        raise AuthenticationRequiredError(
            "Google Calendar credentials are disabled in the test suite."
        )

    monkeypatch.setattr(GoogleOAuthClient, "access_token", _denied)
