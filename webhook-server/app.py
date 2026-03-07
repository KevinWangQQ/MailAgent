"""
MailAgent Webhook Server

接收 Notion Automation webhook，按 database_id 路由到 Redis 队列。
每个用户的 MailAgent 实例 BLPOP 自己的队列。

部署: uvicorn app:app --host 0.0.0.0 --port 8100
"""

import json
import os
import time
import uuid
import base64
from typing import Any, Dict, Optional

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

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


app = FastAPI(title="MailAgent Webhook Server", lifespan=lifespan)


def _extract_text(prop: Dict) -> str:
    """Extract text from Notion property (title or rich_text)."""
    for key in ("title", "rich_text"):
        items = prop.get(key, [])
        if items:
            return items[0].get("text", {}).get("content", "")
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


def parse_properties(raw_props: Dict[str, Any]) -> Dict[str, Any]:
    """Parse Notion properties into flat dict for queue message."""
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
        "AI Summary": ("ai_summary", _extract_text),
        "Reply Suggestion": ("reply_suggestion", _extract_text),
    }

    for notion_key, (out_key, extractor) in field_map.items():
        prop = raw_props.get(notion_key)
        if prop is not None:
            result[out_key] = extractor(prop)

    return result


@app.post("/webhook/notion")
async def handle_notion_webhook(
    request: Request,
    event: str = Query(default="page_updated", description="Event type hint from Notion Automation")
):
    """接收 Notion Automation webhook

    Notion Automation "Send Webhook" action 会 POST 页面数据到此端点。
    通过 ?event=flag_changed 或 ?event=ai_reviewed 区分触发源。
    """
    # Auth check
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        token = request.headers.get("X-Webhook-Token", "")
        if auth != f"Bearer {WEBHOOK_SECRET}" and token != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()

    # Handle both raw page object and wrapped {source, data} format
    data = body.get("data", body)
    if isinstance(data, dict) and "object" not in data and "id" not in data:
        data = body

    # Extract database_id
    parent = data.get("parent", {})
    database_id = parent.get("database_id", "").replace("-", "")

    if not database_id:
        raise HTTPException(status_code=400, detail="No database_id in payload")

    page_id = data.get("id", "")
    raw_props = data.get("properties", {})
    properties = parse_properties(raw_props)

    # Build queue message
    message = {
        "id": f"evt_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "type": event,
        "database_id": database_id,
        "page_id": page_id,
        "properties": properties,
        "timestamp": int(time.time() * 1000),
    }

    # Route to per-user queue
    queue_key = f"{QUEUE_PREFIX}:{database_id}:events"
    await redis_pool.lpush(queue_key, json.dumps(message))

    # Set queue expiry to prevent abandoned queues from piling up
    await redis_pool.expire(queue_key, QUEUE_TTL_DAYS * 86400)

    return {"ok": True, "queue": queue_key, "event_id": message["id"]}


@app.get("/copy", response_class=HTMLResponse)
async def copy_info(d: str = Query(..., description="Base64 encoded JSON")):
    """一键复制邮件信息页面"""
    try:
        raw = base64.urlsafe_b64decode(d).decode("utf-8")
        # 格式化 JSON 方便阅读
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


@app.get("/health")
async def health():
    try:
        await redis_pool.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis: {e}")


@app.get("/admin/stats")
async def admin_stats(request: Request):
    """Queue stats per database_id."""
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Webhook-Token", "")
        if token != WEBHOOK_SECRET:
            raise HTTPException(status_code=401)

    keys = []
    async for key in redis_pool.scan_iter(f"{QUEUE_PREFIX}:*:events"):
        keys.append(key)

    stats = {}
    for key in keys:
        db_id = key.split(":")[1]
        length = await redis_pool.llen(key)
        stats[db_id] = {"queue": key, "pending": length}

    return {"queues": stats, "total_queues": len(stats)}
