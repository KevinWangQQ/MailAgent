import asyncio
import signal
import sys

from loguru import logger
from src.config import config
from src.utils.logger import setup_logger

# 设置日志
setup_logger(config.log_level, config.log_file)

class EmailNotionSyncApp:
    """邮件同步应用主类"""

    def __init__(self):
        from src.mail.new_watcher import NewWatcher
        logger.info("Using NewWatcher (SQLite Radar + AppleScript Arm)")

        # 解析邮箱列表
        mailboxes = [mb.strip() for mb in config.sync_mailboxes.split(',') if mb.strip()]
        if not mailboxes:
            mailboxes = ["收件箱"]

        self.watcher = NewWatcher(
            mailboxes=mailboxes,
            poll_interval=config.radar_poll_interval,
            sync_store_path=config.sync_store_db_path
        )

        # 反向同步（Notion -> Mail.app + 飞书通知）
        from src.mail.reverse_sync import NotionToMailSync
        self.reverse_sync = NotionToMailSync(
            notion_sync=self.watcher.notion_sync,
            arm=self.watcher.arm,
            sync_store=self.watcher.sync_store
        )

        # Redis 事件消费（P3: Notion webhook → Redis → Mail.app）
        self.redis_consumer = None
        if config.redis_events_enabled and config.redis_url:
            from src.events.redis_consumer import RedisConsumer
            from src.events.handlers import EventHandlers

            queue_key = f"mailagent:{config.email_database_id.replace('-', '')}:events"
            self.redis_consumer = RedisConsumer(
                redis_url=config.redis_url,
                redis_db=config.redis_db,
                queue_key=queue_key,
            )

            # 构建飞书通知器（复用 reverse_sync 的或新建）
            feishu = self.reverse_sync._feishu

            handlers = EventHandlers(
                arm=self.watcher.arm,
                sync_store=self.watcher.sync_store,
                feishu=feishu,
                notion_sync=self.watcher.notion_sync,
            )

            self.redis_consumer.on("flag_changed", handlers.handle_flag_changed)
            self.redis_consumer.on("ai_reviewed", handlers.handle_ai_reviewed)
            self.redis_consumer.on("completed", handlers.handle_completed)
            self.redis_consumer.on("create_draft", handlers.handle_create_draft)
            self.redis_consumer.on("page_updated", handlers.handle_page_updated)

            logger.info(f"Redis event consumer configured: queue={queue_key}")

        self._shutdown_event = asyncio.Event()

    def _handle_signal(self, signum, frame):
        """处理系统信号"""
        sig_name = signal.Signals(signum).name
        logger.info(f"Received signal {sig_name}, initiating graceful shutdown...")
        self._shutdown_event.set()

    async def start(self):
        """启动应用"""
        logger.info("=" * 60)
        logger.info("Email to Notion Sync Service")
        logger.info("=" * 60)
        logger.info(f"User: {config.user_email}")
        logger.info(f"Poll interval: {config.radar_poll_interval}s")
        logger.info(f"Log level: {config.log_level}")
        logger.info("=" * 60)

        # 注册信号处理器
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            # 启动邮件监听器（在后台任务中运行）
            watcher_task = asyncio.create_task(self.watcher.start())

            # 启动反向同步循环
            reverse_task = asyncio.create_task(self._reverse_sync_loop())

            # 启动 Redis 事件消费（如果配置）
            redis_task = None
            if self.redis_consumer:
                redis_task = asyncio.create_task(
                    self.redis_consumer.start(shutdown_event=self._shutdown_event)
                )

            # 等待关闭信号
            await self._shutdown_event.wait()

            # 停止组件
            logger.info("Stopping services...")
            await self.watcher.stop()
            if self.redis_consumer:
                await self.redis_consumer.stop()

            # 取消任务
            tasks = [watcher_task, reverse_task]
            if redis_task:
                tasks.append(redis_task)
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # 关闭反向同步资源
            await self.reverse_sync.close()

            # 打印最终统计
            stats = self.watcher.get_stats()
            rs_stats = self.reverse_sync.get_stats()
            logger.info(f"Final stats: synced={stats.get('emails_synced', 0)}, flags={stats.get('flag_changes_synced', 0)}, errors={stats.get('errors', 0)}")
            logger.info(f"Reverse sync: synced={rs_stats.get('total_synced', 0)}, notified={rs_stats.get('total_notified', 0)}")
            if self.redis_consumer:
                rc_stats = self.redis_consumer.get_stats()
                logger.info(f"Redis consumer: received={rc_stats.get('received', 0)}, processed={rc_stats.get('processed', 0)}")
            logger.info("Shutdown complete")

        except Exception as e:
            logger.error(f"Fatal error: {e}")
            sys.exit(1)

    async def _reverse_sync_loop(self):
        """反向同步循环: 定期检查 Notion AI Review 结果并同步到 Mail.app"""
        interval = config.reverse_sync_interval
        logger.info(f"Reverse sync loop started (interval={interval}s)")

        while not self._shutdown_event.is_set():
            try:
                await self.reverse_sync.check_and_sync()
            except Exception as e:
                logger.error(f"Reverse sync error: {e}")

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                break  # shutdown event set
            except asyncio.TimeoutError:
                pass  # normal timeout, continue loop

async def main():
    """主函数"""
    app = EmailNotionSyncApp()
    await app.start()

if __name__ == "__main__":
    asyncio.run(main())
