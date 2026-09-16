import logging
import os
from typing import List

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger(__name__)

MAX_TURNS = int(os.getenv("CONVERSATION_HISTORY_TURNS", "20"))

class _InMemoryStore:
    def __init__(self):
        self._messages = []

    def load(self) -> List:
        return list(self._messages[-(MAX_TURNS * 2):])

    def save(self, user_text: str, assistant_text: str) -> None:
        self._messages.append(HumanMessage(content=user_text))
        self._messages.append(AIMessage(content=assistant_text))

class _PostgresStore:
    def __init__(self, pool):
        self._pool = pool
        with self._pool.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id          BIGSERIAL PRIMARY KEY,
                    role        TEXT        NOT NULL,
                    content     TEXT        NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

    def load(self) -> List:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT role, content FROM (SELECT role, content, id FROM conversation_history "
                "ORDER BY id DESC LIMIT %s) recent ORDER BY id ASC",
                (MAX_TURNS * 2,),
            ).fetchall()
        return [
            HumanMessage(content=f"[earlier in this conversation] {content}") if role == "user"
            else AIMessage(content=f"[earlier reply - historical, may be out of date] {content}")
            for role, content in rows
        ]

    def save(self, user_text: str, assistant_text: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO conversation_history (role, content) VALUES (%s, %s), (%s, %s)",
                ("user", user_text, "assistant", assistant_text),
            )

class ConversationStore:

    def __init__(self):
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.warning(
                "DATABASE_URL not set - conversation history is in-process only "
                "(lost on restart, not shared across replicas)."
            )
            self._impl = _InMemoryStore()
            return
        try:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(conninfo=database_url, min_size=1, max_size=4,
                                  kwargs={"autocommit": True}, open=True)
            self._impl = _PostgresStore(pool)
            logger.info("Conversation history backed by Postgres (shared across replicas).")
        except Exception as e:
            logger.error(f"Falling back to in-process conversation history: {e}")
            self._impl = _InMemoryStore()

    def load_history(self) -> List:
        try:
            history = self._impl.load()
        except Exception as e:
            logger.error(f"Could not load conversation history: {e}")
            return []
        while history and not isinstance(history[0], HumanMessage):
            history.pop(0)
        return history

    def record(self, user_text: str, assistant_text: str) -> None:
        try:
            self._impl.save((user_text or "")[:500], (assistant_text or "")[:500])
        except Exception as e:
            logger.error(f"Could not save conversation history: {e}")
