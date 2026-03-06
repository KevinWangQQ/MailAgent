"""
飞书自定义机器人通知模块

通过 webhook 发送交互式卡片消息，用于通知重要邮件。
支持签名验证（可选）。

Usage:
    notifier = FeishuNotifier(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/xxx")
    await notifier.notify_important_email(page_info)
"""

import time
import hmac
import hashlib
import base64
from typing import Dict, Optional

import aiohttp
from loguru import logger


class FeishuNotifier:
    """飞书自定义机器人通知器"""

    def __init__(self, webhook_url: str, secret: str = ""):
        self.webhook_url = webhook_url
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _gen_sign(self, timestamp: int) -> str:
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    async def notify_important_email(self, page_info: Dict) -> bool:
        """发送重要邮件通知卡片

        Args:
            page_info: 包含以下字段:
                - page_id: Notion 页面 ID
                - subject: 邮件主题
                - from_name: 发件人名称
                - from_email: 发件人邮箱
                - date: 日期字符串
                - ai_action: AI 动作
                - ai_priority: AI 优先级（如 Important, Critical）

        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("Feishu webhook URL not configured, skipping notification")
            return False

        subject = page_info.get("subject", "(No Subject)")
        from_name = page_info.get("from_name", "")
        from_email = page_info.get("from_email", "")
        sender_display = from_name or from_email or "Unknown"
        date_str = page_info.get("date", "")
        ai_priority = page_info.get("ai_priority", "")
        ai_action = page_info.get("ai_action", "")
        page_id = page_info.get("page_id", "")

        notion_url = f"https://notion.so/{page_id.replace('-', '')}" if page_id else ""

        # 根据优先级选择卡片颜色
        template = "red" if ai_priority in ("🔴 紧急",) else "orange"

        card = {
            "header": {
                "title": {"content": "需要回复的邮件", "tag": "plain_text"},
                "template": template
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"content": f"**发件人**\n{sender_display}", "tag": "lark_md"}},
                        {"is_short": True, "text": {"content": f"**优先级**\n{ai_priority or 'N/A'}", "tag": "lark_md"}}
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"content": f"**操作**\n{ai_action or 'N/A'}", "tag": "lark_md"}},
                        {"is_short": True, "text": {"content": f"**时间**\n{date_str or 'N/A'}", "tag": "lark_md"}}
                    ]
                },
                {
                    "tag": "div",
                    "text": {"content": f"**主题**\n{subject}", "tag": "lark_md"}
                },
            ]
        }

        if notion_url:
            card["elements"].append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"content": "在 Notion 中查看", "tag": "plain_text"},
                    "type": "primary",
                    "url": notion_url
                }]
            })

        payload = {"msg_type": "interactive", "card": card}

        if self.secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = self._gen_sign(timestamp)

        try:
            session = await self._get_session()
            async with session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 0:
                        logger.info(f"Feishu notification sent: {subject[:50]}")
                        return True
                    else:
                        logger.error(f"Feishu API error: {result}")
                        return False
                else:
                    text = await resp.text()
                    logger.error(f"Feishu webhook failed: HTTP {resp.status} - {text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"Feishu notification failed: {e}")
            return False
