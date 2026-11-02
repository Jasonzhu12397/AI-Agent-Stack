import logging
import os
import signal
import time
from itertools import count

import psycopg

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("ai-agent-worker")
stopping = False
database_url = os.getenv("DATABASE_URL", "postgresql://agent:agent_dev_password@localhost:5432/agent_stack")
poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))


def request_shutdown(signum: int, frame: object) -> None:
    global stopping
    stopping = True
    logger.info("Shutdown requested (signal=%s)", signum)


def run() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    logger.info("Worker started; polling interval=%ss", poll_seconds)
    for _ in count():
        if stopping:
            break
        try:
            with psycopg.connect(database_url, connect_timeout=5) as connection:
                connection.execute("SELECT 1")
            logger.debug("Database heartbeat succeeded")
        except Exception:
            logger.exception("Worker database heartbeat failed")
        time.sleep(poll_seconds)
    logger.info("Worker stopped")


if __name__ == "__main__":
    run()
