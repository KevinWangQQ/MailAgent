# MailAgent Webhook Server 部署指南

与 Notion2JIRA 部署在同一台服务器，共用 Redis 服务（不同 DB），PM2 统一监控。

## 前置条件

- Python 3.9+
- Redis（已有，Notion2JIRA 使用中）
- PM2（已有）
- Nginx + Let's Encrypt（已有）

## 1. 部署代码

```bash
cd /opt
git clone <repo-url> mailagent-webhook
cd mailagent-webhook/webhook-server

# Python 虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置

```bash
cp .env.example .env
vim .env
```

```env
REDIS_URL=redis://localhost:6379
REDIS_DB=2
WEBHOOK_SECRET=<生成一个随机 token>
QUEUE_TTL_DAYS=7
```

**Redis DB 分配：**

| DB | 用途 |
|----|------|
| 0-1 | Notion2JIRA |
| 2 | MailAgent webhook 事件队列 |

生成 secret：
```bash
openssl rand -hex 32
```

## 3. PM2 启动

```bash
mkdir -p logs
pm2 start ecosystem.config.js
pm2 save
```

验证：
```bash
pm2 status
pm2 logs mailagent-webhook --lines 20
curl http://127.0.0.1:8100/health
```

## 4. Nginx 反向代理

创建站点配置：
```bash
sudo vim /etc/nginx/sites-available/mailagent-webhook
```

```nginx
server {
    listen 80;
    server_name mailagent-webhook.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/mailagent-webhook /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 5. HTTPS（Let's Encrypt）

```bash
sudo certbot --nginx -d mailagent-webhook.yourdomain.com
```

## 6. Notion Automation 配置

在 Notion 邮件数据库中创建 Automation：

**Trigger 1 — Flag 变化：**
- When: `Is Read` 或 `Is Flagged` property is edited
- Action: Send webhook
  - URL: `https://mailagent-webhook.yourdomain.com/webhook/notion?event=flag_changed`
  - Header: `Authorization: Bearer <WEBHOOK_SECRET>`

**Trigger 2 — AI Review 完成：**
- When: `AI Review Status` is set to `Reviewed`
- Action: Send webhook
  - URL: `https://mailagent-webhook.yourdomain.com/webhook/notion?event=ai_reviewed`
  - Header: `Authorization: Bearer <WEBHOOK_SECRET>`

## 7. 本地 MailAgent 配置

在 macOS 端的 MailAgent `.env` 中添加：

```env
# Redis 事件消费（Notion → Mail 方向）
REDIS_URL=redis://<服务器IP>:6379
REDIS_DB=2
REDIS_EVENTS_ENABLED=true

# 飞书通知（可选）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<your-hook-id>
FEISHU_WEBHOOK_SECRET=<签名密钥>
FEISHU_NOTIFY_ENABLED=true
```

> **注意**：如果 Redis 不在公网，需要确保 macOS 能访问服务器的 6379 端口（VPN / SSH tunnel / 安全组放行）。

## 8. 验证

```bash
# 服务器端
curl https://mailagent-webhook.yourdomain.com/health

# 手动推送测试事件
curl -X POST https://mailagent-webhook.yourdomain.com/webhook/notion?event=flag_changed \
  -H "Authorization: Bearer <WEBHOOK_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "test-page-id",
    "parent": {"database_id": "<your-database-id>"},
    "properties": {
      "Message ID": {"rich_text": [{"text": {"content": "test@example.com"}}]},
      "Is Read": {"checkbox": true}
    }
  }'

# 检查 Redis 队列
redis-cli -n 2 LLEN "mailagent:<database_id_no_dashes>:events"
```

## 运维

```bash
# 查看队列状态
curl -H "X-Webhook-Token: <SECRET>" https://mailagent-webhook.yourdomain.com/admin/stats

# PM2 常用
pm2 restart mailagent-webhook
pm2 logs mailagent-webhook
pm2 monit
```
