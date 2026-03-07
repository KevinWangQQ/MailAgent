# Openclaw 飞书卡片回调开发指南

## 概述

MailAgent 通过飞书应用机器人向用户推送重要邮件通知卡片，卡片包含两个交互按钮，点击后回调至 Openclaw AI Agent 处理。

**按钮一览**：

| 按钮 | action | 触发条件 | 用途 |
|------|--------|---------|------|
| 打开 Notion | 无（URL 跳转） | 始终 | 跳转 Notion 邮件页面 |
| ✨ 优化回复 | `enhance_reply` | 始终 | AI 检索上下文后生成高质量回复 |
| 📝 创建草稿 | `create_draft` | 有 reply_suggestion | 基于建议回复直接创建 Mail.app 草稿 |

## 回调入口

飞书应用机器人的卡片按钮点击后，飞书向应用配置的 **请求地址（Event URL）** 发送 POST 请求，事件类型为 `card.action.trigger`。

飞书 POST body 结构：
```json
{
  "schema": "2.0",
  "header": {
    "event_id": "...",
    "event_type": "card.action.trigger",
    "token": "verification_token"
  },
  "event": {
    "operator": {
      "open_id": "ou_xxx",
      "user_id": "xxx"
    },
    "action": {
      "value": { "action": "enhance_reply", "...": "..." },
      "tag": "button"
    }
  }
}
```

路由逻辑：
```python
value = event["event"]["action"]["value"]
action = value["action"]

if action == "enhance_reply":
    await handle_enhance_reply(value)
elif action == "create_draft":
    await handle_create_draft(value)
```

## 回调数据结构

两个按钮共享以下公共字段：

```json
{
  "action": "enhance_reply | create_draft",

  // ── 定位字段 ──
  "row_id": 12345,
  "page_id": "2ef15375-830a-4b12-...",
  "database_id": "2df15375830d8094...",
  "message_id": "MWHPR05MB3390...@namprd05.prod.outlook.com",
  "notion_url": "https://notion.so/2ef15375830...",

  // ── 邮件元数据 ──
  "subject": "【立项评审】Omada SDN Controller V6.3",
  "from_email": "nemo.mo@tp-link.com",
  "from_name": "Nemo Mo",
  "to": "yuanquan.chen@tp-link.com, alice@tp-link.com",
  "cc": "bob@tp-link.com",
  "date": "2026-03-06T17:25:00+08:00",
  "mailbox": "收件箱",

  // ── AI 标注 ──
  "ai_action": "需要回复",
  "ai_priority": "🔴 紧急"
}
```

**`enhance_reply` 额外字段**：

| 字段 | 最大长度 | 说明 |
|------|---------|------|
| `ai_summary` | 500 字符 | AI 生成的邮件摘要 |
| `reply_suggestion` | 800 字符 | AI 初步建议回复（可能为空） |

**`create_draft` 额外字段**：

| 字段 | 最大长度 | 说明 |
|------|---------|------|
| `reply_suggestion` | 800 字符 | AI 建议回复（此按钮仅在有值时显示） |

## 处理流程

### ✨ 优化回复 (`enhance_reply`)

```
用户点击 → Openclaw 收到回调
  ├─ 1. 通过 database_id + page_id 从 Notion API 获取邮件完整正文和附件
  ├─ 2. 通过 message_id 检索同一线程的历史邮件上下文
  ├─ 3. 结合 ai_summary、reply_suggestion（如有）和历史上下文
  │     调用 LLM 生成高质量回复
  ├─ 4. 通过飞书消息将优化回复发回用户
  │     （建议用交互式卡片，附「采纳并创建草稿」按钮）
  └─ 5. 用户确认后触发 create_draft 流程
```

### 📝 创建草稿 (`create_draft`)

```
用户点击 → Openclaw 收到回调
  ├─ 1. 从 reply_suggestion 获取回复正文
  ├─ 2. 调用 MailAgent AppleScript 接口创建草稿
  │     ├─ 收件人: from_email (原发件人)
  │     ├─ 抄送: 从 to/cc 中去掉自己后保留
  │     ├─ 主题: Re: {subject}
  │     └─ 正文: reply_suggestion
  ├─ 3. 通过飞书消息通知用户: "草稿已创建，请在 Mail.app 中查看"
  └─ 4. 可选: 更新 Notion Processing Status → 已完成
```

## 草稿创建 AppleScript 参考

```applescript
tell application "Mail"
    set newMsg to make new outgoing message with properties {
        subject: "Re: 【立项评审】Omada SDN Controller V6.3",
        content: "Hi Nemo，收到...",
        visible: true
    }
    tell newMsg
        make new to recipient at end of to recipients with properties {
            address: "nemo.mo@tp-link.com",
            name: "Nemo Mo"
        }
        -- 可选: 添加 CC
        make new cc recipient at end of cc recipients with properties {
            address: "alice@tp-link.com"
        }
    end tell
    -- 不调用 send，仅保存为草稿
end tell
```

Exchange 账户的草稿会自动同步到 Outlook，用户可在任意设备查看和发送。

## 安全建议

- 验证飞书签名：使用 Encrypt Key 验证请求来源
- `value` 字段有长度截断（`ai_summary` ≤ 500, `reply_suggestion` ≤ 800）
- 建议对 `page_id` 做二次验证，确认页面存在且属于当前用户
- `database_id` 可用于确认操作范围在正确的 Notion 数据库内
