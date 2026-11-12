"""Seed a representative EFOS workspace for demos and visual verification.

Run against a disposable database:
    $env:EFOS_DB_NAME="efos_demo"; python scripts/seed_demo.py --force
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from efos_app.config import AppConfig
from efos_app.models import (
    NodeKind,
    NodeRef,
    ProjectState,
    RecurrenceFrequency,
    TaskState,
)
from efos_app.service import EFOSService


def _dt(day: datetime, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day.date(), time(hour, minute))


def seed(service: EFOSService) -> None:
    today = datetime.now()
    design = service.create_condition("Design approved", evaluation_state=True)
    copy = service.create_condition("Copy approved", evaluation_state=True)
    api = service.create_condition("Analytics key provisioned", evaluation_state=False)
    vendor = service.create_condition("Vendor SLA confirmed", evaluation_state=None)

    draft = service.create_task(
        "Draft launch brief", start=_dt(today, 9, 0), end=_dt(today, 10, 30)
    )
    review = service.create_task(
        "Review release copy", start=_dt(today, 10, 0), end=_dt(today, 11, 0)
    )
    provision = service.create_task(
        "Provision analytics keys", start=_dt(today, 11, 15), end=_dt(today, 12, 15)
    )
    publish = service.create_task("Publish release notes")
    inventory = service.create_task(
        "Audit component inventory",
        start=_dt(today, 13, 0),
        end=_dt(today, 14, 0),
        is_recurring=True,
    )
    architecture = service.create_task(
        "Architecture review block",
        start=_dt(today, 14, 30),
        end=_dt(today, 16, 0),
    )
    archive = service.create_task("Archive old prototypes")
    legacy = service.create_task("Migrate legacy exports")

    for task_id, condition_id in (
        (draft, design),
        (review, copy),
        (provision, api),
        (inventory, vendor),
        (architecture, design),
    ):
        service.connect(
            NodeRef(NodeKind.CONDITION, condition_id), NodeRef(NodeKind.TASK, task_id)
        )
    service.connect(NodeRef(NodeKind.TASK, draft), NodeRef(NodeKind.TASK, publish))
    service.connect(NodeRef(NodeKind.TASK, provision), NodeRef(NodeKind.TASK, publish))
    service.connect(NodeRef(NodeKind.TASK, review), NodeRef(NodeKind.TASK, publish))

    service.complete_task(archive)
    service.defer_task(legacy)

    platform = service.create_project("Q4 platform launch")
    brand_project = service.create_project("Brand system refresh")
    legacy_project = service.create_project("Legacy migration")

    # Attach prerequisites before preconditions so an intermediate, partially
    # defined project cannot auto-resolve while the dependency set is assembled.
    service.connect(NodeRef(NodeKind.TASK, publish), NodeRef(NodeKind.PROJECT, platform))
    service.connect(
        NodeRef(NodeKind.TASK, review), NodeRef(NodeKind.PROJECT, brand_project)
    )
    for condition_id in (design, copy):
        service.connect(
            NodeRef(NodeKind.CONDITION, condition_id),
            NodeRef(NodeKind.PROJECT, platform),
        )
    service.connect(
        NodeRef(NodeKind.CONDITION, design),
        NodeRef(NodeKind.PROJECT, brand_project),
    )
    service.shelve_project(legacy_project)

    service.save_rule(
        inventory,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=1,
        by_day="SU",
        reset_hour=18,
        reset_minute=0,
    )
    service.load_view()


def clear(service: EFOSService) -> None:
    snapshot = service.repo.snapshot()
    for task in list(snapshot.tasks.values()):
        service.repo.delete_task(task.id)
    for project in list(snapshot.projects.values()):
        service.repo.delete_project(project.id)
    for condition in list(snapshot.conditions.values()):
        service.repo.delete_condition(condition.id)
    service.load_view()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Clear existing records first.")
    args = parser.parse_args()
    config = AppConfig.load()
    service = EFOSService(config)
    service.start()
    try:
        view = service.load_view()
        existing = (
            len(view.snapshot.tasks)
            + len(view.snapshot.projects)
            + len(view.snapshot.conditions)
        )
        if existing and not args.force:
            print(
                f"Database {config.db_display} already contains {existing} records. "
                "Re-run with --force to clear it first."
            )
            return 1
        if existing:
            clear(service)
        seed(service)
        view = service.load_view()
        print(
            f"Seeded {config.db_display}: {len(view.snapshot.tasks)} tasks, "
            f"{len(view.snapshot.projects)} projects, "
            f"{len(view.snapshot.conditions)} conditions, "
            f"{view.snapshot.count_edges()} dependencies."
        )
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
