import logging
import os

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

def build_checkpointer():
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
