"""Write the reasoned plan back to the platform.

The agent decides which machine is a master and which is a worker; the platform
must not keep a second, stale opinion. This module records the decision where
the platform already stores machine state (``bmh_name``, ``lifecycle_state``)
so the planning result stays authoritative in the Rust API database.

Nothing here is invented: the write-back only mirrors what
:mod:`apps.eph_agent.fleet_planning` already decided.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.eph_agent.fleet_planning import ROLE_CONTROL_PLANE, FleetPlan  # noqa: E402
from tools.platform_state import (  # noqa: E402
    machine_context,
    record_audit_event,
    update_machine,
)

LIFECYCLE_BY_ROLE = {
    ROLE_CONTROL_PLANE: "planned_control_plane",
    "worker": "planned_worker",
}


def write_back_plan(
    plan: FleetPlan,
    *,
    database_url: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Mirror the plan into the Rust-owned machine table."""

    updated = 0
    audited = 0
    for assignment in plan.assignments:
        machine_id = str(assignment.machine.machine_id)
        lifecycle = LIFECYCLE_BY_ROLE.get(assignment.role, "planned")
        if not update_machine(
            machine_id,
            bmh_name=assignment.machine.name,
            lifecycle_state=lifecycle,
            explicit=database_url,
        ):
            continue
        updated += 1
        context = machine_context(machine_id, explicit=database_url)
        if context is None:
            continue
        record_audit_event(
            tenant_id=context["tenant_id"],
            project_id=context["project_id"],
            event_type="fleet.machine.role_planned",
            resource_type="agent_machine",
            resource_id=machine_id,
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
            explicit=database_url,
        )
        audited += 1
    return {"updated": updated, "audited": audited}
