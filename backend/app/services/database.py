"""Shared MongoDB connection scope for synchronous services and worker commands."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pymongo import MongoClient
from pymongo.client_session import ClientSession
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


@contextmanager
def transactional_connection(
    settings: Settings,
) -> Iterator[tuple[Database[dict[str, Any]], ClientSession]]:
    """Yield (database, session) inside one MongoDB transaction.

    Used where dev/03 requires business writes and their dispatch/outbox rows
    to commit atomically (plan confirmation, execution resume). Transactions
    need a replica set: the dev Compose runs single-node rs0 (see dev/12);
    against a standalone mongod this raises OperationFailure, which callers
    surface as 503 rather than silently dropping atomicity.
    """
    timeout_ms = int(settings.dependency_timeout_seconds * 1000)
    with MongoClient(
        settings.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=timeout_ms * 4,
        tz_aware=True,
    ) as client:
        session = client.start_session()
        try:
            with session.start_transaction():
                yield client[settings.mongodb_database], session
        finally:
            session.end_session()
