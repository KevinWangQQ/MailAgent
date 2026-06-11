// Sprint 1.8 — handler unit tests + cli-schema shape validation.
//
// Two coverage layers:
//   1. Functional: each handler returns the expected rows under filters,
//      offsets, missing-internal-id, FTS5 query.
//   2. Schema: ajv against the same docs/cli-schema/*.schema.json that
//      generates the renderer types. If the backend bumps a schema and we
//      don't `pnpm gen:types`, this test fails loudly.

import { afterAll, beforeAll, describe, expect, test, vi } from 'vitest'
import Database from 'better-sqlite3'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

import Ajv2020, { type ValidateFunction } from 'ajv/dist/2020.js'

import { buildFixtureDb } from '../../fixtures/sync-store-fixture'

// vi.mock must be in module scope; we point the handler module at our fixture
// db instead of the production resolveDbPath() which would try to open
// ~/Documents/MailAgent/data/sync_store.db.
let fixtureDb: Database.Database
vi.mock('../../../src/electron/main/db', () => ({
  getDb: () => fixtureDb,
  closeDb: () => {},
  resolveDbPath: () => ':memory:'
}))

// Same here — handlers/email.ts calls ipcMain.handle() at module load via
// registerEmailHandlers, but we only need the pure DAO functions; stub
// ipcMain so importing the module doesn't crash outside Electron.
vi.mock('electron', () => ({
  ipcMain: { handle: vi.fn() }
}))

// Now import the module under test (after vi.mock declarations).
const handlers = await import('../../../src/electron/main/handlers/email')

// ---- schema validator setup -------------------------------------------------

const SCHEMA_DIR = resolve(__dirname, '../../../../docs/cli-schema')

function loadAjv(): Ajv2020 {
  const ajv = new Ajv2020({ allErrors: true, strict: false })
  for (const f of readdirSync(SCHEMA_DIR)) {
    if (!f.endsWith('.schema.json')) continue
    const raw = JSON.parse(readFileSync(resolve(SCHEMA_DIR, f), 'utf8'))
    ajv.addSchema(raw, f)
  }
  return ajv
}

function compileFor(ajv: Ajv2020, file: string): ValidateFunction {
  // Schemas were `addSchema(raw, file)`'d up-front so $ref to _common works;
  // recompiling here would double-register and throw. `getSchema(key)`
  // returns the lazily-compiled validator.
  const v = ajv.getSchema(file)
  if (!v) throw new Error(`schema ${file} not registered in ajv`)
  return v
}

function wrap(data: unknown, metaExtra: Record<string, unknown> = {}): unknown {
  // email-search.schema.json marks query/total_hits/limit as required on meta;
  // most other schemas only need duration_ms. Tests pass the per-schema extras.
  return {
    status: 'success',
    schema_version: 1,
    data,
    meta: { duration_ms: 0, ...metaExtra }
  }
}

// ---- tests ------------------------------------------------------------------

let ajv: Ajv2020
let validateList: ValidateFunction
let validateGet: ValidateFunction
let validateBody: ValidateFunction
let validateSearch: ValidateFunction
let validateAttachmentList: ValidateFunction

beforeAll(() => {
  fixtureDb = buildFixtureDb()
  ajv = loadAjv()
  validateList = compileFor(ajv, 'email-list.schema.json')
  validateGet = compileFor(ajv, 'email-get.schema.json')
  validateBody = compileFor(ajv, 'email-body.schema.json')
  validateSearch = compileFor(ajv, 'email-search.schema.json')
  validateAttachmentList = compileFor(ajv, 'attachment-list.schema.json')
})

afterAll(() => {
  fixtureDb?.close()
})

