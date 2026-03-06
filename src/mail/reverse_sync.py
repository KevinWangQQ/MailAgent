"""
反向同步模块: Notion -> Mail.app

当 AI 审核完邮件后，将操作同步回 Mail.app，并对重要邮件发送飞书通知。
支持的操作:
- Mark Read: 标记已读
- Flag Important: 设置旗标
- Mark Read and Flag: 标记已读并设置旗标
- Archive: 归档（当前实现为标记已读）
"""

from datetime import datetime
from typing import Dict, Optional
from loguru import logger

from src.mail.applescript_arm import AppleScriptArm
from src.mail.sync_store import SyncStore
from src.notion.sync import NotionSync
from src.config import config


class NotionToMailSync:
    """反向同步: Notion -> Mail.app"""

    ACTION_MARK_READ = "Mark Read"
    ACTION_FLAG_IMPORTANT = "Flag Important"
    ACTION_MARK_READ_AND_FLAG = "Mark Read and Flag"
    ACTION_ARCHIVE = "Archive"

    # 需要飞书通知的 AI Action（包含 Flag 的动作表示重要）
    NOTIFY_ACTIONS = {ACTION_FLAG_IMPORTANT, ACTION_MARK_READ_AND_FLAG}

    def __init__(
        self,
        notion_sync: NotionSync = None,
        arm: AppleScriptArm = None,
        sync_store: SyncStore = None
    ):
        self.notion_sync = notion_sync or NotionSync()
        self.arm = arm or AppleScriptArm()
        self.sync_store = sync_store
        self.last_check: Optional[datetime] = None
        self.sync_count = 0
        self.error_count = 0
        self.notify_count = 0

        # 飞书通知器（延迟初始化）
        self._feishu = None
        if config.feishu_notify_enabled and config.feishu_webhook_url:
            from src.notify.feishu import FeishuNotifier
            self._feishu = FeishuNotifier(
                webhook_url=config.feishu_webhook_url,
                secret=config.feishu_webhook_secret
            )
            logger.info("Feishu notification enabled")

        logger.info("NotionToMailSync initialized")

    async def close(self):
        if self._feishu:
            await self._feishu.close()

    async def check_and_sync(self) -> Dict[str, int]:
        """检查 Notion 状态变更并同步到 Mail.app"""
        stats = {"synced": 0, "failed": 0, "skipped": 0, "notified": 0}

        try:
            pages = await self.notion_sync.query_pages_for_reverse_sync()

            if not pages:
                logger.debug("No pages need reverse sync")
                self.last_check = datetime.now()
                return stats

            logger.info(f"Found {len(pages)} pages for reverse sync")

            for page in pages:
                try:
                    success = await self.sync_single_page(page)
                    if success:
                        stats["synced"] += 1
                        # 飞书通知
                        if await self._try_notify(page):
                            stats["notified"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as e:
                    logger.error(f"Failed to sync page {page.get('page_id', '?')}: {e}")
                    stats["failed"] += 1

            self.sync_count += stats["synced"]
            self.error_count += stats["failed"]
            self.notify_count += stats["notified"]

            logger.info(
                f"Reverse sync: synced={stats['synced']}, "
                f"failed={stats['failed']}, notified={stats['notified']}"
            )

        except Exception as e:
            logger.error(f"Reverse sync check failed: {e}")

        self.last_check = datetime.now()
        return stats

    async def sync_single_page(self, page: Dict) -> bool:
        """同步单个 Notion 页面到 Mail.app"""
        page_id = page.get("page_id")
        message_id = page.get("message_id")
        ai_action = page.get("ai_action")
        mailbox = page.get("mailbox") or None

        if not message_id:
            logger.warning(f"Page {page_id} has no Message ID, skipping")
            return False

        msg_short = message_id[:40] + "..." if len(message_id) > 40 else message_id
        logger.info(f"Syncing to Mail: {msg_short} action={ai_action}")

        # 查找 internal_id（优先快速路径）
        internal_id = self._lookup_internal_id(message_id)

        success = False
        if ai_action == self.ACTION_MARK_READ:
            success = self._do_mark_read(internal_id, message_id, mailbox)
        elif ai_action == self.ACTION_FLAG_IMPORTANT:
            success = self._do_flag(internal_id, message_id, mailbox)
        elif ai_action == self.ACTION_MARK_READ_AND_FLAG:
            success = self._do_mark_read_and_flag(internal_id, message_id, mailbox)
        elif ai_action == self.ACTION_ARCHIVE:
            success = self._do_mark_read(internal_id, message_id, mailbox)
        else:
            if ai_action:
                logger.warning(f"Unknown action '{ai_action}', defaulting to mark as read")
            success = self._do_mark_read(internal_id, message_id, mailbox)

        if success:
            # Echo prevention: 更新 SyncStore 的 flags 使其与 Mail.app 一致
            # 这样 flag 变化检测不会误报刚被 reverse sync 修改的邮件
            self._update_store_flags(internal_id, ai_action)

            try:
                await self.notion_sync.update_page_mail_sync_status(page_id, synced=True)
                logger.info(f"Reverse sync completed for {msg_short}")
            except Exception as e:
                logger.error(f"Failed to update Notion sync status: {e}")
                return False
        else:
            logger.error(f"Failed to execute action on Mail.app: {msg_short}")

        return success

    def _lookup_internal_id(self, message_id: str) -> Optional[int]:
        """从 SyncStore 查找 internal_id"""
        if not self.sync_store:
            return None
        try:
            record = self.sync_store.get_by_message_id(message_id)
            if record:
                return record.internal_id if hasattr(record, 'internal_id') else record.get('internal_id')
        except Exception:
            pass
        return None

    def _do_mark_read(self, internal_id: Optional[int], message_id: str, mailbox: str = None) -> bool:
        try:
            if internal_id:
                return self.arm.mark_as_read_by_id(internal_id, True, mailbox)
            return self.arm.mark_as_read(message_id, True, mailbox)
        except Exception as e:
            logger.error(f"mark_as_read failed: {e}")
            return False

    def _do_flag(self, internal_id: Optional[int], message_id: str, mailbox: str = None) -> bool:
        try:
            if internal_id:
                return self.arm.set_flag_by_id(internal_id, True, mailbox)
            return self.arm.set_flag(message_id, True, mailbox)
        except Exception as e:
            logger.error(f"set_flag failed: {e}")
            return False

    def _do_mark_read_and_flag(self, internal_id: Optional[int], message_id: str, mailbox: str = None) -> bool:
        try:
            if not self._do_mark_read(internal_id, message_id, mailbox):
                return False
            return self._do_flag(internal_id, message_id, mailbox)
        except Exception as e:
            logger.error(f"mark_read_and_flag failed: {e}")
            return False

    async def _try_notify(self, page: Dict) -> bool:
        """检查是否需要发送飞书通知并发送"""
        if not self._feishu:
            return False

        ai_action = page.get("ai_action", "")
        ai_priority = page.get("ai_priority", "")

        # 触发条件：AI Action 包含 Flag，或 AI Priority 为 Important/Critical/Urgent
        should_notify = (
            ai_action in self.NOTIFY_ACTIONS
            or ai_priority in ("Important", "Critical", "Urgent")
        )

        if not should_notify:
            return False

        return await self._feishu.notify_important_email(page)

    def _update_store_flags(self, internal_id: Optional[int], ai_action: str):
        """Echo prevention: 反向同步后更新 SyncStore flags 使其与 Mail.app 一致"""
        if not self.sync_store or not internal_id:
            return

        try:
            record = self.sync_store.get(internal_id)
            if not record:
                return

            is_read = record.get('is_read', False) if isinstance(record, dict) else getattr(record, 'is_read', False)
            is_flagged = record.get('is_flagged', False) if isinstance(record, dict) else getattr(record, 'is_flagged', False)

            if ai_action in (self.ACTION_MARK_READ, self.ACTION_MARK_READ_AND_FLAG, self.ACTION_ARCHIVE):
                is_read = True
            if ai_action in (self.ACTION_FLAG_IMPORTANT, self.ACTION_MARK_READ_AND_FLAG):
                is_flagged = True

            self.sync_store.update_local_flags(internal_id, bool(is_read), bool(is_flagged))
        except Exception as e:
            logger.warning(f"Failed to update store flags for echo prevention: {e}")

    def get_stats(self) -> Dict:
        return {
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "total_synced": self.sync_count,
            "total_errors": self.error_count,
            "total_notified": self.notify_count,
        }
