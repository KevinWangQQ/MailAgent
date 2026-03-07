"""
MailAgent Webhook Server

接收 Notion Automation webhook 和外部系统指令，按 database_id 路由到 Redis 队列。
每个用户的 MailAgent 实例 BLPOP 自己的队列。

部署: uvicorn app:app --host 0.0.0.0 --port 8100
"""

import asyncio
import json
import os
import time
import uuid
import base64
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field

# Config
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_DB = int(os.getenv("REDIS_DB", "2"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
QUEUE_PREFIX = "mailagent"
QUEUE_TTL_DAYS = int(os.getenv("QUEUE_TTL_DAYS", "7"))

redis_pool: Optional[redis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pool
    redis_pool = redis.from_url(f"{REDIS_URL}/{REDIS_DB}", decode_responses=True)
    yield
    if redis_pool:
        await redis_pool.close()


APP_DESCRIPTION = """
## 概述

MailAgent Webhook Server 是邮件同步系统的中间层，负责接收外部指令并路由到 Redis 队列。

每个用户的本地 MailAgent 实例通过 `BLPOP` 消费自己的队列（按 `database_id` 隔离）。

## 认证

所有写接口和查询接口均需认证（`/health` 除外），支持两种方式：

- Header: `X-Webhook-Token: <WEBHOOK_SECRET>`
- Header: `Authorization: Bearer <WEBHOOK_SECRET>`

## 架构流程

```
Openclaw / 外部系统
    │ POST /api/command
    ▼
Webhook Server (FastAPI) ──→ Redis 队列 ──→ 本地 MailAgent
    ▲                                           │
    │ GET /api/command/{id}/result               │ SET result
    └────────────────────────────────────────────┘
```
"""

tags_metadata = [
    {"name": "指令 API", "description": "外部系统（Openclaw 等）调用的指令接口，支持发送指令和查询执行结果"},
    {"name": "Notion Webhook", "description": "接收 Notion Automation 的 webhook 回调"},
    {"name": "运维", "description": "健康检查和队列统计"},
]

app = FastAPI(
    title="MailAgent Webhook Server",
    description=APP_DESCRIPTION,
    version="1.2.0",
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)


# ── Pydantic Models ──────────────────────────────────────────────────────

class CommandRequest(BaseModel):
    """发送指令请求体"""
    model_config = {"extra": "allow"}

    database_id: str = Field(
        ...,
        description="Notion 数据库 ID（支持带/不带连字符）",
        examples=["2df15375830d8094bf5ce86930c89843"],
    )
    command: str = Field(
        ...,
        description=(
            "指令类型：\n"
            "- `create_draft` — 创建 Mail.app 回复草稿\n"
            "- `flag_changed` — 同步旗标/已读状态到 Mail.app\n"
            "- `ai_reviewed` — AI 审核完成，触发飞书通知 + Mail.app 标旗\n"
            "- `completed` — 标记已完成，移除 Mail.app 旗标"
        ),
        examples=["create_draft"],
    )
    page_id: Optional[str] = Field(
        "",
        description="Notion 页面 ID，执行完成后用于更新 Processing Status",
        examples=["31b15375-830d-8102-afe4-cd7693979fc5"],
    )
    message_id: Optional[str] = Field(
        "",
        description="RFC 2822 Message-ID，用于在 SyncStore 中查找 Mail.app internal_id",
        examples=["MWHPR05MB3390A1B2C3@namprd05.prod.outlook.com"],
    )
    reply_suggestion: Optional[str] = Field(
        "",
        description=(
            "回复正文（`create_draft` 必填）。支持 Markdown 富文本格式，"
            "自动转为 HTML 粘贴到 Mail.app。支持：**加粗**、*斜体*、"
            "`行内代码`、列表、引用、表格"
        ),
        examples=["Hi Neil,\n\nThank you for the feedback.\n\n**Key points:**\n- Issue will be fixed\n- ETA: next sprint"],
    )
    mailbox: Optional[str] = Field(
        "收件箱",
        description="邮箱名称：`收件箱` 或 `发件箱`",
        examples=["收件箱"],
    )
    mode: Optional[str] = Field(
        "reply-all",
        description=(
            "草稿模式（仅 `create_draft` 有效）：\n"
            "- `reply-all` — 回复所有人，保留完整线程历史（默认）\n"
            "- `reply` — 仅回复发件人，保留线程历史\n"
            "- `new` — 新建独立邮件（需同时传 `to` 和 `subject`）"
        ),
        examples=["reply-all"],
    )
    extra_to: Optional[str] = Field(
        "",
        description="额外收件人（逗号分隔）。reply 模式追加到 To 列表，new 模式追加到收件人",
        examples=["alice@tp-link.com,bob@tp-link.com"],
    )
    extra_cc: Optional[str] = Field(
        "",
        description="额外抄送（逗号分隔）。自动过滤掉自己的邮箱地址",
        examples=["manager@tp-link.com"],
    )
    to: Optional[str] = Field(
        "",
        description="收件人邮箱（`new` 模式必填）",
        examples=["neil.mabini@tp-link.com"],
    )
    subject: Optional[str] = Field(
        "",
        description="邮件主题（`new` 模式必填）",
        examples=["MAC Group Follow-up"],
    )
    is_read: Optional[bool] = Field(
        None,
        description="已读状态（仅 `flag_changed` 有效）",
    )
    is_flagged: Optional[bool] = Field(
        None,
        description="旗标状态（仅 `flag_changed` 有效）",
    )


class CommandResponse(BaseModel):
    """发送指令响应"""
    ok: bool = Field(True, description="是否成功推入队列")
    queue: str = Field(
        ...,
        description="Redis 队列名，格式 `mailagent:{database_id}:events`",
        examples=["mailagent:2df15375830d8094bf5ce86930c89843:events"],
    )
    event_id: str = Field(
        ...,
        description="指令唯一 ID，用于调用 `/api/command/{event_id}/result` 查询执行结果",
        examples=["cmd_1772909109795_54026fc6"],
    )


class CommandResultResponse(BaseModel):
    """指令执行结果"""
    status: str = Field(
        ...,
        description=(
            "执行状态：\n"
            "- `pending` — 尚未执行完成（本地 MailAgent 还未消费该指令）\n"
            "- `success` — 执行成功\n"
            "- `error` — 执行失败"
        ),
        examples=["success"],
    )
    success: Optional[bool] = Field(
        None,
        description="脚本执行是否成功（仅 `create_draft` 有此字段）",
    )
    method: Optional[str] = Field(
        None,
        description=(
            "草稿创建方式（仅 `create_draft` 成功时有此字段）：\n"
            "- `reply_all_internal_id` — Reply All，通过 internal_id 定位（快速 ~1s）\n"
            "- `reply_all_message_id` — Reply All，fallback 到 message_id（慢 ~100s）\n"
            "- `reply_internal_id` — Reply，通过 internal_id 定位\n"
            "- `reply_message_id` — Reply，fallback 到 message_id\n"
            "- `new` — 新建模式\n"
            "- `standalone_fallback` — 回复模式找不到原始邮件，降级为新建"
        ),
        examples=["reply_all_internal_id"],
    )
    screenshot_path: Optional[str] = Field(
        None,
        description="截图文件路径（仅使用 `--screenshot` 参数时返回）",
    )
    error: Optional[str] = Field(
        None,
        description="错误信息（仅 status=error 时有此字段）",
    )
    note: Optional[str] = Field(
        None,
        description="附加说明（如 standalone_fallback 的降级说明）",
    )


class WebhookResponse(BaseModel):
    """Webhook 响应"""
    ok: bool = Field(True, description="是否成功入队")
    queue: str = Field(..., description="Redis 队列名")
    event_id: str = Field(..., description="事件 ID")


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = Field("ok", examples=["ok"])
    redis: str = Field("connected", examples=["connected"])


class QueueInfo(BaseModel):
    queue: str = Field(..., description="队列名")
    pending: int = Field(..., description="待处理消息数")


class StatsResponse(BaseModel):
    """队列统计响应"""
    queues: Dict[str, QueueInfo] = Field(..., description="各 database_id 的队列状态")
    total_queues: int = Field(..., description="活跃队列总数")


# ── Helper Functions ─────────────────────────────────────────────────────

def _extract_text(prop: Dict) -> str:
    for key in ("title", "rich_text"):
        items = prop.get(key, [])
        if items:
            return "".join(item.get("text", {}).get("content", "") for item in items)
    return ""


def _extract_rich_text(prop: Dict) -> str:
    """Extract rich text preserving formatting as Markdown (Feishu compatible)."""
    _feishu_colors = {"gray": "grey", "red": "red", "blue": "blue", "green": "green"}
    for key in ("title", "rich_text"):
        items = prop.get(key, [])
        if not items:
            continue
        parts = []
        for item in items:
            item_type = item.get("type", "text")
            # Equation
            if item_type == "equation":
                expr = item.get("equation", {}).get("expression", "")
                if expr:
                    parts.append(f"`{expr}`")
                continue
            text = item.get("text", {}).get("content", "")
            if not text:
                continue
            ann = item.get("annotations", {})
            link = item.get("text", {}).get("link")
            if ann.get("code"):
                if "\n" in text:
                    parts.append(f"\n```\n{text}\n```\n")
                    continue
                else:
                    text = f"`{text}`"
            else:
                if ann.get("bold") and ann.get("italic"):
                    text = f"***{text}***"
                elif ann.get("bold"):
                    text = f"**{text}**"
                elif ann.get("italic"):
                    text = f"*{text}*"
                if ann.get("strikethrough"):
                    text = f"~~{text}~~"
                if ann.get("underline"):
                    text = f"<u>{text}</u>"
            if link and link.get("url"):
                text = f"[{text}]({link['url']})"
            color = ann.get("color", "default")
            if color != "default" and not color.endswith("_background"):
                fc = _feishu_colors.get(color)
                if fc:
                    text = f"<font color='{fc}'>{text}</font>"
            parts.append(text)
        return "".join(parts)
    return ""


def _extract_select(prop: Dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _extract_checkbox(prop: Dict) -> bool:
    return prop.get("checkbox", False)


def _extract_email(prop: Dict) -> str:
    return prop.get("email", "") or ""


def _extract_date(prop: Dict) -> str:
    d = prop.get("date")
    return d.get("start", "") if d else ""


def _extract_raw_rich_text(prop: Dict) -> list:
    """Extract raw Notion rich_text items for local HTML conversion."""
    for key in ("title", "rich_text"):
        items = prop.get(key, [])
        if items:
            return items
    return []


def parse_properties(raw_props: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    field_map = {
        "Message ID": ("message_id", _extract_text),
        "Subject": ("subject", _extract_text),
        "From Name": ("from_name", _extract_text),
        "From": ("from_email", _extract_email),
        "To": ("to_addr", _extract_text),
        "CC": ("cc_addr", _extract_text),
        "Date": ("date", _extract_date),
        "Is Read": ("is_read", _extract_checkbox),
        "Is Flagged": ("is_flagged", _extract_checkbox),
        "Action Type": ("ai_action", _extract_select),
        "Priority": ("ai_priority", _extract_select),
        "Processing Status": ("ai_review_status", _extract_select),
        "Mailbox": ("mailbox", _extract_select),
        "Category": ("category", _extract_select),
        "AI Summary": ("ai_summary", _extract_rich_text),
        "Reply Suggestion": ("reply_suggestion", _extract_rich_text),
    }
    for notion_key, (out_key, extractor) in field_map.items():
        prop = raw_props.get(notion_key)
        if prop is not None:
            result[out_key] = extractor(prop)
    # Pass raw rich_text blocks for local HTML conversion
    for notion_key, out_key in [("Reply Suggestion", "reply_suggestion_rich"), ("AI Summary", "ai_summary_rich")]:
        prop = raw_props.get(notion_key)
        if prop is not None:
            raw_items = _extract_raw_rich_text(prop)
            if raw_items:
                result[out_key] = raw_items
    return result
    return result


def _check_auth(request: Request):
    if not WEBHOOK_SECRET:
        return
    auth = request.headers.get("Authorization", "")
    token = request.headers.get("X-Webhook-Token", "")
    if auth != f"Bearer {WEBHOOK_SECRET}" and token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── API Endpoints ────────────────────────────────────────────────────────

@app.post(
    "/api/command",
    response_model=CommandResponse,
    tags=["指令 API"],
    summary="发送指令",
)
async def handle_command(body: CommandRequest, request: Request):
    """发送指令到本地 MailAgent 执行。

    指令推入 Redis 队列，由对应 `database_id` 的 MailAgent 实例消费执行。
    返回 `event_id` 用于查询执行结果。

    ## 支持的指令类型

    | command | 说明 | 必需字段 |
    |---------|------|---------|
    | `create_draft` | 创建 Mail.app 回复草稿 | `reply_suggestion` |
    | `flag_changed` | 同步旗标/已读状态到 Mail.app | `message_id` + `is_read`/`is_flagged` |
    | `ai_reviewed` | AI 审核完成 → 飞书通知 + 标旗 | `message_id` + `ai_action` + `ai_priority` |
    | `completed` | 标记已完成 → 移除 Mail.app 旗标 | `message_id` |

    ## 完整调用流程

    ```
    Step 1: POST /api/command → 获取 event_id
    Step 2: GET  /api/command/{event_id}/result?wait=30 → 等待执行结果
    ```

    ## 富文本支持

    `reply_suggestion` 支持 Markdown 格式，自动转为 HTML 粘贴到 Mail.app：

    - **加粗** (`**text**`)、*斜体* (`*text*`)、`行内代码`
    - 无序列表 (`- item`)
    - 引用块 (`> quote`)
    - 表格 (`| A | B |`)
    """
    _check_auth(request)

    database_id = body.database_id.replace("-", "")
    command = body.command

    if not database_id or not command:
        raise HTTPException(status_code=400, detail="Missing database_id or command")

    body_dict = body.model_dump(exclude_none=True)
    meta_keys = {"database_id", "command", "page_id"}
    properties = {k: v for k, v in body_dict.items() if k not in meta_keys}

    message = {
        "id": f"cmd_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "type": command,
        "database_id": database_id,
        "page_id": body.page_id or "",
        "properties": properties,
        "timestamp": int(time.time() * 1000),
    }

    queue_key = f"{QUEUE_PREFIX}:{database_id}:events"
    await redis_pool.lpush(queue_key, json.dumps(message))
    await redis_pool.expire(queue_key, QUEUE_TTL_DAYS * 86400)

    return CommandResponse(ok=True, queue=queue_key, event_id=message["id"])


@app.get(
    "/api/command/{event_id}/result",
    response_model=CommandResultResponse,
    tags=["指令 API"],
    summary="查询指令执行结果",
)
async def get_command_result(
    request: Request,
    event_id: str = Path(..., description="POST /api/command 返回的 event_id"),
    wait: int = Query(
        default=0, ge=0, le=60,
        description=(
            "长轮询等待秒数（0-60）。\n"
            "- `0`：立即返回当前状态\n"
            "- `30`（推荐）：等待最多 30 秒，草稿创建通常 5-10 秒完成\n"
            "- 服务器每秒检查一次 Redis"
        ),
    ),
):
    """查询指令的执行结果。

    本地 MailAgent 执行完成后将结果写入 Redis（TTL 1 小时），通过此端点查询。

    ## 返回状态

    | status | 说明 |
    |--------|------|
    | `pending` | 尚未执行完成（本地 MailAgent 还未消费或正在执行） |
    | `success` | 执行成功，附带 `method` 等详细信息 |
    | `error` | 执行失败，附带 `error` 错误描述 |

    ## `method` 值说明（`create_draft` 成功时）

    | method | 含义 | 耗时 |
    |--------|------|------|
    | `reply_all_internal_id` | Reply All，通过 internal_id 定位 | ~1s |
    | `reply_all_message_id` | Reply All，fallback 到 message_id | ~100s |
    | `reply_internal_id` | Reply，通过 internal_id 定位 | ~1s |
    | `reply_message_id` | Reply，fallback 到 message_id | ~100s |
    | `new` | 新建模式 | ~3s |
    | `standalone_fallback` | 回复模式找不到原始邮件，降级为新建 | ~3s |

    ## 注意事项

    - 结果在 Redis 中保留 **1 小时**（TTL 3600s），过期后返回 `pending`
    - 建议 `wait=30`，草稿创建通常 5-10 秒完成
    - 如果不需要结果，可以 fire-and-forget（只调用 POST，不查询 result）
    """
    _check_auth(request)

    key = f"mailagent:results:{event_id}"
    raw = await redis_pool.get(key)
    if raw:
        return json.loads(raw)
    if wait <= 0:
        return CommandResultResponse(status="pending")

    for _ in range(wait):
        await asyncio.sleep(1)
        raw = await redis_pool.get(key)
        if raw:
            return json.loads(raw)
    return CommandResultResponse(status="pending")


@app.post(
    "/webhook/notion",
    response_model=WebhookResponse,
    tags=["Notion Webhook"],
    summary="接收 Notion Automation webhook",
)
async def handle_notion_webhook(
    request: Request,
    event: str = Query(
        default="page_updated",
        description=(
            "事件类型（由 Notion Automation URL 参数指定）：\n"
            "- `page_updated` — 通用更新（默认，自动路由）\n"
            "- `flag_changed` — Is Read / Is Flagged 变化\n"
            "- `ai_reviewed` — Processing Status → AI Reviewed\n"
            "- `completed` — Processing Status → 已完成\n"
            "- `create_draft` — 创建草稿按钮触发"
        ),
    ),
):
    """接收 Notion Automation 的 webhook 回调。

    Notion Automation \"Send Webhook\" action 发送页面数据到此端点，
    服务器自动解析 Notion properties 并路由到对应用户的 Redis 队列。

    ## 与 `/api/command` 的区别

    | 特性 | `/api/command` | `/webhook/notion` |
    |------|---------------|-------------------|
    | 调用方 | Openclaw / 外部系统 | Notion Automation |
    | 请求体 | Flat JSON | Notion 原始页面对象 |
    | 字段解析 | 直接透传 | 自动从 Notion properties 提取 |
    | 事件类型 | `command` 字段 | `?event=` Query 参数 |
    | 结果回传 | 支持 | 不支持 |

    ## Notion Automation 配置

    - 触发条件：Processing Status 变化、Button 点击等
    - Action：Send Webhook → `https://mailagent.chenge.ink/webhook/notion?event=ai_reviewed`
    """
    _check_auth(request)

    body = await request.json()

    data = body.get("data", body)
    if isinstance(data, dict) and "object" not in data and "id" not in data:
        data = body

    parent = data.get("parent", {})
    database_id = parent.get("database_id", "").replace("-", "")

    if not database_id:
        raise HTTPException(status_code=400, detail="No database_id in payload")

    page_id = data.get("id", "")
    raw_props = data.get("properties", {})
    properties = parse_properties(raw_props)

    message = {
        "id": f"evt_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "type": event,
        "database_id": database_id,
        "page_id": page_id,
        "properties": properties,
        "timestamp": int(time.time() * 1000),
    }

    queue_key = f"{QUEUE_PREFIX}:{database_id}:events"
    await redis_pool.lpush(queue_key, json.dumps(message))
    await redis_pool.expire(queue_key, QUEUE_TTL_DAYS * 86400)

    return WebhookResponse(ok=True, queue=queue_key, event_id=message["id"])


@app.get("/copy", response_class=HTMLResponse, include_in_schema=False)
async def copy_info(d: str = Query(..., description="Base64 encoded JSON")):
    try:
        raw = base64.urlsafe_b64decode(d).decode("utf-8")
        data = json.loads(raw)
        formatted = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        formatted = raw if 'raw' in dir() else "Invalid data"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>邮件信息</title>
<style>
body{{font-family:system-ui;max-width:600px;margin:40px auto;padding:0 20px;background:#f5f5f5}}
pre{{background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5}}
button{{background:#4f46e5;color:#fff;border:none;padding:12px 24px;border-radius:6px;font-size:16px;cursor:pointer;width:100%;margin-top:12px}}
button:active{{background:#4338ca}}
.ok{{background:#16a34a}}
</style></head><body>
<pre id="info">{formatted}</pre>
<button onclick="copyInfo()">一键复制</button>
<script>
function copyInfo(){{
  const text=document.getElementById('info').textContent;
  navigator.clipboard.writeText(text).then(()=>{{
    const btn=document.querySelector('button');
    btn.textContent='已复制 ✓';btn.classList.add('ok');
    setTimeout(()=>{{btn.textContent='一键复制';btn.classList.remove('ok')}},2000);
  }});
}}
</script></body></html>"""


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
async def health():
    """检查服务和 Redis 连接状态。无需认证。"""
    try:
        await redis_pool.ping()
        return HealthResponse(status="ok", redis="connected")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis: {e}")


@app.get(
    "/admin/stats",
    response_model=StatsResponse,
    tags=["运维"],
    summary="队列统计",
)
async def admin_stats(request: Request):
    """查看各 database_id 的队列待处理消息数。需要认证。"""
    _check_auth(request)

    keys = []
    async for key in redis_pool.scan_iter(f"{QUEUE_PREFIX}:*:events"):
        keys.append(key)

    stats = {}
    for key in keys:
        db_id = key.split(":")[1]
        length = await redis_pool.llen(key)
        stats[db_id] = QueueInfo(queue=key, pending=length)

    return StatsResponse(queues=stats, total_queues=len(stats))
