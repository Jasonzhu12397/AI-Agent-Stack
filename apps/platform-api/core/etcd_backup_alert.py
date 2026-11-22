"""Paging a human when the watchdog finds a cluster without usable backups.

The watchdog already writes an audit event and exits non-zero, which is enough
for CI and for anyone reading the chain, but neither of those wakes anybody up.
This module turns the same report into one POST to a webhook the operator owns,
so alerting lives next to the finding that caused it instead of in a cron line
nobody remembers.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

#: Where to POST. Deliberately not a setting: an alert target is deployment
#: configuration, and the scheduler container already reads its environment.
WEBHOOK_ENV = "ETCD_BACKUP_ALERT_WEBHOOK"
TIMEOUT_ENV = "ETCD_BACKUP_ALERT_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 10.0

#: ``(url, body, timeout) -> HTTP status``. Injectable so a test never opens a
#: socket and so a future transport (mail, chat) can reuse the payload.
Sender = Callable[[str, bytes, float], int]


def webhook_url() -> str:
    """The configured alert target, or an empty string when alerting is off."""

    return (os.environ.get(WEBHOOK_ENV) or "").strip()


def timeout_seconds() -> float:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def alert_text(report: dict[str, Any]) -> str:
    """One line an on-call reader can act on without opening the JSON."""

    problems = report.get("problems") or []
    checked = report.get("checked_at") or ""
    listed = "; ".join(
        "{cluster}: {code}{age}".format(
            cluster=item.get("cluster_name") or item.get("deployment_id"),
            code=item.get("code"),
            age=(
                f" (newest backup {item['newest_age_hours']:.1f}h old)"
                if isinstance(item.get("newest_age_hours"), (int, float))
                else " (no backup ever reported)"
            ),
        )
        for item in problems
    )
    return f"{len(problems)} cluster(s) without a usable recent etcd backup at {checked}: {listed}"


def alert_body(report: dict[str, Any]) -> bytes:
    """The POST body: the report, the readable summary, and the marker."""

    problems = report.get("problems") or []
    payload = {
        "kind": "etcd_backup_problem",
        "checked_at": report.get("checked_at"),
        "problem_count": len(problems),
        "text": alert_text(report),
        "problems": [
            {
                "deployment_id": item.get("deployment_id"),
                "cluster_name": item.get("cluster_name"),
                "code": item.get("code"),
                "newest": item.get("newest"),
                "newest_age_hours": item.get("newest_age_hours"),
                "restore_point": item.get("restore_point"),
                "scheduler_age_hours": item.get("scheduler_age_hours"),
                "last_run_status": item.get("last_run_status"),
            }
            for item in problems
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _post(url: str, body: bytes, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(getattr(response, "status", 0) or 0)


def deliver(
    report: dict[str, Any],
    *,
    sender: Sender | None = None,
    url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST the problems in ``report`` to the webhook, and say what happened.

    Returns a small, secret-free dictionary: the target may carry a token in its
    path, so it is never echoed back into a report or an audit event.
    """

    target = (url if url is not None else webhook_url()).strip()
    if not target:
        return {"configured": False, "delivered": False, "reason": "no_webhook"}
    if not (report.get("problems") or []):
        # A watchdog that pages on "all good" trains its reader to ignore it.
        return {"configured": True, "delivered": False, "reason": "nothing_to_report"}
    body = alert_body(report)
    effective_timeout = timeout if timeout is not None else timeout_seconds()
    try:
        status = (sender or _post)(target, body, effective_timeout)
    except Exception as exc:  # noqa: BLE001 - a failed page must not fail the probe
        return {
            "configured": True,
            "delivered": False,
            "reason": "send_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    if not 200 <= status < 300:
        return {
            "configured": True,
            "delivered": False,
            "reason": "http_status",
            "status": status,
        }
    return {"configured": True, "delivered": True, "status": status}
