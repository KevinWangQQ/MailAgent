"""
飞书自定义机器人通知模块

通过 webhook 发送交互式卡片消息，用于通知重要邮件。
支持签名验证（可选）。
"""

import time
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from typing import Dict, Optional

import aiohttp
from loguru import logger


class FeishuNotifier:
    """飞书自定义机器人通知器"""

    # 只通知最近 N 天内的邮件，防止补偿同步时的通知风暴
    NOTIFY_MAX_AGE_DAYS = 3

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

    def _is_recent(self, date_str: str) -> bool:
        """检查邮件日期是否在通知窗口内"""
        if not date_str:
            return True  # 无日期信息时默认通知
        try:
            # 支持 ISO 格式: 2026-03-05T10:30:00+08:00 或 2026-03-05
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            cutoff = datetime.now(dt.tzinfo) - timedelta(days=self.NOTIFY_MAX_AGE_DAYS)
            return dt >= cutoff
        except (ValueError, TypeError):
            return True

    async def notify_important_email(self, page_info: Dict) -> bool:
        """发送重要邮件通知卡片"""
        if not self.webhook_url:
            return False

        date_str = page_info.get("date", "")

        # 跳过过期邮件通知
        if not self._is_recent(date_str):
            logger.debug(f"Skipping notification for old email: {page_info.get('subject', '')[:40]}")
            return False

        subject = page_info.get("subject", "(No Subject)")
        from_name = page_info.get("from_name", "")
        from_email = page_info.get("from_email", "")
        sender_display = from_name or from_email or "Unknown"
        ai_priority = page_info.get("ai_priority", "")
        ai_action = page_info.get("ai_action", "")
        page_id = page_info.get("page_id", "")
        ai_summary = page_info.get("ai_summary", "")
        row_id = page_info.get("row_id")
        message_id = page_info.get("message_id", "")
        category = page_info.get("category", "")

        notion_url = f"https://notion.so/{page_id.replace('-', '')}" if page_id else ""
        template = "red" if ai_priority in ("🔴 紧急",) else "orange"

        card = {
            "header": {
                "title": {"content": f"📬 {ai_action or '需要处理'}", "tag": "plain_text"},
                "template": template
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"content": f"**{subject}**", "tag": "lark_md"}
                },
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
                        {"is_short": True, "text": {"content": f"**分类**\n{category or 'N/A'}", "tag": "lark_md"}},
                        {"is_short": True, "text": {"content": f"**时间**\n{date_str[:16] if date_str else 'N/A'}", "tag": "lark_md"}}
                    ]
                },
            ]
        }

        # AI 概要
        if ai_summary:
            card["elements"].append({
                "tag": "div",
                "text": {"content": f"**概要**\n{ai_summary[:300]}", "tag": "lark_md"}
            })

        # 分隔线
        card["elements"].append({"tag": "hr"})

        # 可复制的结构化信息（供 Openclaw 使用）
        info_lines = [f"Row ID: {row_id or 'N/A'}"]
        if message_id:
            info_lines.append(f"Message-ID: {message_id[:60]}")
        info_lines.append(f"Action: {ai_action or 'N/A'}")
        info_lines.append(f"Priority: {ai_priority or 'N/A'}")
        info_block = "\n".join(info_lines)

        card["elements"].append({
            "tag": "div",
            "text": {"content": f"```\n{info_block}\n```", "tag": "lark_md"}
        })

        # 按钮行
        actions = []
        if notion_url:
            actions.append({
                "tag": "button",
                "text": {"content": "在 Notion 中查看", "tag": "plain_text"},
                "type": "primary",
                "url": notion_url
            })
        if actions:
            card["elements"].append({"tag": "action", "actions": actions})

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
