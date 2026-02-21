import logging
import os
import signal
import sys
import threading

from dotenv import load_dotenv

# Load env file — try .env first, fall back to env.local
load_dotenv()
load_dotenv("env.local")

from src.llm_client import LLMClient
from src.mattermost_client import MattermostClient
from src.memory import Memory
from src.agent import Agent
from src.listener import Listener
from src.scheduler import Scheduler

# ── Logging ─────────────────────────────────────────────────────────

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    logger.info("=== AI-архитектор метрик: запуск ===")

    # ── Init components ─────────────────────────────────────────
    try:
        memory = Memory()
        llm = LLMClient()
        mm = MattermostClient()
        agent = Agent(llm, memory, mm)
        listener = Listener(agent, mm)
        scheduler = Scheduler(agent, memory, mm, llm)
    except Exception as e:
        logger.fatal("Failed to initialize: %s", e, exc_info=True)
        sys.exit(1)

    # ── Graceful shutdown ───────────────────────────────────────
    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # ── Start scheduler in background thread ────────────────────
    scheduler_thread = threading.Thread(
        target=scheduler.start,
        name="scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    logger.info("Scheduler thread started")

    # ── Start listener in main thread (blocks) ──────────────────
    logger.info("Starting WebSocket listener (main thread)...")
    try:
        listener.start()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    finally:
        logger.info("=== AI-архитектор метрик: остановлен ===")


if __name__ == "__main__":
    main()
