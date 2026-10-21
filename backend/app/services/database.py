"""Shared MongoDB connection scope for synchronous services and worker commands."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database

from app.config import Settings


@contextmanager
def database_connection(settings: Settings) -> Iterator[Database[dict[str, Any]]]:
    """Select the configured database and close the client even when work fails.

    MongoClient connects lazily: callers perform a command to verify availability.
    Call synchronous database work from a thread when using an async API handler.
    """
    timeout_ms = int(settings.dependency_timeout_seconds * 1000)
    with MongoClient(
        settings.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=timeout_ms,
        tz_aware=True,
    ) as client:
        yield client[settings.mongodb_database]