describe('listEmails', () => {
  test('returns rows ordered by date desc and is schema-valid', () => {
    const rows = handlers.listEmails({ limit: 10 })
    expect(rows).toHaveLength(4)
    expect(rows[0]?.internal_id).toBe(101) // most recent date
    expect(rows[1]?.internal_id).toBe(102)
    expect(rows[2]?.internal_id).toBe(103)
    expect(rows[3]?.internal_id).toBe(201)

    const wrapped = wrap(rows, {
      total: rows.length,
      limit: 10,
      offset: 0,
      count: rows.length
    })
    const ok = validateList(wrapped)
    if (!ok) console.error('list schema errors:', validateList.errors)
    expect(ok).toBe(true)
  })

  test('honours mailbox filter', () => {
    const rows = handlers.listEmails({ mailbox: '发件箱' })
    expect(rows).toHaveLength(1)
    expect(rows[0]?.sender).toBe('me@example.com')
  })

  test('honours status + isRead filters together', () => {
    const rows = handlers.listEmails({ status: 'synced', isRead: false })
    expect(rows.map((r) => r.internal_id)).toEqual([101])
  })

  test('limit clamps to ≥1', () => {
    const rows = handlers.listEmails({ limit: 0 })
    // clamp lower bound is 1 — at minimum we get the most recent row
    expect(rows.length).toBeGreaterThanOrEqual(1)
  })

  test('booleans round-trip correctly (is_read/is_flagged 0/1 → false/true)', () => {
    const [first] = handlers.listEmails({ mailbox: '收件箱', limit: 1 })
    expect(typeof first.is_read).toBe('boolean')
    expect(typeof first.is_flagged).toBe('boolean')
    expect(first.is_flagged).toBe(true) // internal_id 101 had is_flagged=1
  })

  test('notion_url shape', () => {
    const [first] = handlers.listEmails({ limit: 1 })
    expect(first.notion_url).toMatch(/^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/)
  })
})

describe('getEmail', () => {
  test('returns full record with body summary and attachments', () => {
    const rec = handlers.getEmail(101)
    expect(rec).not.toBeNull()
    expect(rec?.body?.format).toBe('html')
    expect(rec?.attachments).toHaveLength(2)
    const [, derived] = rec!.attachments!
    expect(derived.derived_from).not.toBeNull()
    expect(derived.derived_format).toBe('pdf')

    const ok = validateGet(wrap(rec))
    if (!ok) console.error('schema errors:', validateGet.errors)
    expect(ok).toBe(true)
  })

  test('returns null when internal_id missing', () => {
    expect(handlers.getEmail(99_999)).toBeNull()
  })

  test('body is null when email_body row missing (fetch_failed case)', () => {
    const rec = handlers.getEmail(103)
    expect(rec?.body).toBeNull()
    expect(rec?.attachments).toEqual([])
  })
})

describe('getEmailBody', () => {
  test('markdown format returns body_markdown content', () => {
    const body = handlers.getEmailBody(101, 'markdown')
    expect(body?.format).toBe('markdown')
    expect(body?.content).toContain('redis client')
    expect(validateBody(wrap(body))).toBe(true)
  })

  test('html format returns body_html content', () => {
    const body = handlers.getEmailBody(101, 'html')
    expect(body?.format).toBe('html')
    expect(body?.content).toContain('<p>')
  })

  test('raw format returns only the sha256 hash', () => {
    const body = handlers.getEmailBody(101, 'raw')
    expect(body?.format).toBe('raw')
    expect(body?.content).toBe('sha256-aaa-101')
  })

  test('missing internal_id returns null', () => {
    expect(handlers.getEmailBody(99_999, 'markdown')).toBeNull()
  })
})

