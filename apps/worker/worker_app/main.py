import logging
import os
import signal
import socket
import time
from itertools import count

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_api.settings")
django.setup()

from worker_app.tasks import process_once

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("ai-agent-worker")
stopping = False
poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
worker_id = os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")


def request_shutdown(signum: int, frame: object) -> None:
    global stopping
    stopping = True
    logger.info("Shutdown requested (signal=%s)", signum)


def run() -> None:
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("WORKER_POLL_SECONDS must be greater than 0 and no more than 60")
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    logger.info("Worker started (worker_id=%s, poll_seconds=%s)", worker_id, poll_seconds)
    for _ in count():
        if stopping:
            break
        try:
            worked = process_once(worker_id)
            if not worked:
                time.sleep(poll_seconds)
        except Exception:
            logger.exception("Worker loop failed")
            time.sleep(poll_seconds)
    logger.info("Worker stopped")


if __name__ == "__main__":
    run()
