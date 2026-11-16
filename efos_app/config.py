"""Configuration loading for EFOS.

The desktop build and the development source tree share one resolver: an
optional ``.env`` profile plus optional values saved by the first-launch
database setup wizard. Secrets are held in memory only and are never written to
logs. The optional low-privilege application role password lives in the Windows
Credential Manager, never in a flat file.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values

from .credentials import CredentialVault

APP_NAME = "EFOS"
APP_DISPLAY_NAME = "Executive Function Optimization System"
APP_VERSION = "2.1.0"
SCHEMA_VERSION = "2.1.0"
ORG_NAME = "EFOS"

RUNTIME_PROFILE_FILE = "runtime.json"
UI_STATE_FILE = "ui_state.json"
RUNTIME_SECRET_TARGET = "runtime-db"
OAUTH_SECRET_TARGET = "google-calendar-oauth"


class ConfigurationError(RuntimeError):
    """Raised when EFOS cannot locate a usable configuration profile."""


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _search_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.environ.get("EFOS_ENV_FILE", "").strip().strip('"')
    if override:
        roots.append(Path(override).expanduser())
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        roots.append(exe.parent / ".env")
        roots.extend(parent / ".env" for parent in list(exe.parents)[:5])
    else:
        source = Path(__file__).resolve().parent.parent
        roots.append(source / ".env")
        roots.extend(parent / ".env" for parent in list(source.parents)[:3])
    cwd = Path.cwd().resolve()
    roots.append(cwd / ".env")
    roots.extend(parent / ".env" for parent in list(cwd.parents)[:3])
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "EFOS" / ".env")
    roots.append(Path.home() / ".efos" / ".env")
    return _dedupe(roots)


def resolve_env_file() -> Path | None:
    """Return the first .env profile, or None when the runtime profile is used."""
    candidates = _search_roots()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def app_data_dir() -> Path:
    override = os.environ.get("EFOS_APP_DATA", "").strip()
    if override:
        candidates = [Path(override).expanduser()]
    else:
        candidates = []
        if os.environ.get("APPDATA"):
            candidates.append(Path(os.environ["APPDATA"]) / "EFOS")
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "EFOS")
        candidates.append(Path.home() / ".efos")
        candidates.append(Path.cwd() / ".efos-data")
    last_error: OSError | None = None
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base
        except OSError as exc:
            last_error = exc
    raise ConfigurationError(
        f"No writable EFOS data directory is available ({last_error})."
    )


def resource_path(relative: str) -> Path:
    """Resolve a bundled resource in source and PyInstaller builds."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / relative
    return Path(__file__).resolve().parent.parent / relative


def runtime_profile_path() -> Path:
    return app_data_dir() / RUNTIME_PROFILE_FILE


def ui_state_path() -> Path:
    return app_data_dir() / UI_STATE_FILE


def load_ui_state() -> dict:
    """Locally persisted appearance and window state (works without registry)."""
    try:
        value = json.loads(ui_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, ConfigurationError):
        return {}


def save_ui_state(values: dict) -> None:
    current = load_ui_state()
    current.update({key: value for key, value in values.items() if value is not None})
    try:
        path = ui_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, indent=2), encoding="utf-8")
        temporary.replace(path)
    except (OSError, ConfigurationError):
        return


def load_runtime_profile() -> dict:
    path = runtime_profile_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_runtime_profile(values: dict) -> None:
    """Persist non-secret runtime connection fields.

    The password is written to the Windows Credential Manager under
    ``runtime-db`` by :func:`save_runtime_password`.
    """
    path = runtime_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {
        key: values[key]
        for key in ("host", "port", "user", "database", "admin_user", "calendar_id")
        if key in values
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    temporary.replace(path)


def save_runtime_password(user: str, password: str) -> None:
    CredentialVault().set(RUNTIME_SECRET_TARGET, user, password)


@dataclass(frozen=True, slots=True)
class DbProfile:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = "efos"

    @property
    def display(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"

    def dsn(self, *, database: str | None = None, timeout: int = 5) -> str:
        return (
            f"host={self.host} port={self.port} dbname={database or self.database} "
            f"user={self.user} password={self.password} connect_timeout={timeout} "
            "application_name=EFOS"
        )

    def maintenance_dsn(self, *, timeout: int = 5) -> str:
        return self.dsn(database="postgres", timeout=timeout)


@dataclass(frozen=True, slots=True)
class AppConfig:
    env_path: Path | None
    db_host: str
    db_port: int
    db_user: str
    db_name: str
    db_password: str = field(repr=False)
    admin_user: str = "postgres"
    admin_password: str = field(default="", repr=False)
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    google_calendar_id: str = ""
    data_dir: Path = field(default_factory=app_data_dir)

    @property
    def google_enabled(self) -> bool:
        return bool(
            self.google_client_id and self.google_client_secret and self.google_calendar_id
        )

    @property
    def deepseek_enabled(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def db_display(self) -> str:
        return self.profile.display

    @property
    def profile(self) -> DbProfile:
        return DbProfile(
            self.db_host, self.db_port, self.db_user, self.db_password, self.db_name
        )

    @property
    def admin_profile(self) -> DbProfile:
        return DbProfile(
            self.db_host,
            self.db_port,
            self.admin_user,
            self.admin_password or self.db_password,
            "postgres",
        )

    def dsn(self, *, database: str | None = None) -> str:
        return self.profile.dsn(database=database)

    def with_database(self, database: str) -> "AppConfig":
        return replace(self, db_name=database)

    @classmethod
    def load(cls) -> "AppConfig":
        env_path = resolve_env_file()
        file_values = dotenv_values(str(env_path)) if env_path else {}
        runtime = load_runtime_profile()

        def value(key: str, default: str = "") -> str:
            raw = file_values.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
            return os.environ.get(key, default).strip()

        password = value("EFOS_DB_PASSWORD")
        user = runtime.get("user") or value("EFOS_DB_USER", "postgres")
        if runtime:
            vault = CredentialVault().get(RUNTIME_SECRET_TARGET)
            if vault and vault[0] == user and vault[1]:
                password = vault[1]
        if not password:
            raise ConfigurationError(
                "No PostgreSQL password is configured. Add EFOS_DB_PASSWORD to a "
                ".env profile or complete the database setup wizard."
            )

        port_text = str(runtime.get("port") or value("EFOS_DB_PORT", "5432"))
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ConfigurationError("EFOS_DB_PORT must be an integer.") from exc

        database = runtime.get("database") or value("EFOS_DB_NAME", "efos")
        host = runtime.get("host") or value("EFOS_DB_HOST", "localhost")
        admin_user = runtime.get("admin_user") or value("EFOS_ADMIN_USER", "postgres")
        admin_password = value("EFOS_ADMIN_PASSWORD")

        return cls(
            env_path=env_path,
            db_host=host,
            db_port=port,
            db_user=user,
            db_name=database,
            db_password=password,
            admin_user=admin_user,
            admin_password=admin_password,
            deepseek_api_key=value("DEEPSEEK_API_KEY"),
            deepseek_base_url=value(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_model=value("DEEPSEEK_MODEL", "deepseek-chat"),
            google_client_id=value("EFOS_GOOGLE_CLIENT_ID"),
            google_client_secret=value("EFOS_GOOGLE_CLIENT_SECRET"),
            google_calendar_id=runtime.get("calendar_id")
            or value("EFOS_GOOGLE_CALENDAR_ID"),
        )