describe('searchEmails (FTS5)', () => {
  // Sprint 16 — searchEmails 返回 SearchResult { items, total_indexed };
  // 之前直接返 SearchHit[]; 测试用 .items 拿数组保持原断言形状.
  test('English word hit', () => {
    const result = handlers.searchEmails({ query: 'redis', limit: 10 })
    const hits = result.items
    expect(hits).toHaveLength(1)
    expect(hits[0]?.internal_id).toBe(101)
    expect(hits[0]?.snippet ?? '').toContain('<mark>')
    const wrapped = wrap(hits, { query: 'redis', total_hits: hits.length, limit: 10 })
    const ok = validateSearch(wrapped)
    if (!ok) console.error('search schema errors:', validateSearch.errors)
    expect(ok).toBe(true)
  })

  test('CJK prefix-wildcard hit (DESIGN.md note: unicode61 needs * for plain CN)', () => {
    const hits = handlers.searchEmails({ query: '产品*', limit: 10 }).items
    expect(hits.length).toBeGreaterThanOrEqual(1)
    expect(hits[0]?.internal_id).toBe(102)
  })

  test('mailbox filter narrows results', () => {
    const all = handlers.searchEmails({ query: 'notion', limit: 10 }).items
    const sent = handlers.searchEmails({ query: 'notion', mailbox: '发件箱', limit: 10 }).items
    expect(sent.length).toBeLessThanOrEqual(all.length)
  })

  test('empty query short-circuits to {items:[], total_indexed:N}', () => {
    const r = handlers.searchEmails({ query: '   ', limit: 10 })
    expect(r.items).toEqual([])
    expect(typeof r.total_indexed).toBe('number')
  })

  test('rank is monotonically non-decreasing (smaller = better per bm25)', () => {
    const hits = handlers.searchEmails({ query: 'redis OR 产品*', limit: 10 }).items
    for (let i = 1; i < hits.length; i++) {
      expect(hits[i]!.rank).toBeGreaterThanOrEqual(hits[i - 1]!.rank)
    }
  })

  test('hits carry ai_priority + lang from llm_processing LEFT JOIN', () => {
    // Sprint 16 search-module — hits expose mapped priority + language so
    // the palette EmailHitRow can render priority chip + lang-pip without
    // a follow-up IPC. Email 101 in the fixture has "🔴 紧急" → critical
    // and English body. Email 102 has "🟡 重要" → important + Chinese.
    const r101 = handlers.searchEmails({ query: 'redis', limit: 5 }).items[0]
    expect(r101?.ai_priority).toBe('critical')
    expect(r101?.lang).toBe('en')

    const r102 = handlers.searchEmails({ query: '产品*', limit: 5 }).items[0]
    expect(r102?.ai_priority).toBe('important')
    expect(r102?.lang).toBe('zh')
  })

  test('total_indexed reflects email_body_fts row count and survives empty query', () => {
    // The fixture inserts body rows for emails 101 + 102 (103/201 are body-less).
    const blank = handlers.searchEmails({ query: '', limit: 0 })
    const withHits = handlers.searchEmails({ query: 'redis', limit: 5 })
    expect(blank.total_indexed).toBeGreaterThan(0)
    expect(withHits.total_indexed).toBe(blank.total_indexed)
  })
})

describe('attachment shape', () => {
  test('the attachments[] inside email:get matches attachment-list schema', () => {
    const rec = handlers.getEmail(101)!
    expect(validateAttachmentList(wrap(rec.attachments))).toBe(true)
  })
})

