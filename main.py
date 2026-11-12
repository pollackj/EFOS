"""EFOS desktop entry point.

Run with ``python main.py`` in development, or launch the packaged ``EFOS.exe``.
First launch runs the guided database setup when provisioning is required; a
partial or mismatched schema aborts startup with a detailed report.
"""
from __future__ import annotations

import ctypes
import json
import logging
import multiprocessing
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from efos_app.config import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    ORG_NAME,
    AppConfig,
    ConfigurationError,
    app_data_dir,
    load_ui_state,
)
from efos_app.db import Database
from efos_app.errors import (
    DatabaseConnectionError,
    EFOSError,
    ProvisioningRequiredError,
    SchemaValidationError,
)
from efos_app.service import EFOSService
from efos_app.ui.dialogs import (
    DatabaseSetupDialog,
    SchemaMismatchDialog,
)
from efos_app.ui.graph import LayoutStore
from efos_app.ui.icons import brand_pixmap
from efos_app.ui.main_window import MainWindow
from efos_app.ui.theme import DEFAULT_THEME, apply_theme

log = logging.getLogger("efos")


def _configure_logging(data_dir: Path) -> None:
    level = logging.DEBUG if os.environ.get("EFOS_DEBUG") else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        handler = RotatingFileHandler(
            data_dir / "efos.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError:
        log.warning("File logging is unavailable; using console logging only.")


def _install_exception_hook() -> None:
    def handle(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception_only(exc_type, exc_value)).strip()
        box = QMessageBox()
        box.setWindowTitle("EFOS encountered an error")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("An unexpected error interrupted the operation.")
        box.setInformativeText(detail[:900])
        box.setDetailedText(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
        box.exec()

    sys.excepthook = handle


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EFOS.ExecutionConsole.2"
        )
    except Exception:
        pass


def _error_dialog(title: str, message: str) -> None:
    box = QMessageBox()
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setText(title)
    box.setInformativeText(message)
    box.exec()


def _write_self_test(payload: dict) -> Path:
    target = Path(os.environ.get("EFOS_SELFTEST_OUT", Path.cwd() / "efos_selftest.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return target


def _run_self_test() -> int:
    payload: dict = {
        "app_version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }
    try:
        config = AppConfig.load()
        payload["env_path"] = str(config.env_path) if config.env_path else None
        payload["db"] = config.db_display
        payload["deepseek_configured"] = config.deepseek_enabled
        payload["google_oauth_configured"] = config.google_enabled
        data_dir = app_data_dir()
        payload["data_dir"] = str(data_dir)
        _configure_logging(data_dir)
        from efos_app.config import resource_path

        schema_path = resource_path("efos_app/sql/schema.sql")
        payload["schema_resource"] = str(schema_path)
        payload["schema_resource_exists"] = schema_path.is_file()
        service = EFOSService(config)
        try:
            service.start()
            view = service.load_view(reconcile=False)
            payload["ok"] = True
            payload["counts"] = {
                "tasks": len(view.snapshot.tasks),
                "projects": len(view.snapshot.projects),
                "conditions": len(view.snapshot.conditions),
                "edges": view.snapshot.count_edges(),
            }
            payload["schema_counts"] = service.repo.schema_counts()
            payload["connection"] = service.connection_status().metadata
        finally:
            service.close()
        target = _write_self_test(payload)
        log.info("Self-test passed; report at %s", target)
        return 0
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        _write_self_test(payload)
        return 4


def _setup_dialog(config: AppConfig | None) -> bool:
    if config is not None and config.db_user != "postgres":
        app_user = config.db_user
    else:
        app_user = "efos_app"
    dialog = DatabaseSetupDialog(
        host=config.db_host if config else "localhost",
        port=config.db_port if config else 5432,
        database=config.db_name if config else "efos",
        app_user=app_user,
    )
    return bool(dialog.exec()) and dialog.result_values is not None


def _load_config_with_setup() -> AppConfig | None:
    try:
        return AppConfig.load()
    except ConfigurationError as exc:
        log.warning("Configuration unresolved: %s", exc)
        if not _setup_dialog(None):
            return None
        try:
            return AppConfig.load()
        except ConfigurationError as second:
            _error_dialog("EFOS configuration error", str(second))
            return None


def _start_service(config: AppConfig, parent=None) -> EFOSService | None:
    """Start the service, running guided setup on provisioning failure."""
    for attempt in range(2):
        service = EFOSService(config)
        try:
            service.start()
            return service
        except SchemaValidationError as exc:
            log.error("Schema validation failed: %s", exc)
            dialog = SchemaMismatchDialog(str(exc), exc.discrepancies, parent)
            dialog.exec()
            return None
        except ProvisioningRequiredError as exc:
            log.warning("Provisioning required: %s", exc)
            if not _setup_dialog(config):
                return None
            try:
                config = AppConfig.load()
            except ConfigurationError as config_error:
                _error_dialog("EFOS configuration error", str(config_error))
                return None
            continue
        except DatabaseConnectionError as exc:
            log.exception("Database startup failed")
            if attempt == 0:
                box = QMessageBox(parent)
                box.setWindowTitle("PostgreSQL connection failed")
                box.setIcon(QMessageBox.Icon.Critical)
                box.setText(f"EFOS could not reach {config.db_display}.")
                box.setInformativeText(str(exc)[:700])
                box.setStandardButtons(
                    QMessageBox.StandardButton.Retry
                    | QMessageBox.StandardButton.Close
                )
                if box.exec() == QMessageBox.StandardButton.Retry:
                    continue
            _error_dialog("PostgreSQL connection failed", str(exc))
            return None
    return None


def main() -> int:
    multiprocessing.freeze_support()
    _set_windows_app_id()
    if "--self-test" in sys.argv:
        return _run_self_test()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(QIcon(brand_pixmap(256)))
    saved_theme = str(load_ui_state().get("theme", DEFAULT_THEME))
    apply_theme(app, saved_theme)
    _install_exception_hook()

    config = _load_config_with_setup()
    if config is None:
        return 2
    try:
        data_dir = app_data_dir()
    except ConfigurationError as exc:
        _error_dialog("EFOS data directory error", str(exc))
        return 2
    _configure_logging(data_dir)
    log.info("Starting EFOS %s against %s", APP_VERSION, config.db_display)

    service = _start_service(config)
    if service is None:
        return 3

    layout_store = LayoutStore(data_dir / "workspace_layout.json", config.db_display)
    window = MainWindow(service, config, layout_store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
