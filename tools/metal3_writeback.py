"""Write the reasoned plan back to the platform.

The agent decides which machine is a master and which is a worker; the platform
must not keep a second, stale opinion. This module records the decision where
the platform already stores machine state (``bmh_name``, ``lifecycle_state``)
and writes an audit event per machine, so the reasoning that produced the plan
is reviewable later without re-reading plan.json.

Nothing here is invented: the write-back only mirrors what
:mod:`apps.eph_agent.fleet_planning` already decided.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API_DIR = ROOT / "apps" / "platform-api"
for _path in (ROOT, PLATFORM_API_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from apps.eph_agent.fleet_planning import ROLE_CONTROL_PLANE, FleetPlan  # noqa: E402

LIFECYCLE_BY_ROLE = {
    ROLE_CONTROL_PLANE: "planned_control_plane",
    "worker": "planned_worker",
}


def write_back_plan(
    plan: FleetPlan,
    *,
    settings_module: str = "platform_api.settings",
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Mirror the plan into the platform and audit every assignment."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    import django

    django.setup()
    from core.audit import record_audit_event
    from core.models import AgentMachine

    updated = 0
    audited = 0
    for assignment in plan.assignments:
        machine = AgentMachine.objects.filter(pk=assignment.machine.machine_id).first()
        if machine is None:
            continue
        changed: list[str] = []
        if machine.bmh_name != assignment.machine.name:
            machine.bmh_name = assignment.machine.name
            changed.append("bmh_name")
        lifecycle = LIFECYCLE_BY_ROLE.get(assignment.role, "planned")
        if machine.lifecycle_state != lifecycle:
            machine.lifecycle_state = lifecycle
            changed.append("lifecycle_state")
        if changed:
            machine.save(update_fields=[*changed, "updated_at"])
            updated += 1
        record_audit_event(
            None,
            machine.tenant,
            "fleet.machine.role_planned",
            "agent_machine",
            machine.id,
            project=machine.project,
            metadata={
                "cluster_name": plan.cluster_name,
                "namespace": plan.namespace,
                "role": assignment.role,
                "pool": assignment.pool_name,
                "score": assignment.score,
                "reason": assignment.reason,
                "control_plane_count": plan.control_plane_count,
                **(extra_metadata or {}),
            },
        )
        audited += 1
    return {"updated": updated, "audited": audited}
