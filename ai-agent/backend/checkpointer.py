import logging
import os

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


def build_checkpointer():
    """
    Returns a LangGraph checkpointer. If DATABASE_URL is set, uses a Postgres-backed PostgresSaver
    so paused approval sessions and conversation state survive pod restarts and are shared across
    replicas. Falls back to the in-process MemorySaver for local dev without Postgres.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set - using in-process MemorySaver (state lost on restart, not shared across replicas).")
        return MemorySaver()

    from langgraph.checkpoint.postgres import PostgresSaver

    saver_cm = PostgresSaver.from_conn_string(database_url)
    saver = saver_cm.__enter__()
    saver.setup()
    logger.info("Using PostgresSaver for checkpoint storage.")
    return saver
