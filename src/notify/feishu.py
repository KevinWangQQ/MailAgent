"""
飞书应用机器人通知模块

通过飞书 Open API 发送交互式卡片消息，用于通知重要邮件。
支持卡片按钮回调（由 Openclaw 处理）。
"""

import json
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

import aiohttp
from loguru import logger


class FeishuNotifier:
    """飞书应用机器人通知器"""

    NOTIFY_MAX_AGE_DAYS = 3
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        chat_id: str = "",
        webhook_url: str = "",
        secret: str = "",
        database_id: str = "",
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self._database_id = database_id
        # webhook 作为 fallback
        self.webhook_url = webhook_url
        self.webhook_secret = secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: str = ""
        self._token_expire: float = 0
        self._use_app_api = bool(app_id and app_secret and chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        session = await self._get_session()
        async with session.post(self.TOKEN_URL, json={
            "app_id": self.app_id, "app_secret": self.app_secret
        }) as resp:
            data = await resp.json()
            if data.get("code") != 0:
                logger.error(f"Feishu token failed: {data}")
                return ""
            self._token = data["tenant_access_token"]
            self._token_expire = time.time() + data.get("expire", 7200)
            return self._token

    def _is_recent(self, date_str: str) -> bool:
        if not date_str:
            return True
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            cutoff = datetime.now(dt.tzinfo) - timedelta(days=self.NOTIFY_MAX_AGE_DAYS)
            return dt >= cutoff
        except (ValueError, TypeError):
            return True

    async def notify_important_email(self, page_info: Dict) -> bool:
        """发送重要邮件通知卡片"""
        if not self._use_app_api and not self.webhook_url:
            return False

        # 跳过发件箱邮件
        mailbox = page_info.get("mailbox", "")
        if mailbox in ("发件箱", "已发送邮件", "已发送"):
            return False

        date_str = page_info.get("date", "")
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
        internal_id = page_info.get("internal_id")
        message_id = page_info.get("message_id", "")
        category = page_info.get("category", "")
        reply_suggestion = page_info.get("reply_suggestion", "")
        to_addr = page_info.get("to_addr", "")
        cc_addr = page_info.get("cc_addr", "")

        notion_url = f"https://notion.so/{page_id.replace('-', '')}" if page_id else ""
        template = "red" if ai_priority in ("🔴 紧急",) else "orange"

        card = self._build_card(
            subject=subject, sender_display=sender_display,
            ai_priority=ai_priority, ai_action=ai_action,
            category=category, date_str=date_str,
            ai_summary=ai_summary, reply_suggestion=reply_suggestion,
            notion_url=notion_url, template=template,
            page_id=page_id, message_id=message_id,
            row_id=row_id, internal_id=internal_id,
            from_email=from_email,
            to_addr=to_addr, cc_addr=cc_addr,
            mailbox=mailbox,
        )

        if self._use_app_api:
            return await self._send_via_app_api(card, subject)
        return await self._send_via_webhook(card, subject)

    def _build_card(self, **kw) -> Dict:
        subject = kw["subject"]
        sender_display = kw["sender_display"]
        ai_priority = kw["ai_priority"]
        ai_action = kw["ai_action"]
        category = kw["category"]
        date_str = kw["date_str"]
        ai_summary = kw["ai_summary"]
        reply_suggestion = kw["reply_suggestion"]
        notion_url = kw["notion_url"]
        template = kw["template"]
        page_id = kw["page_id"]
        message_id = kw["message_id"]
        row_id = kw["row_id"]
        internal_id = kw.get("internal_id")
        from_email = kw["from_email"]
        to_addr = kw.get("to_addr", "")
        cc_addr = kw.get("cc_addr", "")
        mailbox = kw.get("mailbox", "")

        elements = [
            {"tag": "markdown", "content": f"**{subject}**"},
            {
                "tag": "column_set",
                "flex_mode": "trisect",
                "background_style": "grey",
                "columns": [
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "elements": [{"tag": "markdown", "content": f"**发件人**\n{sender_display}"}]
                    },
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "elements": [{"tag": "markdown", "content": f"**优先级**\n{ai_priority or 'N/A'}"}]
                    },
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "elements": [{"tag": "markdown", "content": f"**时间**\n{date_str[:16] if date_str else 'N/A'}"}]
                    },
                ]
            },
        ]

        if ai_summary:
            elements.append({"tag": "markdown", "content": f"**📝 概要**\n{ai_summary[:300]}"})

        if reply_suggestion:
            if len(reply_suggestion) > 150:
                elements.append({
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "plain_text", "content": "💡 建议回复（点击展开）"}},
                    "elements": [{"tag": "markdown", "content": reply_suggestion[:800]}]
                })
            else:
                elements.append({"tag": "markdown", "content": f"**💡 建议回复**\n{reply_suggestion}"})

        elements.append({"tag": "hr"})

        # 完整信息折叠面板
        info_data = {
            "internal_id": internal_id, "page_id": page_id,
            "database_id": self._database_id, "message_id": message_id,
            "subject": subject, "from": sender_display, "from_email": from_email,
            "to": to_addr, "cc": cc_addr,
            "date": date_str, "mailbox": mailbox,
            "action": ai_action, "priority": ai_priority, "category": category,
            "summary": ai_summary[:200] if ai_summary else "",
            "reply_suggestion": reply_suggestion[:300] if reply_suggestion else "",
            "notion_url": notion_url,
        }
        info_json = json.dumps(info_data, ensure_ascii=False, indent=2)

        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": "📋 完整信息（点击展开复制）"}},
            "elements": [{"tag": "markdown", "content": f"```json\n{info_json}\n```"}]
        })

        # 按钮行
        actions = []
        if notion_url:
            actions.append({
                "tag": "button",
                "text": {"content": "打开 Notion", "tag": "plain_text"},
                "type": "default",
                "url": notion_url
            })

        # 按钮回调公共字段
        base_callback = {
            "internal_id": internal_id, "page_id": page_id,
            "database_id": self._database_id,
            "message_id": message_id, "notion_url": notion_url,
            "subject": subject, "mailbox": mailbox,
            "from_email": from_email, "from_name": sender_display,
            "to": to_addr, "cc": cc_addr,
            "date": date_str,
            "chat_id": self.chat_id,
            "ai_action": ai_action, "ai_priority": ai_priority,
        }
        actions.append({
            "tag": "button",
            "text": {"content": "✨ 优化回复", "tag": "plain_text"},
            "type": "primary",
            "value": {**base_callback, "action": "enhance_reply",
                      "ai_summary": (ai_summary or "")[:500],
                      "reply_suggestion": (reply_suggestion or "")[:800]}
        })

        # 「📝 创建草稿」— 基于现有建议回复直接生成 Mail.app 草稿
        if reply_suggestion:
            actions.append({
                "tag": "button",
                "text": {"content": "📝 创建草稿", "tag": "plain_text"},
                "type": "default",
                "value": {**base_callback, "action": "create_draft",
                          "reply_suggestion": reply_suggestion[:800]}
            })

        if actions:
            elements.append({"tag": "action", "actions": actions})

        return {
            "header": {
                "title": {"content": f"📬 {ai_action or '需要处理'}", "tag": "plain_text"},
                "template": template,
            },
            "elements": elements,
        }

    async def _send_via_app_api(self, card: Dict, subject: str) -> bool:
        token = await self._get_token()
        if not token:
            return False
        try:
            session = await self._get_session()
            headers = {"Authorization": f"Bearer {token}"}
            async with session.post(
                self.MSG_URL,
                params={"receive_id_type": "chat_id"},
                headers=headers,
                json={
                    "receive_id": self.chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("code") != 0:
                    logger.error(f"Feishu app API error: {data}")
                    return False

            msg_id = data.get("data", {}).get("message_id", "")
            logger.info(f"Feishu app notification sent: {subject[:50]} ({msg_id})")

            # 回写 open_message_id 到按钮回调
            if msg_id:
                self._inject_open_message_id(card, msg_id)
                async with session.patch(
                    f"{self.MSG_URL}/{msg_id}",
                    headers=headers,
                    json={"content": json.dumps(card)},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as patch_resp:
                    patch_data = await patch_resp.json()
                    if patch_data.get("code") != 0:
                        logger.warning(f"Feishu PATCH open_message_id failed: {patch_data}")

            return True
        except Exception as e:
            logger.error(f"Feishu app notification failed: {e}")
            return False

    @staticmethod
    def _inject_open_message_id(card: Dict, msg_id: str):
        """将 open_message_id 注入所有按钮 value"""
        for el in card.get("elements", []):
            if el.get("tag") == "action":
                for btn in el.get("actions", []):
                    if isinstance(btn.get("value"), dict):
                        btn["value"]["open_message_id"] = msg_id

    async def _send_via_webhook(self, card: Dict, subject: str) -> bool:
        """Webhook fallback"""
        import hmac, hashlib, base64
        payload = {"msg_type": "interactive", "card": card}
        if self.webhook_secret:
            timestamp = int(time.time())
            string_to_sign = f"{timestamp}\n{self.webhook_secret}"
            hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            payload["timestamp"] = str(timestamp)
            payload["sign"] = base64.b64encode(hmac_code).decode("utf-8")
        try:
            session = await self._get_session()
            async with session.post(
                self.webhook_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 0:
                        logger.info(f"Feishu webhook notification sent: {subject[:50]}")
                        return True
                    logger.error(f"Feishu webhook error: {result}")
                return False
        except Exception as e:
            logger.error(f"Feishu webhook notification failed: {e}")
            return False
