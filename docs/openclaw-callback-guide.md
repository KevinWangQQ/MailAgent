# Openclaw 飞书卡片回调开发指南

## 回调入口

飞书应用机器人的卡片按钮点击后，飞书会向应用配置的 **请求地址（Event URL）** 发送 POST 请求。Openclaw 需要在其 webhook endpoint 中处理 `card.action.trigger` 类型的事件。

## 按钮回调数据结构

### 1. ✨ 优化回复 (`enhance_reply`)

**场景**：用户看到邮件通知后，希望 AI 基于完整上下文（邮件原文、历史线程、联系人关系）生成高质量回复。

**回调 `value`**：
```json
{
  "action": "enhance_reply",
  "message_id": "MWHPR05MB3390...@namprd05.prod.outlook.com",
  "page_id": "2ef15375-830a-4b...",
  "subject": "【立项评审】Omada SDN Controller V6.3",
  "from_email": "nemo.mo@tp-link.com",
  "from_name": "Nemo Mo",
  "notion_url": "https://notion.so/2ef15375830...",
  "ai_summary": "Omada SDN Controller V6.3 立项评审..."
}
```

**建议处理流程**：
1. 通过 `page_id` 从 Notion 获取邮件完整内容（正文、附件摘要）
2. 通过 `message_id` / `subject` 检索同一线程的历史邮件
3. 结合 `ai_summary` 和上下文，调用 LLM 生成优化回复
4. 将生成的回复通过飞书消息发回用户（可以用新卡片或更新原卡片）
5. 用户确认后可触发 `create_draft` 流程

### 2. 📝 创建草稿 (`create_draft`)

**场景**：用户对现有的 AI 建议回复满意，直接在 Mail.app 中创建邮件草稿。

**回调 `value`**：
```json
{
  "action": "create_draft",
  "message_id": "MWHPR05MB3390...@namprd05.prod.outlook.com",
  "page_id": "2ef15375-830a-4b...",
  "subject": "Re: 【立项评审】Omada SDN Controller V6.3",
  "from_email": "nemo.mo@tp-link.com",
  "from_name": "Nemo Mo",
  "notion_url": "https://notion.so/2ef15375830...",
  "reply_suggestion": "Hi Nemo，收到，我会参加立项评审会议。关于V6.3的功能规划..."
}
```

**建议处理流程**：
1. 从 `reply_suggestion` 获取回复内容
2. 调用 MailAgent 的 AppleScript 接口创建草稿：
   - 收件人：`from_email`
   - 主题：`Re: {subject}`（如原 subject 不含 `Re:` 前缀则添加）
   - 正文：`reply_suggestion`
3. 草稿创建成功后，通过飞书消息通知用户："草稿已创建，请在 Mail.app 中查看"
4. 可选：更新 Notion 页面的 Processing Status

## 飞书事件结构参考

飞书发送的卡片回调 POST body：
```json
{
  "schema": "2.0",
  "header": {
    "event_id": "...",
    "event_type": "card.action.trigger",
    "token": "..."
  },
  "event": {
    "operator": {
      "open_id": "ou_xxx",
      "user_id": "xxx"
    },
    "action": {
      "value": {
        "action": "enhance_reply",
        "message_id": "...",
        ...
      },
      "tag": "button"
    }
  }
}
```

核心路由逻辑：
```python
action = event["event"]["action"]["value"]["action"]

if action == "enhance_reply":
    await handle_enhance_reply(event["event"]["action"]["value"])
elif action == "create_draft":
    await handle_create_draft(event["event"]["action"]["value"])
```

## 草稿创建 AppleScript 参考

MailAgent 的 `AppleScriptArm` 支持通过 AppleScript 创建邮件草稿：

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
    end tell
    -- 不调用 send，仅保存为草稿
end tell
```

Exchange 账户的草稿会自动同步到 Outlook，用户可在任意设备查看和发送。

## 回调安全

- 验证飞书签名：使用 Encrypt Key 验证请求来源
- `value` 中的字段已做长度截断（summary ≤ 300, reply_suggestion ≤ 500）
- 建议 Openclaw 侧对 `page_id` 做二次验证，确认页面存在且属于当前用户
