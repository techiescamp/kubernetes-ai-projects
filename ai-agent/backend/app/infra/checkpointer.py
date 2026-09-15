import logging
import os

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


def build_checkpointer():
    """
    Returns a LangGraph checkpointer. If DATABASE_URL is set, uses a Postgres-backed PostgresSaver
    built on a connection pool - a single raw psycopg connection (the pattern in LangGraph's own
    quickstart docs) goes stale after any idle timeout or network blip and PostgresSaver does not
    reconnect it, so every subsequent request fails with `psycopg.OperationalError: the connection
    is closed` (observed in a real deployment - see SPEC.md). A pool hands out a fresh/healthy
    connection per operation instead. Falls back to the in-process MemorySaver for local dev
    without Postgres.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set - using in-process MemorySaver (state lost on restart, not shared across replicas).")
        return MemorySaver()

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )
    saver = PostgresSaver(pool)
    saver.setup()
    logger.info("Using PostgresSaver (connection pool) for checkpoint storage.")
    return saver
