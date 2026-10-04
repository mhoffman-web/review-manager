"""One logging setup for every entry point (uvicorn web, worker, CLI).

Under `uvicorn app.web:app` only uvicorn's own loggers have handlers, so app.* INFO
lines (sync results, alerts, schema changes) were silently dropped. LOG_LEVEL sets it."""
from __future__ import annotations

import logging
import os

_done = False


def setup_logging() -> None:
    global _done
    if _done:
        return
    _done = True
    level = getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("app").setLevel(level)
    for noisy in ("httpx", "urllib3", "msal", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
