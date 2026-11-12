"""PostgreSQL lifecycle, canonical schema bootstrap, and query helpers.

Startup follows the specification exactly:

1. read the connection profile;
2. open a maintenance connection to the server's ``postgres`` database;
3. check whether the target database exists;
4. create it outside a transaction when missing;
5. reconnect to the target and execute the canonical schema byte-for-byte;
6. verify every table, index, trigger, enum, and required column;
7. abort with a detailed discrepancy report if a partial or mismatched schema
   is found. Destructive automatic recovery is never attempted.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2 import pool, sql
from psycopg2.extras import RealDictCursor

from .config import AppConfig, DbProfile, SCHEMA_VERSION, resource_path
from .errors import (
    DatabaseConnectionError,
    EFOSError,
    ProvisioningRequiredError,
    ReadOnlyQueryError,
    SchemaValidationError,
)
from .models import ConnectionStatus

log = logging.getLogger(__name__)

EFOS_TABLES = frozenset(
    {
        "schema_meta",
        "conditions",
        "notes",
        "tasks",
        "projects",
        "task_preconditions",
        "project_preconditions",
        "task_prerequisites",
        "project_prerequisites",
        "task_notes",
        "project_notes",
        "condition_notes",
        "task_recurrence_rules",
        "events",
        "assistant_threads",
        "assistant_messages",
        "custom_queries",
        "calendar_accounts",
        "calendar_events",
    }
)

EFOS_ENUMS: dict[str, list[str]] = {
    "project_state": ["active", "stalled", "shelved", "resolved"],
    "task_state": ["available", "unavailable", "completed", "deferred"],
    "recurrence_frequency": ["daily", "weekly", "monthly", "yearly"],
}

EFOS_TRIGGERS = frozenset(
    {
        "auto_project_fsm_history",
        "auto_task_fsm_history",
        "refresh_note_timestamp",
        "touch_custom_query_timestamp",
        "touch_assistant_thread_timestamp",
        "events_immutable_update_delete",
        "validate_task_available_before_update",
        "validate_project_resolved_before_update",
        "project_preconditions_sync_flag",
        "project_prerequisites_sync_flag",
    }
)

EFOS_INDEXES = frozenset(
    {
        "idx_tasks_schedule",
        "idx_tasks_state",
        "idx_projects_state",
        "idx_events_occurred",
        "idx_events_entity",
        "idx_assistant_messages_thread",
        "idx_assistant_threads_updated",
        "idx_custom_queries_sort",
        "idx_calendar_events_window",
        "idx_calendar_events_account",
        "project_prerequisites_project_source_uq",
        "project_prerequisites_task_source_uq",
        "task_prerequisites_project_source_uq",
        "task_prerequisites_task_source_uq",
    }
)

EFOS_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "tasks": {
        "id",
        "name",
        "is_recurring",
        "current_state",
        "previous_state",
        "created_at",
        "scheduled_start",
        "scheduled_end",
    },
    "projects": {
        "id",
        "name",
        "current_state",
        "previous_state",
        "resolution_conditions_unknown",
        "created_at",
    },
    "conditions": {"id", "display_name", "evaluation_state", "created_at"},
    "task_preconditions": {"task_id", "condition_id"},
    "project_preconditions": {"project_id", "condition_id"},
    "task_prerequisites": {"task_id", "source_task_id", "source_project_id"},
    "project_prerequisites": {"project_id", "source_task_id", "source_project_id"},
    "task_recurrence_rules": {
        "task_id",
        "frequency",
        "interval",
        "by_day",
        "by_month_day",
        "occurrence_count",
        "until_date",
        "reset_hour",
        "reset_minute",
        "last_reset_window",
    },
    "notes": {"id", "content", "updated_at"},
    "events": {
        "id",
        "occurred_at",
        "event_type",
        "entity_type",
        "entity_id",
        "summary",
        "details",
    },
    "assistant_threads": {"id", "title", "created_at", "updated_at"},
    "assistant_messages": {"id", "thread_id", "role", "content", "metadata", "created_at"},
    "custom_queries": {
        "id",
        "label",
        "prompt",
        "favorite",
        "sort_index",
        "created_at",
        "updated_at",
    },
    "calendar_accounts": {
        "id",
        "calendar_id",
        "last_sync_at",
        "sync_token",
        "sync_status",
        "last_error",
    },
    "calendar_events": {
        "id",
        "account_id",
        "calendar_id",
        "summary",
        "description",
        "location",
        "start_at",
        "end_at",
        "all_day",
        "status",
    },
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call|do|"
    r"copy|merge|vacuum|analyze|refresh|reindex|cluster|comment|security|"
    r"set|reset|lock|listen|notify|unlisten|discard|prepare|execute|deallocate|"
    r"dblink|lo_import|lo_export|pg_read_file|pg_sleep)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)
_LIMIT_RE = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)


def quote_identifier(name: str) -> str:
    if _IDENTIFIER.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _clean_db_error(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    text = re.sub(r"password\s*=\s*\S+", "password=***", text, flags=re.IGNORECASE)
    text = re.sub(r"(postgres(?:ql)?://)[^\s]+", r"\1***", text)
    return text[:420]


def schema_sql_text() -> str:
    path = resource_path("efos_app/sql/schema.sql")
    if not path.is_file():
        raise SchemaValidationError(f"Bundled schema was not found at {path}.")
    text = path.read_text(encoding="utf-8")
    # psql meta-commands are not SQL; the canonical file is pure SQL, but strip
    # defensively so a stray backslash line cannot break psycopg2.
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("\\")]
    return "\n".join(lines)


def connect_target(profile: DbProfile, timeout: int = 5) -> Any:
    try:
        return psycopg2.connect(profile.dsn(timeout=timeout))
    except psycopg2.Error as exc:
        raise DatabaseConnectionError(
            f"PostgreSQL at {profile.display} is unreachable: {_clean_db_error(exc)}"
        ) from exc


def test_connection(profile: DbProfile) -> tuple[bool, str]:
    try:
        conn = psycopg2.connect(profile.dsn(timeout=5))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, current_database()")
                user, database = cur.fetchone()
        finally:
            conn.close()
        return True, f"Connected as {user} to {database}."
    except psycopg2.Error as exc:
        return False, _clean_db_error(exc)


def provision_database(admin: DbProfile, target: DbProfile) -> None:
    """First-launch setup used by the guided onboarding wizard."""
    ensure_database(None, admin_profile=admin, target_profile=target)


def ensure_database(
    config: AppConfig | None = None,
    *,
    admin_profile: DbProfile | None = None,
    target_profile: DbProfile | None = None,
) -> None:
    """Create, initialize, and verify the target database.

    Raises ProvisioningRequiredError when the admin role lacks CREATEDB and the
    target database does not exist. Raises SchemaValidationError when the target
    exists but its schema is partial or mismatched.
    """
    admin = admin_profile or (config.admin_profile if config else None)
    target = target_profile or (config.profile if config else None)
    if admin is None or target is None:
        raise DatabaseConnectionError("A database connection profile is required.")

    # Daily app-mode fast path: an initialized database is verified with the
    # low-privilege application role. Administrative credentials are only needed
    # for first-launch provisioning, schema creation, or privilege grants.
    empty_confirmed = False
    try:
        probe = psycopg2.connect(target.dsn())
    except pg_errors.InvalidCatalogName:
        probe = None
    except psycopg2.Error:
        probe = None
    if probe is not None:
        try:
            with probe.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = 'public'
                       AND table_name = ANY(%s)
                    """,
                    (sorted(EFOS_TABLES),),
                )
                existing = {row[0] for row in cur.fetchall()}
            if existing == EFOS_TABLES:
                with probe.cursor() as cur:
                    verify_schema(cur)
                probe.close()
                return
            if existing:
                missing = sorted(EFOS_TABLES - existing)
                probe.close()
                raise SchemaValidationError(
                    "The target database contains a partial EFOS schema. Startup was "
                    "aborted and the database was not modified. Restore a clean "
                    "database or remove the partial EFOS objects, then relaunch.",
                    [f"Missing table: public.{name}" for name in missing],
                )
            probe.close()
            empty_confirmed = True
        except SchemaValidationError:
            probe.close()
            raise
        except psycopg2.Error:
            try:
                probe.close()
            except psycopg2.Error:
                pass

    # 1-2. Maintenance connection to the server default database.
    try:
        maintenance = psycopg2.connect(admin.maintenance_dsn())
    except psycopg2.Error as exc:
        if empty_confirmed:
            raise ProvisioningRequiredError(
                "The target database exists but contains no EFOS schema. "
                "Administrative credentials are required to initialize it; provide "
                "them in the database setup wizard."
            ) from exc
        raise DatabaseConnectionError(
            "PostgreSQL maintenance connection to "
            f"{admin.host}:{admin.port}/postgres failed: {_clean_db_error(exc)}"
        ) from exc
    maintenance.autocommit = True
    created = False
    try:
        with maintenance.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target.database,))
            exists = cur.fetchone() is not None
            if not exists:
                try:
                    cur.execute(f"CREATE DATABASE {quote_identifier(target.database)}")
                except pg_errors.InsufficientPrivilege as exc:
                    raise ProvisioningRequiredError(
                        "The configured PostgreSQL role does not have CREATEDB "
                        "privilege. Complete the database setup wizard with an "
                        "administrative role, then save a low-privilege application "
                        "role for daily use."
                    ) from exc
                created = True
    finally:
        maintenance.close()

    # 5. Reconnect to the target and inspect/initialize the schema.
    try:
        conn = psycopg2.connect(admin.dsn(database=target.database))
    except pg_errors.InvalidCatalogName as exc:
        raise DatabaseConnectionError(
            f"The target database {target.database!r} could not be opened."
        ) from exc
    except psycopg2.Error as exc:
        raise DatabaseConnectionError(
            f"PostgreSQL at {target.display} refused the connection: {_clean_db_error(exc)}"
        ) from exc

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('efos_schema_bootstrap'))")
            cur.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_name = ANY(%s)
                """,
                (sorted(EFOS_TABLES),),
            )
            existing = {row[0] for row in cur.fetchall()}
            if existing and existing != EFOS_TABLES:
                missing = sorted(EFOS_TABLES - existing)
                raise SchemaValidationError(
                    "The target database contains a partial EFOS schema. Startup was "
                    "aborted and the database was not modified. Restore a clean "
                    "database or remove the partial EFOS objects, then relaunch.",
                    [f"Missing table: public.{name}" for name in missing],
                )
            if not existing:
                ddl = schema_sql_text()
                cur.execute(ddl)
                log.info("Initialized EFOS schema in %s", target.display)
            verify_schema(cur)
        conn.commit()
    except SchemaValidationError:
        conn.rollback()
        raise
    except psycopg2.Error as exc:
        conn.rollback()
        raise DatabaseConnectionError(
            f"Schema initialization failed at {target.display}: {_clean_db_error(exc)}"
        ) from exc
    finally:
        conn.close()

    # 10.3 Mode B: grant only routine DML to the low-privilege application role.
    if target.user != admin.user:
        _grant_application_role(admin, target, created=created)


def _grant_application_role(admin: DbProfile, target: DbProfile, *, created: bool) -> None:
    try:
        conn = psycopg2.connect(admin.dsn(database=target.database))
    except psycopg2.Error as exc:
        raise DatabaseConnectionError(_clean_db_error(exc)) from exc
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.user,))
            if cur.fetchone() is None:
                if not target.password:
                    raise ProvisioningRequiredError(
                        f"The application role {target.user!r} does not exist and no "
                        "password was supplied to create it."
                    )
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(target.user), sql.Literal(target.password)
                    )
                )
            elif target.password:
                # The wizard owns the application role password; align it so the
                # saved runtime credentials are usable immediately.
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(target.user), sql.Literal(target.password)
                    )
                )
            role = quote_identifier(target.user)
            cur.execute(f"GRANT CONNECT ON DATABASE {quote_identifier(target.database)} TO {role}")
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
            )
            cur.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"
            )
            cur.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
            )
            cur.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
            )
    except ProvisioningRequiredError:
        raise
    except psycopg2.Error as exc:
        raise DatabaseConnectionError(
            "Could not grant routine privileges to the application role: "
            f"{_clean_db_error(exc)}"
        ) from exc
    finally:
        conn.close()


def verify_schema(cur: Any) -> None:
    discrepancies: list[str] = []
    cur.execute(
        """
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name = ANY(%s)
        """,
        (sorted(EFOS_TABLES),),
    )
    found = {row[0] for row in cur.fetchall()}
    for table in sorted(EFOS_TABLES - found):
        discrepancies.append(f"Missing table: public.{table}")

    if not discrepancies:
        cur.execute(
            """
            SELECT typname, array_agg(enumlabel ORDER BY enumsortorder) AS labels
              FROM pg_type t
              JOIN pg_enum e ON e.enumtypid = t.oid
             WHERE typname = ANY(%s)
             GROUP BY typname
            """,
            (sorted(EFOS_ENUMS),),
        )
        enums = {row[0]: list(row[1]) for row in cur.fetchall()}
        for name, labels in EFOS_ENUMS.items():
            if enums.get(name) != labels:
                discrepancies.append(
                    f"Enum public.{name} is {enums.get(name)!r}; expected {labels!r}"
                )

        cur.execute(
            """
            SELECT c.table_name, c.column_name
              FROM information_schema.columns c
             WHERE c.table_schema = 'public'
               AND c.table_name = ANY(%s)
            """,
            (sorted(EFOS_REQUIRED_COLUMNS),),
        )
        columns: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            columns.setdefault(table, set()).add(column)
        for table, required in EFOS_REQUIRED_COLUMNS.items():
            if table not in found:
                continue
            missing = sorted(required - columns.get(table, set()))
            for column in missing:
                discrepancies.append(f"Missing column: public.{table}.{column}")

        cur.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
        )
        triggers = {row[0] for row in cur.fetchall()}
        for trigger in sorted(EFOS_TRIGGERS - triggers):
            discrepancies.append(f"Missing trigger: {trigger}")

        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        indexes = {row[0] for row in cur.fetchall()}
        for index in sorted(EFOS_INDEXES - indexes):
            discrepancies.append(f"Missing index: {index}")

        try:
            cur.execute(
                "SELECT value FROM public.schema_meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
            if not row or row[0] != SCHEMA_VERSION:
                discrepancies.append(
                    "Schema version mismatch: "
                    f"{row[0] if row else 'missing'} (expected {SCHEMA_VERSION})"
                )
        except psycopg2.Error:
            cur.connection.rollback()
            discrepancies.append("public.schema_meta is unreadable")

    if discrepancies:
        raise SchemaValidationError(
            "The target database exists but its EFOS schema is corrupted, partial, "
            "or mismatched. Startup was aborted and no destructive recovery was "
            "attempted. The validation report is below.",
            discrepancies,
        )


class Database:
    """Thread-safe pool with explicit startup validation."""

    def __init__(
        self,
        config: AppConfig,
        *,
        profile: DbProfile | None = None,
        minconn: int = 1,
        maxconn: int = 8,
    ):
        self.config = config
        self.profile = profile or config.profile
        self._pool: pool.ThreadedConnectionPool | None = None
        self._minconn = minconn
        self._maxconn = maxconn
        self._bootstrapped = False

    @property
    def display(self) -> str:
        return self.profile.display

    def connect(self) -> None:
        if self._pool is not None:
            return
        self.bootstrap()
        try:
            self._pool = pool.ThreadedConnectionPool(
                self._minconn, self._maxconn, dsn=self.profile.dsn()
            )
        except psycopg2.Error as exc:
            raise DatabaseConnectionError(
                f"PostgreSQL at {self.profile.display} is unreachable: "
                f"{_clean_db_error(exc)}"
            ) from exc

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        ensure_database(
            self.config,
            admin_profile=self.config.admin_profile,
            target_profile=self.profile,
        )
        self._bootstrapped = True

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self._pool is None:
            self.connect()
        assert self._pool is not None
        try:
            conn = self._pool.getconn()
        except psycopg2.Error as exc:
            raise DatabaseConnectionError(
                f"PostgreSQL at {self.profile.display} is unreachable: "
                f"{_clean_db_error(exc)}"
            ) from exc
        try:
            conn.autocommit = False
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except psycopg2.Error:
                pass
            raise
        finally:
            try:
                self._pool.putconn(conn)
            except psycopg2.Error:
                pass

    def fetch_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]

    def fetch_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount

    def execute_returning(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None

    def schema_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in sorted(EFOS_TABLES):
            row = self.fetch_one(f"SELECT count(*) AS n FROM public.{table}")
            counts[table] = int(row["n"]) if row else 0
        return counts

    def probe(self) -> ConnectionStatus:
        """Live connectivity check. No repository or UI state is stored here."""
        try:
            conn = psycopg2.connect(self.profile.dsn(timeout=4))
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return ConnectionStatus(
                True,
                self.profile.host,
                self.profile.port,
                self.profile.database,
            )
        except psycopg2.Error as exc:
            return ConnectionStatus(
                False,
                self.profile.host,
                self.profile.port,
                self.profile.database,
                _clean_db_error(exc),
            )

    def run_readonly_query(
        self, sql: str, *, max_rows: int = 200, timeout_ms: int = 8000
    ) -> tuple[list[str], list[dict[str, Any]]]:
        validated = validate_readonly_sql(sql)
        limited = _LIMIT_RE.sub("", validated).rstrip().rstrip(";")
        wrapped = (
            f"SELECT * FROM (\n{limited}\n) AS efos_rag_result LIMIT {int(max_rows)}"
        )
        with self.connection() as conn:
            previous = conn.autocommit
            conn.autocommit = False
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    cur.execute(wrapped)
                    rows = [dict(row) for row in cur.fetchall()]
                    columns = [d.name for d in cur.description or []]
                    conn.rollback()
                    return columns, rows
            except psycopg2.Error:
                conn.rollback()
                raise
            finally:
                conn.autocommit = previous

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = None
            self._bootstrapped = False


def validate_readonly_sql(sql: str) -> str:
    if not isinstance(sql, str):
        raise ReadOnlyQueryError("Generated SQL was not text.")
    cleaned = sql.strip()
    if not cleaned:
        raise ReadOnlyQueryError("Generated SQL was empty.")
    if len(cleaned) > 12000:
        raise ReadOnlyQueryError("Generated SQL exceeded the safety size limit.")
    without_block = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    without_line = re.sub(r"--[^\n]*", " ", without_block)
    if "/*" in without_line or "*/" in without_line:
        raise ReadOnlyQueryError("Malformed SQL comments were rejected.")
    normalized = without_line.strip()
    if ";" in normalized.rstrip(";"):
        raise ReadOnlyQueryError("Only a single SQL statement is permitted.")
    normalized = normalized.rstrip(";").strip()
    if not _ALLOWED_START.match(normalized):
        raise ReadOnlyQueryError("Only SELECT or WITH queries are permitted.")
    forbidden = _FORBIDDEN_SQL.search(normalized)
    if forbidden:
        raise ReadOnlyQueryError(
            f"The token “{forbidden.group(0).upper()}” is not permitted in a read-only query."
        )
    lower = normalized.lower()
    if any(
        token in lower
        for token in (
            "information_schema",
            "pg_catalog",
            "pg_",
            "current_setting(",
            "set_config(",
            "dblink",
        )
    ):
        raise ReadOnlyQueryError("System catalogs are outside the EFOS query surface.")
    return normalized
