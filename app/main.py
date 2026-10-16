"""Process entry point: Streamable HTTP MCP on POST /mcp, GET /health, port from PORT (default 8000)."""

import json
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette

from app import capability_bind
from app.mcp_server import CAPABILITY_PREFIX, mcp

_LOG_FIELDS = ("tool", "attachment_id", "size_bytes", "duration_ms", "error_code", "exception_type")


class JsonFormatter(logging.Formatter):
    """One JSON object per line with only the allow-listed fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for key in _LOG_FIELDS:
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        return json.dumps(entry)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Request URLs carry attachment ids only, but keep transport chatter out of the logs.
    for noisy in (
        "httpx",
        "httpcore",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app() -> Starlette:
    capability_bind.bind(mcp, CAPABILITY_PREFIX)
    return mcp.streamable_http_app()


def main() -> None:
    configure_logging()
    uvicorn.run(
        create_app(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
