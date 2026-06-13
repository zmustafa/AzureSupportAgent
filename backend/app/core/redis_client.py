"""Redis client for session/context cache and rate limiting."""
from __future__ import annotations

import redis.asyncio as redis

from app.core.config import get_settings

settings = get_settings()

redis_client: redis.Redis = redis.from_url(settings.redis_url, decode_responses=True)
