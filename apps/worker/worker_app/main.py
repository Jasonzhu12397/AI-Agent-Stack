import logging
import signal
import time
from itertools import count

from sqlalchemy import text

from app.config import settings
from app.db import engine

logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger("ai-agent-worker")
stopping = False


def request_shutdown(signum: int, frame: object) -> None:
    global stopping
    stopping = True
    logger.info("Shutdown requested (signal=%s)", signum)


def run() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    logger.info("Worker started; polling interval=%ss", settings.worker_poll_seconds)

    for _ in count():
        if stopping:
            break
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.debug("Database heartbeat succeeded")
        except Exception:
            logger.exception("Worker database heartbeat failed")
        time.sleep(settings.worker_poll_seconds)

    logger.info("Worker stopped")


if __name__ == "__main__":
    run()