// ============================================================
// PR-2a: smartQueryTransform + searchEmails smart mode
// ============================================================
describe('smartQueryTransform (PR-2a)', () => {
  // 跟 src/repository/email_repository.py:smart_query_transform 算法对齐;
  // 改其中一边请同步另一边, 否则 chat tool 跟 CLI / webhook 行为分叉.
  const t = handlers.smartQueryTransform

  test('empty / whitespace returns as-is', () => {
    expect(t('')).toBe('')
    expect(t('   ')).toBe('   ')
  })

  test('single CJK char gets * prefix', () => {
    expect(t('产')).toBe('产*')
    expect(t('会')).toBe('会*')
  })

  test('multi-char CJK gets prefix-or-char-and fallback', () => {
    expect(t('产品')).toBe('(产品* OR (产* AND 品*))')
    expect(t('本周产品评审')).toBe(
      '(本周产品评审* OR (本* AND 周* AND 产* AND 品* AND 评* AND 审*))'
    )
  })

  test('pure latin token unchanged', () => {
    expect(t('redis')).toBe('redis')
  })

  test('multi latin tokens use AND', () => {
    expect(t('redis timeout')).toBe('redis AND timeout')
    expect(t('project plan review')).toBe('project AND plan AND review')
  })

  test('mixed latin and CJK tokens', () => {
    expect(t('redis 超时')).toBe('redis AND (超时* OR (超* AND 时*))')
  })

  test('mixed char within one token', () => {
    expect(t('Redis超时')).toBe('(Redis AND (超时* OR (超* AND 时*)))')
  })

  test('phrase with quotes returns raw', () => {
    expect(t('"redis timeout"')).toBe('"redis timeout"')
  })

  test('wildcard returns raw', () => {
    expect(t('redis*')).toBe('redis*')
    expect(t('产品*')).toBe('产品*')
  })

  test('explicit operators return raw', () => {
    expect(t('redis AND timeout')).toBe('redis AND timeout')
    expect(t('redis OR cache')).toBe('redis OR cache')
    expect(t('redis NOT timeout')).toBe('redis NOT timeout')
  })

  test('punctuation returns raw', () => {
    expect(t('redis-timeout')).toBe('redis-timeout')
    expect(t('user@example.com')).toBe('user@example.com')
    expect(t('(redis)')).toBe('(redis)')
    expect(t('body:redis')).toBe('body:redis')
  })

  test('hiragana treated as CJK', () => {
    const result = t('ひらがな')
    expect(result.startsWith('(ひらがな*')).toBe(true)
    expect(result).toContain('ひ*')
    expect(result).toContain('ら*')
  })

  test('hangul treated as CJK', () => {
    expect(t('안녕')).toBe('(안녕* OR (안* AND 녕*))')
  })

  test('whitespace normalized', () => {
    expect(t('  redis    timeout  ')).toBe('redis AND timeout')
  })
})

describe('searchEmails smart mode (PR-2a)', () => {
  test('smart mode default: natural CJK keyword goes through transform', () => {
    // 102 的 subject = '产品 OKR' (从 fixture seed) → '产品*' prefix match
    // smart '产品' → '(产品* OR (产* AND 品*))' → 主表 hit
    const r = handlers.searchEmails({ query: '产品' })
    expect(r.items.length).toBeGreaterThanOrEqual(1)
    expect(r.mode).toBe('smart')
    expect(r.transformed_query).toBe('(产品* OR (产* AND 品*))')
  })

  test('raw mode bypasses transform', () => {
    const r = handlers.searchEmails({ query: '产品', mode: 'raw' })
    expect(r.mode).toBe('raw')
    expect(r.transformed_query).toBeUndefined()
  })

  test('explicit FTS5 syntax passes through unchanged in smart mode', () => {
    const r = handlers.searchEmails({ query: '产品*' })
    expect(r.mode).toBe('smart')
    // 含 wildcard → smartQueryTransform 判 raw passthrough, transformed_query 不应设置
    expect(r.transformed_query).toBeUndefined()
  })

  test('smart mode equivalence with raw for plain latin', () => {
    const smart = handlers.searchEmails({ query: 'redis' })
    const raw = handlers.searchEmails({ query: 'redis', mode: 'raw' })
    expect(smart.items.map((i) => i.internal_id)).toEqual(raw.items.map((i) => i.internal_id))
    // 单 token latin → transform 不变化, 没 transformed_query
    expect(smart.transformed_query).toBeUndefined()
  })
})

// ---- Sprint 2 D0: enriched view IPCs -----------------------------------------

