import asyncio

import httpx
from redis.asyncio import Redis

from app.config import Settings
from app.services.database import database_connection


def _ping_mongo(settings: Settings) -> bool:
    with database_connection(settings) as database:
        return bool(database.command("ping")["ok"])


async def _ping_redis(settings: Settings) -> bool:
    client = Redis.from_url(
        settings.celery_broker_url.get_secret_value(),
        socket_connect_timeout=settings.dependency_timeout_seconds,
        socket_timeout=settings.dependency_timeout_seconds,
    )
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def _ping_qdrant(settings: Settings) -> bool:
    headers = {}
    if settings.vector_store_api_key:
        headers["api-key"] = settings.vector_store_api_key.get_secret_value()
    async with httpx.AsyncClient(timeout=settings.dependency_timeout_seconds) as client:
        response = await client.get(
            settings.vector_store_url.rstrip("/") + "/readyz", headers=headers
        )
        return response.status_code == 200


async def check_dependencies(settings: Settings) -> dict[str, str]:
    # The payload deliberately excludes exception messages, addresses and credentials.
    results = await asyncio.gather(
        asyncio.to_thread(_ping_mongo, settings),
        _ping_redis(settings),
        _ping_qdrant(settings),
        return_exceptions=True,
    )
    return {
        name: "ok" if result is True else "unavailable"
        for name, result in zip(("mongodb", "redis", "qdrant"), results, strict=True)
    }
