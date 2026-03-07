"""
Webhook 事件处理器

处理从 Redis 队列收到的 Notion 变更事件：
- flag_changed: Is Read / Is Flagged 变化 → 同步到 Mail.app
- ai_reviewed: AI Review 完成 → 飞书通知
- completed: 用户标记已完成 → 移除 Mail.app 旗标
- page_updated: 通用事件 → 自动判断处理方式
"""

from typing import Dict, Optional
from loguru import logger

from src.mail.applescript_arm import AppleScriptArm
from src.mail.sync_store import SyncStore
from src.notify.feishu import FeishuNotifier


class EventHandlers:
    """Webhook 事件处理集合"""

    def __init__(
        self,
        arm: AppleScriptArm,
        sync_store: SyncStore,
        feishu: Optional[FeishuNotifier] = None,
    ):
        self.arm = arm
        self.sync_store = sync_store
        self.feishu = feishu

    async def handle_flag_changed(self, event: Dict):
        """处理 flag 变化事件: Notion → Mail.app"""
        props = event.get("properties", {})
        message_id = props.get("message_id", "")
        is_read = props.get("is_read")
        is_flagged = props.get("is_flagged")

        if not message_id:
            logger.warning(f"flag_changed event missing message_id: {event.get('id')}")
            return

        # 查找 internal_id
        record = self.sync_store.get_by_message_id(message_id)
        if not record:
            logger.warning(f"Email not found in SyncStore: {message_id[:40]}")
            return

        internal_id = record.get('internal_id') if isinstance(record, dict) else getattr(record, 'internal_id', None)
        mailbox = record.get('mailbox') if isinstance(record, dict) else getattr(record, 'mailbox', None)
        stored_read = bool(record.get('is_read') if isinstance(record, dict) else getattr(record, 'is_read', False))
        stored_flagged = bool(record.get('is_flagged') if isinstance(record, dict) else getattr(record, 'is_flagged', False))

        changed = False

        # 同步 read 状态
        if is_read is not None and is_read != stored_read:
            if internal_id:
                success = self.arm.mark_as_read_by_id(internal_id, is_read, mailbox)
            else:
                success = self.arm.mark_as_read(message_id, is_read, mailbox)
            if success:
                changed = True
                logger.info(f"Flag sync: read={is_read} for {message_id[:40]}")

        # 同步 flagged 状态
        if is_flagged is not None and is_flagged != stored_flagged:
            if internal_id:
                success = self.arm.set_flag_by_id(internal_id, is_flagged, mailbox)
            else:
                success = self.arm.set_flag(message_id, is_flagged, mailbox)
            if success:
                changed = True
                logger.info(f"Flag sync: flagged={is_flagged} for {message_id[:40]}")

        # 更新 SyncStore 防止 echo
        if changed and internal_id:
            new_read = is_read if is_read is not None else stored_read
            new_flagged = is_flagged if is_flagged is not None else stored_flagged
            self.sync_store.update_local_flags(internal_id, new_read, new_flagged)

    async def handle_ai_reviewed(self, event: Dict):
        """处理 AI Review 完成事件: 飞书通知"""
        props = event.get("properties", {})
        page_id = event.get("page_id", "")

        ai_priority = props.get("ai_priority", "")
        ai_action = props.get("ai_action", "")

        # 飞书通知：重要/紧急 且 需要行动
        notify_priorities = {"🔴 紧急", "🟡 重要"}
        flag_actions = {"需要回复", "需要决策", "需要Review", "需要会议", "需要跟进", "等待响应"}
        should_notify = (
            ai_priority in notify_priorities
            and ai_action in flag_actions
        )

        if should_notify and self.feishu:
            message_id = props.get("message_id", "")
            # 查找 internal_id
            internal_id = None
            if message_id and self.sync_store:
                record = self.sync_store.get_by_message_id(message_id)
                if record:
                    internal_id = record.get('internal_id') if isinstance(record, dict) else getattr(record, 'internal_id', None)

            await self.feishu.notify_important_email({
                "page_id": page_id,
                "message_id": message_id,
                "internal_id": internal_id,
                "subject": props.get("subject", ""),
                "from_name": props.get("from_name", ""),
                "from_email": props.get("from_email", ""),
                "to_addr": props.get("to_addr", ""),
                "cc_addr": props.get("cc_addr", ""),
                "date": props.get("date", ""),
                "mailbox": props.get("mailbox", ""),
                "ai_action": ai_action,
                "ai_priority": ai_priority,
                "ai_summary": props.get("ai_summary", ""),
                "reply_suggestion": props.get("reply_suggestion", ""),
                "category": props.get("category", ""),
            })

    async def handle_completed(self, event: Dict):
        """处理用户标记已完成事件: 移除 Mail.app 旗标"""
        props = event.get("properties", {})
        message_id = props.get("message_id", "")

        if not message_id:
            logger.warning(f"completed event missing message_id: {event.get('id')}")
            return

        record = self.sync_store.get_by_message_id(message_id)
        if not record:
            logger.warning(f"Email not found in SyncStore: {message_id[:40]}")
            return

        internal_id = record.get('internal_id') if isinstance(record, dict) else getattr(record, 'internal_id', None)
        mailbox = record.get('mailbox') if isinstance(record, dict) else getattr(record, 'mailbox', None)
        stored_flagged = bool(record.get('is_flagged') if isinstance(record, dict) else getattr(record, 'is_flagged', False))

        if not stored_flagged:
            logger.debug(f"Already unflagged, skipping: {message_id[:40]}")
            return

        # 移除旗标 + 标记已读
        if internal_id:
            self.arm.set_flag_by_id(internal_id, False, mailbox)
            self.arm.mark_as_read_by_id(internal_id, True, mailbox)
        else:
            self.arm.set_flag(message_id, False, mailbox)
            self.arm.mark_as_read(message_id, True, mailbox)

        # Echo prevention
        if internal_id:
            self.sync_store.update_local_flags(internal_id, True, False)

        logger.info(f"Completed: unflagged {message_id[:40]}")

    async def handle_page_updated(self, event: Dict):
        """通用事件: 根据内容自动判断"""
        props = event.get("properties", {})
        ai_review_status = props.get("ai_review_status", "")

        if ai_review_status == "AI Reviewed":
            await self.handle_ai_reviewed(event)
        elif ai_review_status == "已完成":
            await self.handle_completed(event)

        # 始终检查 flag 变化
        if "is_read" in props or "is_flagged" in props:
            await self.handle_flag_changed(event)