describe('listEmailsEnriched', () => {
  test('returns rows in date-desc order with body snippet + LLM labels + attach count', () => {
    const rows = handlers.listEmailsEnriched({ limit: 10 })
    expect(rows.map((r) => r.internal_id)).toEqual([101, 102, 103, 201])

    // 101 — has body + full LLM labels + 2 non-inline attachments (orig + derived)
    // Sprint 19: listEnriched 不再读 body blob → snippet 恒 null (懒取);
    // 内容断言迁到本 describe 末尾的 listEmailSnippets。
    expect(rows[0].snippet).toBeNull()
    expect(rows[0].lang).toBe('en')
    expect(rows[0].ai_priority).toBe('critical') // mapped from "🔴 紧急"
    expect(rows[0].ai_action).toBe('需要回复')
    expect(rows[0].attach_count).toBe(2)

    // 102 — has CN body + partial LLM labels + only an inline (cid:) attachment
    // The inline image must NOT bump the user-visible attach_count.
    expect(rows[1].snippet).toBeNull() // Sprint 19 懒取, 内容见末尾 listEmailSnippets
    expect(rows[1].lang).toBe('zh')
    expect(rows[1].ai_priority).toBe('important') // mapped from "🟡 重要"
    expect(rows[1].ai_action).toBe('需要决策')
    expect(rows[1].attach_count).toBe(0)

    // 103 — fetch_failed, no email_body row, no llm_processing row
    expect(rows[2].snippet).toBeNull()
    expect(rows[2].lang).toBe('unknown')
    expect(rows[2].ai_priority).toBeNull()
    expect(rows[2].ai_action).toBeNull()
    expect(rows[2].attach_count).toBe(0)

    // 201 — sent box, has body? no body seeded → snippet null; no LLM row either
    expect(rows[3].snippet).toBeNull()
    expect(rows[3].lang).toBe('unknown')
    expect(rows[3].ai_priority).toBeNull()

    // Sprint 19 — snippet 内容由 listEmailSnippets 懒取提供 (listEnriched 只给 null
    // 占位)。把原先挂在 listEnriched 上的内容断言迁来, 保住覆盖 + 验证懒取路径:
    // 有 body 的 101/102 返回内容; 无 body 的 103/201 不进 map。
    const snippets = handlers.listEmailSnippets([101, 102, 103, 201])
    expect(snippets[101]).toMatch(/^Hey, the redis client/)
    expect(snippets[102]?.startsWith('本周 *产品*')).toBe(true)
    expect(snippets[103]).toBeUndefined()
    expect(snippets[201]).toBeUndefined()
  })

  test('mailbox filter does not trip the JOIN ambiguity (m.mailbox vs llm.mailbox)', () => {
    const rows = handlers.listEmailsEnriched({ mailbox: '发件箱' })
    expect(rows).toHaveLength(1)
    expect(rows[0].internal_id).toBe(201)
  })

  test('honours isRead + status filters', () => {
    const rows = handlers.listEmailsEnriched({ status: 'synced', isRead: false })
    expect(rows.map((r) => r.internal_id)).toEqual([101])
  })

  test('cli.gen.ts EmailMeta core fields are intact (extends contract)', () => {
    const row = handlers.listEmailsEnriched({ limit: 1 })[0]!
    // Schema-anchored fields must still be there and well-shaped
    expect(typeof row.internal_id).toBe('number')
    expect(typeof row.subject).toBe('string')
    expect(typeof row.sender).toBe('string')
    expect(typeof row.is_read).toBe('boolean')
    expect(typeof row.is_flagged).toBe('boolean')
    expect(row.notion_url).toMatch(/^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/)
  })
})

describe('listMailboxes', () => {
  test('aggregates per mailbox with total + unread + flagged + failed counts', () => {
    const rows = handlers.listMailboxes()
    // 收件箱 has 3 rows (101 unread+flagged, 102 read, 103 unread+failed) →
    //   total=3, unread=2, flagged=1, failed=1
    // 发件箱 has 1 row (201 read, synced) → total=1, all-zero counts
    // Sprint 10 user-acceptance shape: listMailboxes now returns flagged +
    // failed alongside total + unread so the Sidebar virtual entries can
    // surface real counts (previous hardcoded 0).
    // attention (「需关注」): 101 labels=紧急+需要回复, 102 labels=重要+需要决策
    // (主表 ai_* 为 NULL → labels_json fallback) → 收件箱 2; 103 无 LLM → 不计。
    expect(rows).toEqual([
      { mailbox: '收件箱', total: 3, unread: 2, flagged: 1, failed: 1, attention: 2 },
      { mailbox: '发件箱', total: 1, unread: 0, flagged: 0, failed: 0, attention: 0 }
    ])
  })

  test('excludes NULL / empty-string mailbox rows', () => {
    // Insert a row with mailbox=NULL; it must not show up in the list.
    const db = fixtureDb
    db.prepare(
      `INSERT INTO email_metadata (internal_id, message_id, subject, sender, mailbox, is_read, is_flagged)
       VALUES (999, '<orphan@example.com>', 'orphan', 'x@x', NULL, 0, 0)`
    ).run()
    try {
      const rows = handlers.listMailboxes()
      // Should still be 2 entries (the NULL-mailbox row excluded)
      expect(rows.map((r) => r.mailbox)).toEqual(['收件箱', '发件箱'])
    } finally {
      db.prepare('DELETE FROM email_metadata WHERE internal_id = 999').run()
    }
  })
})

