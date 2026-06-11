"""Enriched email view endpoints (src/api/routers/email_views.py).

Exercises the 6 enriched read endpoints against the shared temp-DB fixture
(tests/api/conftest.py). That fixture's DDL deliberately OMITS the v13/v14
migration columns (ai_priority / ai_action / processing_status) and the
llm_processing table — so these tests double as the **graceful-degradation**
contract (gotcha #4): the endpoints must still return correct metadata with AI
fields degraded to null, never 500 on schema drift.

Wire shapes mirror the Electron main handler (handlers/email.ts) so the web
HttpApi can map 1:1 onto EnrichedEmailMeta / MailboxSummary / AIFields.
"""

from __future__ import annotations

from tests.api.conftest import EMAIL_ID, EMAIL_NO_BODY_ID


def _ok_envelope(payload: dict) -> None:
    assert payload["status"] == "success"
    assert payload["schema_version"] == 1
    assert payload["error"] is None
    assert payload["meta"]["source"] == "sqlite"
    assert payload["meta"]["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# GET /api/email/list-enriched
# ---------------------------------------------------------------------------


def test_list_enriched_shape_and_degraded_ai(client):
    r = client.get("/api/email/list-enriched")
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    assert isinstance(data, list)
    ids = {row["internal_id"] for row in data}
    # Both seeded emails present (neither is 'skipped').
    assert {EMAIL_ID, EMAIL_NO_BODY_ID}.issubset(ids)

    item = next(row for row in data if row["internal_id"] == EMAIL_ID)
    # EnrichedEmailMeta = list-item + enriched extras.
    for key in (
        "snippet", "has_body", "lang", "ai_priority", "ai_action",
        "ai_category", "attach_count", "is_important", "processing_status",
        "notion_url", "subject", "sender", "is_read", "is_flagged",
    ):
        assert key in item, f"missing enriched key {key!r}"
    # Sprint 19 — snippet always lazy-null on the list query.
    assert item["snippet"] is None
    # EMAIL_ID has a body row → has_body true; EMAIL_NO_BODY_ID has none.
    assert item["has_body"] is True
    no_body = next(row for row in data if row["internal_id"] == EMAIL_NO_BODY_ID)
    assert no_body["has_body"] is False
    # attach_count excludes inline (all 3 fixture attachments are is_inline=0).
    assert item["attach_count"] == 3
    # is_important promoted from the column.
    assert item["is_important"] is True
    # Degraded AI schema (no ai_priority col / no llm_processing) → null fields,
    # 'unknown' lang. Must NOT 500.
    assert item["ai_priority"] is None
    assert item["ai_action"] is None
    assert item["ai_category"] is None
    assert item["lang"] == "unknown"
    assert item["processing_status"] is None

    meta = body["meta"]
    assert meta["count"] == len(data)
    assert meta["limit"] == 100
    assert meta["offset"] == 0


def test_list_enriched_filter_mailbox_and_flag(client):
    r = client.get(
        "/api/email/list-enriched",
        params={"mailbox": "收件箱", "isFlagged": "true"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # Only EMAIL_ID is flagged.
    assert [row["internal_id"] for row in data] == [EMAIL_ID]


def test_list_enriched_internalids_whitelist(client):
    r = client.get(
        "/api/email/list-enriched",
        params={"internalIds": f"{EMAIL_NO_BODY_ID}"},
    )
    assert r.status_code == 200
    ids = [row["internal_id"] for row in r.json()["data"]]
    assert ids == [EMAIL_NO_BODY_ID]


def test_list_enriched_bad_internalids_400(client):
    r = client.get("/api/email/list-enriched", params={"internalIds": "abc,def"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_INVALID_ARG"


def test_list_enriched_limit_out_of_range_422(client):
    r = client.get("/api/email/list-enriched", params={"limit": 99999})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/email/mailboxes
# ---------------------------------------------------------------------------


def test_list_mailboxes(client):
    r = client.get("/api/email/mailboxes")
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    assert isinstance(data, list)
    inbox = next(m for m in data if m["mailbox"] == "收件箱")
    assert set(inbox) == {"mailbox", "total", "unread", "flagged", "failed", "attention"}
    # 2 emails in 收件箱; EMAIL_NO_BODY is read, EMAIL_ID unread+flagged.
    assert inbox["total"] == 2
    assert inbox["unread"] == 1
    assert inbox["flagged"] == 1
    # Degraded schema (无 ai_priority/ai_action) → 只剩置顶路径; 基础语料无置顶。
    assert inbox["attention"] == 0
    assert body["meta"]["count"] == len(data)


# ---------------------------------------------------------------------------
# 「需关注」(attention) — list-enriched?attention=true + mailboxes attention 计数
#
# 共享 temp-DB 是降级 schema (无 ai_priority/ai_action/processing_status/
# llm_processing) → 紧急×需动作路径恒假, 只能走置顶路径; 完整判定 (优先级/动作/
# 已完成排除/labels fallback) 的单测在 Electron 侧
# frontend/tests/main/handlers/email.test.ts (全量 fixture schema)。
# ---------------------------------------------------------------------------


def test_attention_pinned_replied_and_sent(client, temp_db):
    import sqlite3

    conn = sqlite3.connect(str(temp_db))
    try:
        # 9001: 置顶收件 → 进; 9002: 置顶但同 thread 有更晚发件箱邮件 (9003) → 已回复排除;
        # 9003: 发件箱本体 → 排除。
        conn.executescript(
            """
            INSERT INTO email_metadata
              (internal_id, message_id, thread_id, subject, sender, date_received,
               mailbox, sync_status, is_pinned)
            VALUES
              (9001, '<att-9001>', 'thread-att-1', 'pinned todo', 'p@example.com',
               '2026-06-01 10:00:00', '收件箱', 'synced', 1),
              (9002, '<att-9002>', 'thread-att-2', 'pinned but replied', 'q@example.com',
               '2026-06-01 11:00:00', '收件箱', 'synced', 1),
              (9003, '<att-9003>', 'thread-att-2', 'my reply', 'me@example.com',
               '2026-06-02 09:00:00', '发件箱', 'synced', 0);
            """
        )
        conn.commit()

        r = client.get("/api/email/list-enriched", params={"attention": "true"})
        assert r.status_code == 200
        ids = {row["internal_id"] for row in r.json()["data"]}
        assert 9001 in ids
        assert 9002 not in ids  # 已回复 (同 thread 更晚发件箱邮件), 置顶也排除
        assert 9003 not in ids  # 发件箱
        assert EMAIL_ID not in ids  # 未置顶 + 降级 schema 无优先级路径

        # mailboxes attention 计数与列表同判定。
        r2 = client.get("/api/email/mailboxes")
        inbox = next(m for m in r2.json()["data"] if m["mailbox"] == "收件箱")
        assert inbox["attention"] == 1
        sent = next(m for m in r2.json()["data"] if m["mailbox"] == "发件箱")
        assert sent["attention"] == 0
    finally:
        conn.execute("DELETE FROM email_metadata WHERE internal_id >= 9001")
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/email/thread/{thread_id}
# ---------------------------------------------------------------------------


def test_list_by_thread(client):
    # EMAIL_ID is on thread-A in the fixture.
    r = client.get("/api/email/thread/thread-A")
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    assert [row["internal_id"] for row in data] == [EMAIL_ID]
    # list-item shape (no to_addr).
    assert "to_addr" not in data[0]
    assert "notion_url" in data[0]


def test_list_by_thread_unknown_empty(client):
    r = client.get("/api/email/thread/nonexistent-thread")
    assert r.status_code == 200
    assert r.json()["data"] == []


# ---------------------------------------------------------------------------
# POST /api/email/threads
# ---------------------------------------------------------------------------


def test_list_by_threads_batch(client):
    r = client.post(
        "/api/email/threads", json={"threadIds": ["thread-A", "ghost", "thread-A"]}
    )
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    # Map keyed by thread_id; only thread-A has rows.
    assert set(data.keys()) == {"thread-A"}
    assert [row["internal_id"] for row in data["thread-A"]] == [EMAIL_ID]


def test_list_by_threads_empty_input(client):
    r = client.post("/api/email/threads", json={"threadIds": []})
    assert r.status_code == 200
    assert r.json()["data"] == {}
    # Missing key → also {}.
    r2 = client.post("/api/email/threads", json={})
    assert r2.status_code == 200
    assert r2.json()["data"] == {}


# ---------------------------------------------------------------------------
# POST /api/email/snippets
# ---------------------------------------------------------------------------


def test_list_snippets(client):
    r = client.post(
        "/api/email/snippets",
        json={"internalIds": [EMAIL_ID, EMAIL_NO_BODY_ID]},
    )
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    # Map keyed by stringified internal_id (JSON object keys are strings).
    assert str(EMAIL_ID) in data
    assert data[str(EMAIL_ID)].startswith("Hello **redis**")
    # EMAIL_NO_BODY_ID has no body row → absent from map.
    assert str(EMAIL_NO_BODY_ID) not in data


def test_list_snippets_empty_input(client):
    r = client.post("/api/email/snippets", json={"internalIds": []})
    assert r.status_code == 200
    assert r.json()["data"] == {}


# ---------------------------------------------------------------------------
# POST /api/email/ai-fields
# ---------------------------------------------------------------------------


def test_ai_fields_degraded(client):
    r = client.post(
        "/api/email/ai-fields",
        json={"internalIds": [EMAIL_ID, EMAIL_NO_BODY_ID]},
    )
    assert r.status_code == 200
    body = r.json()
    _ok_envelope(body)
    data = body["data"]
    # Both emails exist in metadata → both present, keyed by string id.
    assert {str(EMAIL_ID), str(EMAIL_NO_BODY_ID)}.issubset(data.keys())
    af = data[str(EMAIL_ID)]
    assert set(af) == {
        "internal_id", "processing_status", "mailbox", "is_read", "is_flagged",
        "ai_priority", "ai_action", "ai_review_status", "sentiment",
        "ai_model", "labels_raw",
    }
    assert af["mailbox"] == "收件箱"
    assert af["is_flagged"] is True
    # No llm_processing table / no processing_status col in fixture → degraded.
    assert af["ai_priority"] is None
    assert af["ai_review_status"] is None
    assert af["ai_model"] is None
    assert af["labels_raw"] is None
    assert af["processing_status"] is None


def test_ai_fields_unknown_id_absent(client):
    r = client.post("/api/email/ai-fields", json={"internalIds": [424242]})
    assert r.status_code == 200
    assert r.json()["data"] == {}


def test_ai_fields_empty_input(client):
    r = client.post("/api/email/ai-fields", json={"internalIds": []})
    assert r.status_code == 200
    assert r.json()["data"] == {}


# ---------------------------------------------------------------------------
# C10 — batch size cap on IN(...) endpoints (threads / snippets / ai-fields /
# list-enriched internalIds). Over BATCH_IDS_MAX → 400 E_INVALID_ARG, before SQL.
# ---------------------------------------------------------------------------

from src.api.routers.email_views import BATCH_IDS_MAX  # noqa: E402

_OVER = BATCH_IDS_MAX + 1


def test_snippets_oversized_batch_400(client):
    r = client.post(
        "/api/email/snippets", json={"internalIds": list(range(_OVER))}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_INVALID_ARG"


def test_ai_fields_oversized_batch_400(client):
    r = client.post(
        "/api/email/ai-fields", json={"internalIds": list(range(_OVER))}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_INVALID_ARG"


def test_threads_oversized_batch_400(client):
    r = client.post(
        "/api/email/threads",
        json={"threadIds": [f"t-{i}" for i in range(_OVER)]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_INVALID_ARG"


def test_list_enriched_oversized_internalids_400(client):
    ids = ",".join(str(i) for i in range(_OVER))
    r = client.get("/api/email/list-enriched", params={"internalIds": ids})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_INVALID_ARG"


def test_snippets_at_cap_boundary_ok(client):
    # Exactly BATCH_IDS_MAX ids is allowed (none seeded → empty map, but 200).
    r = client.post(
        "/api/email/snippets", json={"internalIds": list(range(BATCH_IDS_MAX))}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_threads_dedupe_under_cap_ok(client):
    # C10 caps the DE-DUPED count: a body with > cap raw ids that collapses to
    # a handful of distinct values must pass (the IN(...) list is what matters).
    raw = ["thread-A"] * _OVER
    r = client.post("/api/email/threads", json={"threadIds": raw})
    assert r.status_code == 200
    assert set(r.json()["data"].keys()) == {"thread-A"}
