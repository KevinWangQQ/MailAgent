"""
统计数据上报模块

定期将本地 MailAgent 的运行统计 POST 到远程 webhook-server，
供看板 Dashboard 展示。
"""

import sys
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from loguru import logger


class StatsReporter:
    """定期上报运行统计到远程看板"""

    def __init__(
        self,
        report_url: str,
        database_id: str,
        token: str = "",
        interval: int = 60,
    ):
        self.report_url = report_url
        self.database_id = database_id.replace("-", "")
        self.token = token
        self.interval = interval
        self._start_time = time.time()
        self._session: Optional[aiohttp.ClientSession] = None
        self._collectors: List[Callable[[], Dict[str, Any]]] = []
        self._alerts: List[Dict[str, Any]] = []
        self._max_alerts = 50

    def add_collector(self, name: str, fn: Callable[[], Dict[str, Any]]):
        """注册一个统计数据收集器"""
        self._collectors.append((name, fn))

    def add_alert(self, level: str, source: str, message: str):
        """添加一条告警记录"""
        self._alerts.append({
            "ts": int(time.time()),
            "level": level,
            "source": source,
            "message": message[:500],
        })
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]

    def _collect(self) -> Dict[str, Any]:
        """收集所有统计数据"""
        payload = {
            "database_id": self.database_id,
            "timestamp": int(time.time()),
            "service": {
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "uptime_seconds": int(time.time() - self._start_time),
                "start_time": self._start_time,
            },
        }
        for name, fn in self._collectors:
            try:
                payload[name] = fn()
            except Exception as e:
                logger.debug(f"Stats collector '{name}' error: {e}")
                payload[name] = {"error": str(e)}

        if self._alerts:
            payload["alerts"] = list(self._alerts)
            self._alerts.clear()

        return payload

    async def report_once(self):
        """执行一次上报"""
        if not self.report_url:
            return

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

        payload = self._collect()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Webhook-Token"] = self.token

        try:
            async with self._session.post(
                self.report_url, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.debug(f"Stats report failed ({resp.status}): {text[:100]}")
        except Exception as e:
            logger.debug(f"Stats report error: {e}")

    async def close(self):
        """关闭 HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()
