"""
Durable conversation history.

This used to be a module-level in-process list (`ConversationMemory` in main.py). With 2 backend
replicas that meant each pod remembered a different half of the conversation and whichever pod
handled your next request decided what "we" had talked about - and everything was lost on restart.
So "what was the last troubleshooting we did" could legitimately have no answer on the pod that
served it. History now lives in the same Postgres the LangGraph checkpointer already uses, so both
replicas read the same record and it survives restarts.

Falls back to an in-process list when DATABASE_URL is unset (local dev), matching checkpointer.py.
"""
import logging
import os
from typing import List

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger(__name__)

# Keep the prompt bounded - only the most recent turns are replayed into the model.
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
        # Labelled so the model can't mistake a past turn for the current request or for live
        # cluster state - these are a record of the conversation, nothing more.
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
    """Thin wrapper so main.py doesn't care which backing store is in use."""

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
            # Never let history storage take the API down - it is a convenience, not core to
            # diagnosing or fixing anything.
            logger.error(f"Falling back to in-process conversation history: {e}")
            self._impl = _InMemoryStore()

    def load_history(self) -> List:
        try:
            history = self._impl.load()
        except Exception as e:
            logger.error(f"Could not load conversation history: {e}")
            return []
        # Bedrock's Converse API rejects a conversation that starts with an assistant turn
        # ("A conversation must start with a user message"), which took the whole API down with a
        # 500. The window can easily begin mid-pair: the MAX_TURNS limit cuts at an arbitrary row,
        # and deleting rows (e.g. purging contaminated ones) leaves an assistant turn first. Drop
        # any leading assistant messages so the replayed history always opens with a user turn.
        while history and not isinstance(history[0], HumanMessage):
            history.pop(0)
        return history

    def record(self, user_text: str, assistant_text: str) -> None:
        try:
            # Cap what's stored: callers pass the full report + proposal + execution + verification
            # text, and every one of those turns is replayed into the next prompt. Left uncapped a
            # few long troubleshooting turns dominate the context window and the model starts
            # echoing old transcripts back at the user.
            # Keep stored turns SHORT. Storing the full report + proposal + YAML + verification
            # (2000 chars each) actively broke diagnosis once history became durable: 20 such turns
            # dominated the prompt, and manifests that had merely been *proposed* read as current
            # cluster state - the model announced a Role was "already updated" when it had never
            # been applied, and echoed pre-permission-change refusals long after the policy changed.
            # History exists to answer "what did we do", which a couple of lines covers; live state
            # must always come from the read tools.
            self._impl.save((user_text or "")[:500], (assistant_text or "")[:500])
        except Exception as e:
            logger.error(f"Could not save conversation history: {e}")
