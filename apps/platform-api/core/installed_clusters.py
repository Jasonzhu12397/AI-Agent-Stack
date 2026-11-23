"""The deployments that describe a cluster that exists right now.

A cluster that was reinstalled keeps its old SUCCEEDED deployment record: the
record was true when it was written, and nothing marks it obsolete afterwards.
Everything that reasons about installed clusters - the backup scheduler, the
backup watchdog - must therefore answer "which record speaks for this cluster"
the same way, or one of them will queue or inspect a cluster that is gone under
that name while the other already moved on.
"""

from __future__ import annotations

from django.db.models import QuerySet

from core.models import ClusterDeployment


def installed_deployments() -> list[ClusterDeployment]:
    """One deployment per installed cluster, newest first.

    A cluster is identified by the tenant, the project, the Target Agent and the
    cluster name: the same name on a different Target Agent is a different
    cluster, the same name on the same Target Agent is one cluster installed
    twice, and the newest succeeded deployment is the one whose target node and
    policy describe it now.
    """

    seen: set[tuple[str, str, str, str]] = set()
    chosen: list[ClusterDeployment] = []
    for deployment in newest_first():
        key = (
            str(deployment.tenant_id),
            str(deployment.project_id),
            str(deployment.target_node_id),
            deployment.cluster_name,
        )
        if key in seen:
            continue
        seen.add(key)
        chosen.append(deployment)
    return chosen


def newest_first() -> QuerySet[ClusterDeployment]:
    """Every succeeded deployment, newest first."""

    return (
        ClusterDeployment.objects.filter(status=ClusterDeployment.Status.SUCCEEDED)
        .select_related("tenant", "project", "target_node")
        .order_by("-created_at", "-id")
    )
