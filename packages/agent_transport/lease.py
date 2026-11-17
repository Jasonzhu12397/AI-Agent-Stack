from __future__ import annotations

import logging
import threading
from typing import Any

from .client import AgentControlClient


LOG = logging.getLogger(__name__)


class CommandLeaseGuard:
    def __init__(
        self,
        client: AgentControlClient,
        command: dict[str, Any],
        renew_interval_seconds: float = 25.0,
    ) -> None:
        if renew_interval_seconds <= 0 or renew_interval_seconds >= 90:
            raise ValueError("Lease renewal interval must be between 0 and 90 seconds")
        self.client = client
        self.command = command
        self.renew_interval_seconds = renew_interval_seconds
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name="agent-command-lease-renewer",
            daemon=True,
        )

    def _renew_loop(self) -> None:
        for _ in iter(lambda: self._stop.wait(self.renew_interval_seconds), True):
            try:
                self.client.renew_lease(self.command)
            except Exception as exc:
                LOG.error("Command lease renewal failed: %s", type(exc).__name__)
                self.lost.set()
                return

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.renew_interval_seconds + 1.0)
