import logging
import os
import sys


def configure_logging() -> None:
    """
    Configures root logging to emit leveled, single-line JSON-ish records to stdout, controlled
    by LOG_LEVEL. Called once at process startup (main.py) before any other module logs.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