describe('listEmailsByThread', () => {
  test('returns sibling rows ordered by date ASC for a multi-member thread', () => {
    const db = fixtureDb
    // Seed two extra siblings on thread-A so there's a real thread to walk.
    db.prepare(
      `INSERT INTO email_metadata
         (internal_id, message_id, thread_id, subject, sender, mailbox,
          is_read, is_flagged, sync_status, notion_page_id, date_received)
       VALUES (104, '<msg-104@example.com>', 'thread-A', 'Re: redis timeout debug session',
               'alice@example.com', '收件箱', 1, 0, 'synced',
               'cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee', '2026-05-15T11:00:00+08:00')`
    ).run()
    db.prepare(
      `INSERT INTO email_metadata
         (internal_id, message_id, thread_id, subject, sender, mailbox,
          is_read, is_flagged, sync_status, notion_page_id, date_received)
       VALUES (100, '<msg-100@example.com>', 'thread-A', 'redis timeout debug session',
               'alice@example.com', '收件箱', 1, 0, 'synced',
               'dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee', '2026-05-15T07:00:00+08:00')`
    ).run()
    try {
      const rows = handlers.listEmailsByThread('thread-A')
      // 100 (07:00) → 101 (09:00) → 104 (11:00) chronological ascending
      expect(rows.map((r) => r.internal_id)).toEqual([100, 101, 104])
      expect(rows[0].thread_id).toBe('thread-A')
      expect(rows[0].notion_url).toMatch(/^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/)
      expect(typeof rows[0].is_read).toBe('boolean')
    } finally {
      db.prepare('DELETE FROM email_metadata WHERE internal_id IN (100, 104)').run()
    }
  })

  test('single-member thread returns just the one email', () => {
    const rows = handlers.listEmailsByThread('thread-B')
    expect(rows).toHaveLength(1)
    expect(rows[0].internal_id).toBe(102)
  })

  test('unknown thread_id returns empty list (not null)', () => {
    expect(handlers.listEmailsByThread('thread-does-not-exist')).toEqual([])
  })

  test('empty / null thread_id input → empty list', () => {
    expect(handlers.listEmailsByThread('')).toEqual([])
    expect(handlers.listEmailsByThread(null as unknown as string)).toEqual([])
  })
})

describe('getAIFields', () => {
  test('decodes labels_json + processing_status + review status for a fully-LLM-processed row', () => {
    const f = handlers.getAIFields(101)!
    expect(f.internal_id).toBe(101)
    expect(f.processing_status).toBe('AI Reviewed')
    expect(f.mailbox).toBe('收件箱')
    expect(f.is_read).toBe(false)
    expect(f.is_flagged).toBe(true)
    expect(f.ai_priority).toBe('critical')
    expect(f.ai_action).toBe('需要回复')
    expect(f.ai_review_status).toBe('reviewed') // llm_status='success' → reviewed
    expect(f.sentiment).toBeNull() // agent doesn't emit this — REVIEW-LOG H-14 follow-up
    expect(f.labels_raw).not.toBeNull()
    expect(f.labels_raw?.category).toBe('🔧 技术支持')
  })

  test('failed LLM run still surfaces partial labels but review_status = pending', () => {
    const f = handlers.getAIFields(102)!
    expect(f.processing_status).toBe('已同步')
    expect(f.ai_priority).toBe('important')
    expect(f.ai_action).toBe('需要决策')
    expect(f.ai_review_status).toBe('pending') // llm_status='failed' → pending
    expect(f.labels_raw?.language).toBe('中文')
  })

  test('no llm_processing row at all → ai_* fields null', () => {
    const f = handlers.getAIFields(103)!
    expect(f.processing_status).toBeNull()
    expect(f.ai_priority).toBeNull()
    expect(f.ai_action).toBeNull()
    expect(f.ai_review_status).toBeNull()
    expect(f.labels_raw).toBeNull()
  })

  test('returns null for a missing internal_id', () => {
    expect(handlers.getAIFields(99_999)).toBeNull()
  })
})

