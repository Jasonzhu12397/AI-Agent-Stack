"""Reconciliation for Cloud Control workflow runs.

Runs carry an absolute deadline. The reconciler fails a running run once its
deadline has passed and cancels every task that is still active, so a stalled
approval or a lost Agent cannot keep a run alive forever.
"""

from __future__ import annotations

from typing import Any

ACTIVE_TASK_STATUSES = ("blocked", "queued", "leased", "awaiting_approval")


def reconcile_workflow_runs(connection: Any, limit: int = 100) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        return {"status": "invalid_limit"}
    with connection.transaction():
        rows = connection.execute(
            """
            SELECT run_id FROM agent_control_workflow_runs
            WHERE status = 'running' AND deadline_at IS NOT NULL
              AND deadline_at <= NOW()
            ORDER BY deadline_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        expired = 0
        for row in rows:
            updated = connection.execute(
                """
                UPDATE agent_control_workflow_runs
                SET status = 'failed',
                    output = jsonb_build_object(
                        'error', 'WORKFLOW_DEADLINE_EXCEEDED',
                        'deadline_at', deadline_at
                    ),
                    finished_at = NOW(), updated_at = NOW()
                WHERE run_id = %s AND status = 'running'
                RETURNING run_id
                """,
                (row["run_id"],),
            ).fetchone()
            if updated is None:
                continue
            connection.execute(
                """
                UPDATE agent_control_tasks
                SET status = 'cancelled', lease_token_hash = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE workflow_run_id = %s
                  AND status IN ('blocked', 'queued', 'leased', 'awaiting_approval')
                """,
                (row["run_id"],),
            )
            expired += 1
    return {"status": "ok", "expired": expired}
