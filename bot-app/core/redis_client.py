import redis

from .config import settings

REDIS_URL = settings.redis_url

redis_cliente = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)