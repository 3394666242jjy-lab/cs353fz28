"""Shared MongoDB access for business modules (M1: pymongo, sync driver).

Endpoints run these calls via ``asyncio.to_thread`` so the event loop is not
blocked; the readiness probe follows the same pattern.
"""

from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

from app.config import Settings, get_settings


@lru_cache
def get_client() -> MongoClient:
    settings = get_settings()
    return MongoClient(settings.mongodb_uri.get_secret_value())


@lru_cache
def get_database() -> Database:
    settings: Settings = get_settings()
    return get_client()[settings.mongodb_database]
