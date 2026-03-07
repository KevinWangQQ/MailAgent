"""
Redis 事件消费者

BLPOP 用户专属队列 mailagent:{database_id}:events，
将事件分发给对应的 handler。
"""

import asyncio
import json
from typing import Callable, Awaitable, Dict, Optional

import redis.asyncio as redis
from loguru import logger


class RedisConsumer:
    """Redis 队列消费者"""

    def __init__(
        self,
        redis_url: str,
        redis_db: int,
        queue_key: str,
        blpop_timeout: int = 10,
    ):
        self.redis_url = redis_url
        self.redis_db = redis_db
        self.queue_key = queue_key
        self.blpop_timeout = blpop_timeout
        self._pool: Optional[redis.Redis] = None
        self._running = False
        self._handlers: Dict[str, Callable[[Dict], Awaitable[None]]] = {}
        self._stats = {"received": 0, "processed": 0, "errors": 0}

    def on(self, event_type: str, handler: Callable[[Dict], Awaitable[None]]):
        """注册事件处理器

        Args:
            event_type: 事件类型 (flag_changed, ai_reviewed, page_updated)
            handler: async handler(event_data)
        """
        self._handlers[event_type] = handler

    async def start(self, shutdown_event: asyncio.Event = None):
        """启动消费循环"""
        self._pool = redis.from_url(
            f"{self.redis_url}/{self.redis_db}",
            decode_responses=True
        )
        self._running = True
        logger.info(f"Redis consumer started: queue={self.queue_key}")

        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break

            try:
                result = await self._pool.blpop(self.queue_key, timeout=self.blpop_timeout)
                if result is None:
                    continue

                _, raw_message = result
                self._stats["received"] += 1

                event = json.loads(raw_message)
                event_type = event.get("type", "page_updated")

                # Try specific handler, then fallback to page_updated
                handler = self._handlers.get(event_type) or self._handlers.get("page_updated")
                if handler:
                    try:
                        await handler(event)
                        self._stats["processed"] += 1
                    except Exception as e:
                        logger.error(f"Event handler error for {event_type}: {e}")
                        self._stats["errors"] += 1
                else:
                    logger.warning(f"No handler for event type: {event_type}")

            except asyncio.CancelledError:
                break
            except redis.ConnectionError as e:
                logger.error(f"Redis connection lost: {e}, reconnecting in 5s...")
                self._stats["errors"] += 1
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Consumer error: {e}")
                self._stats["errors"] += 1
                await asyncio.sleep(1)

        logger.info("Redis consumer stopped")

    async def stop(self):
        self._running = False
        if self._pool:
            await self._pool.close()
            self._pool = None

    def get_stats(self) -> Dict:
        return {**self._stats, "queue": self.queue_key, "running": self._running}

    async def publish_result(self, event_id: str, result: Dict):
        """将事件执行结果写入 Redis，供 webhook-server 轮询"""
        if not self._pool:
            return
        key = f"mailagent:results:{event_id}"
        await self._pool.set(key, json.dumps(result, ensure_ascii=False), ex=3600)
