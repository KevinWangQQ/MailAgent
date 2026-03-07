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

## ID 体系说明

回调中涉及多个不同层面的 ID，务必区分：

| 字段 | 归属 | 含义 | 用途 |
|------|------|------|------|
| `internal_id` | **Mail.app** | SQLite ROWID = AppleScript `id`（整数） | 快速操作 Mail.app（`whose id is <int>` ~1s） |
| `message_id` | **邮件标准** | RFC 2822 Message-ID（字符串） | 邮件系统间的通用标识符 |
| `page_id` | **Notion** | 页面 UUID | 查询/更新 Notion 页面 |
| `database_id` | **Notion** | 数据库 UUID | 确定操作范围 |

**关键性能差异**：
- `whose id is 41285`（internal_id）→ **~1 秒**
- `whose message id is "MWHPR05MB..."`（message_id）→ **~100 秒**

Openclaw 操作 Mail.app 时**始终优先使用 `internal_id`**，仅在 `internal_id` 为 null（邮件已删除）时 fallback 到 `message_id`。

## 回调数据结构

两个按钮共享以下公共字段：

```json
{
  "action": "enhance_reply | create_draft",

  // ── 定位字段 ──
  "internal_id": 41285,
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

**注意**：`internal_id` 可能为 `null`（邮件已从 Mail.app 删除），此时应使用 `message_id` 作为 fallback。

## 处理流程

### ✨ 优化回复 (`enhance_reply`)

```
用户点击 → Openclaw 收到回调
  ├─ 1. 通过 page_id 从 Notion 获取邮件完整正文
  │     推荐: Notion Markdown Export API (beta)，一次调用获取全文
  │     避免: 递归遍历 children blocks（慢且复杂）
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
  ├─ 2. 通过 AppleScript 创建草稿（见下方参考）
  │     ├─ 收件人: from_email (原发件人)
  │     ├─ 抄送: 从 to/cc 中去掉自己后保留
  │     ├─ 主题: Re: {subject}
  │     └─ 正文: reply_suggestion
  ├─ 3. 通过飞书消息通知用户: "草稿已创建，请在 Mail.app 中查看"
  └─ 4. 可选: 更新 Notion Processing Status → 已完成
```

## 性能优化建议

### Mail.app 操作

```python
# ✅ 推荐: 用 internal_id 快速查询 (~1s)
script = f'''
tell application "Mail"
    tell account "Exchange"
        tell mailbox "{mailbox_name}"
            set theMessage to first message whose id is {internal_id}
        end tell
    end tell
end tell
'''

# ❌ 避免: 用 message_id 字符串查询 (~100s)
# whose message id is "MWHPR05MB..."
```

**mailbox 路由**：回调中的 `mailbox` 字段（"收件箱"/"发件箱"）决定 AppleScript 搜索范围。发件箱对应 AppleScript 名称为 `"已发送邮件"`。

### Notion 内容获取

```python
# ✅ 推荐: Markdown Export API (beta) — 一次调用获取完整正文
# GET /v1/blocks/{page_id}/markdown
# 返回完整 Markdown 文本，无需递归遍历

# ❌ 避免: 递归遍历 children blocks
# GET /v1/blocks/{page_id}/children → 逐层递归 → 拼装文本
# 多次 API 调用，慢且容易遗漏嵌套内容
```

### 线程历史检索

```python
# 通过 database_id + Thread ID 关联查询同线程邮件
# Notion 数据库中 "Thread ID" 字段标识同一会话
# "Parent Item" Relation 字段指向线程头邮件
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
- 回调字段有长度截断（`ai_summary` ≤ 500, `reply_suggestion` ≤ 800）
- 建议对 `page_id` 做二次验证，确认页面存在且属于当前用户
- `database_id` 可用于确认操作范围在正确的 Notion 数据库内
- `internal_id` 可能为 null（邮件已删除），需做 null check