describe('listEmailsEnriched attention view (「需关注」)', () => {
  // 判定 = 日报 is_attention (src/reports/data.py) 的 SQL 化:
  //   进入: is_pinned=1 OR (ai_priority ∈ {🔴 紧急, 🟡 重要} AND ai_action ∈ 需动作集)
  //   排除: ①发件箱 ②已回复 (同 thread 有更晚发件箱邮件) ③processing_status='已完成'
  // 种子行 9xx 自包含, afterAll 清理, 不污染其它 describe。
  beforeAll(() => {
    const ins = fixtureDb.prepare(`
      INSERT INTO email_metadata
        (internal_id, message_id, thread_id, subject, sender, date_received,
         mailbox, sync_status, is_pinned, ai_priority, ai_action, processing_status)
      VALUES (@id, @mid, @tid, @subject, 'x@example.com', @date,
              @mailbox, 'synced', @pinned, @pri, @act, @proc)
    `)
    const rows = [
      // 901 紧急+需要回复 (主表列) → 进
      {
        id: 901,
        tid: 'th-att-1',
        date: '2026-06-01T10:00:00+08:00',
        mailbox: '收件箱',
        pinned: 0,
        pri: '🔴 紧急',
        act: '需要回复',
        proc: null
      },
      // 902 紧急+需要回复 但 912 是同 thread 更晚的发件箱邮件 → 已回复, 排除
      {
        id: 902,
        tid: 'th-att-2',
        date: '2026-06-01T10:00:00+08:00',
        mailbox: '收件箱',
        pinned: 0,
        pri: '🔴 紧急',
        act: '需要回复',
        proc: null
      },
      {
        id: 912,
        tid: 'th-att-2',
        date: '2026-06-02T10:00:00+08:00',
        mailbox: '发件箱',
        pinned: 0,
        pri: null,
        act: null,
        proc: null
      },
      // 903 紧急+需要回复, 913 同 thread 但发件**更早** (不是回复) → 进
      {
        id: 903,
        tid: 'th-att-3',
        date: '2026-06-03T10:00:00+08:00',
        mailbox: '收件箱',
        pinned: 0,
        pri: '🟡 重要',
        act: '需要跟进',
        proc: null
      },
      {
        id: 913,
        tid: 'th-att-3',
        date: '2026-06-01T08:00:00+08:00',
        mailbox: '发件箱',
        pinned: 0,
        pri: null,
        act: null,
        proc: null
      },
      // 904 置顶 (无 AI 字段) → 进
      {
        id: 904,
        tid: 'th-att-4',
        date: '2026-06-01T09:00:00+08:00',
        mailbox: '收件箱',
        pinned: 1,
        pri: null,
        act: null,
        proc: null
      },
      // 905 置顶但已完成 → 排除
      {
        id: 905,
        tid: 'th-att-5',
        date: '2026-06-01T09:00:00+08:00',
        mailbox: '收件箱',
        pinned: 1,
        pri: null,
        act: null,
        proc: '已完成'
      },
      // 906 发件箱里的紧急邮件 → 排除 (发件箱)
      {
        id: 906,
        tid: 'th-att-6',
        date: '2026-06-01T09:00:00+08:00',
        mailbox: '发件箱',
        pinned: 0,
        pri: '🔴 紧急',
        act: '需要回复',
        proc: null
      },
      // 907 紧急但 action=仅供参考 (不在需动作集) → 排除
      {
        id: 907,
        tid: 'th-att-7',
        date: '2026-06-01T09:00:00+08:00',
        mailbox: '收件箱',
        pinned: 0,
        pri: '🔴 紧急',
        act: '仅供参考',
        proc: null
      },
      // 908 一般优先级+需要回复 (不在紧急集) → 排除
      {
        id: 908,
        tid: 'th-att-8',
        date: '2026-06-01T09:00:00+08:00',
        mailbox: '收件箱',
        pinned: 0,
        pri: '🟢 一般',
        act: '需要回复',
        proc: null
      }
    ]
    const seed = fixtureDb.transaction(() => {
      for (const r of rows)
        ins.run({ ...r, mid: `<att-${r.id}@example.com>`, subject: `att ${r.id}` })
    })
    seed()
  })

  afterAll(() => {
    fixtureDb.prepare('DELETE FROM email_metadata WHERE internal_id >= 900').run()
  })

  test('紧急×需动作进入、已回复/发件箱/已完成/非命中组合排除', () => {
    const ids = new Set(
      handlers.listEmailsEnriched({ attention: true, limit: 100 }).map((e) => e.internal_id)
    )
    expect(ids.has(901)).toBe(true) // 紧急+需要回复
    expect(ids.has(903)).toBe(true) // 重要+需要跟进, 发件更早不算回复
    expect(ids.has(904)).toBe(true) // 置顶
    expect(ids.has(902)).toBe(false) // 已回复
    expect(ids.has(905)).toBe(false) // 置顶但已完成
    expect(ids.has(906)).toBe(false) // 发件箱
    expect(ids.has(907)).toBe(false) // action 不在需动作集
    expect(ids.has(908)).toBe(false) // priority 不在紧急集
    expect(ids.has(912)).toBe(false)
    expect(ids.has(913)).toBe(false)
  })

  test('labels_json fallback: 主表列为空时从 llm_processing 判定 (存量未 backfill)', () => {
    const ids = new Set(
      handlers.listEmailsEnriched({ attention: true, limit: 100 }).map((e) => e.internal_id)
    )
    // fixture 101 = labels {priority: 🔴 紧急, action_type: 需要回复}, 主表 ai_* 为 NULL
    expect(ids.has(101)).toBe(true)
    // fixture 102 = labels {priority: 🟡 重要, action_type: 需要决策}
    expect(ids.has(102)).toBe(true)
    // fixture 103 无 LLM run → 排除; 201 发件箱+已完成 → 排除
    expect(ids.has(103)).toBe(false)
    expect(ids.has(201)).toBe(false)
  })

  test('attention 与其它 filter 叠加 (AND 语义)', () => {
    const unreadOnly = handlers.listEmailsEnriched({ attention: true, isRead: false, limit: 100 })
    for (const e of unreadOnly) expect(e.is_read).toBe(false)
    const ids = new Set(unreadOnly.map((e) => e.internal_id))
    expect(ids.has(101)).toBe(true) // 101 unread
    expect(ids.has(102)).toBe(false) // 102 已读, 被 isRead=false 滤掉
  })

  test('listMailboxes attention 计数与列表行数同判定', () => {
    const listIds = handlers
      .listEmailsEnriched({ attention: true, limit: 100 })
      .map((e) => e.internal_id)
    const summaries = handlers.listMailboxes()
    const totalAttention = summaries.reduce((sum, m) => sum + (m.attention ?? 0), 0)
    expect(totalAttention).toBe(listIds.length)
    const inbox = summaries.find((m) => m.mailbox === '收件箱')
    // 101, 102, 901, 903, 904 全在收件箱
    expect(inbox?.attention).toBe(5)
    const sent = summaries.find((m) => m.mailbox === '发件箱')
    expect(sent?.attention).toBe(0)
  })
})
