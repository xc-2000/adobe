from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings


@dataclass(slots=True)
class Resources:
    database: AsyncEngine
    redis: Redis

    async def close(self) -> None:
        await self.redis.aclose()
        await self.database.dispose()


def create_resources(settings: Settings) -> Resources:
    database = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return Resources(database=database, redis=redis)
