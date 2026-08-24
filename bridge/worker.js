/**
 * SPHERA Bridge v0.0.6
 *
 * Changes from v0.0.5 — artifact lifecycle completed:
 *
 * 1. CONTENT STORED IN INTENT
 *    artifact_intent now includes the artifact content (bounded by 100 KB cap).
 *    The ledger is self-contained: a reconciliation process can retry the
 *    GitHub sync from the ledger alone without asking the caller to resend.
 *
 * 2. COMPLETION EVENTS
 *    After GitHub write: appends artifact_committed (with SHA) or
 *    artifact_failed (with error). Both reference intent_id and intent_seq.
 *    The ledger now distinguishes success from failure — provenance is complete.
 *
 * 3. IDEMPOTENCY KEY
 *    Caller may supply idempotency_key (string). Bridge auto-generates one if
 *    absent. On retry with the same key:
 *    - If intent exists and completion exists → return completion idempotently.
 *    - If intent exists but no completion → retry GitHub, write completion.
 *    No duplicate intent events are ever created for the same key.
 *
 * Artifact flow:
 *   artifact_intent (content stored) → GitHub write → artifact_committed | artifact_failed
 *   Both completion types reference: intent_id, intent_seq.
 */

const MAX_MESSAGE_BYTES    = 10_000;
const MAX_ARTIFACT_BYTES   = 100_000;
const MAX_COMMIT_MSG_CHARS = 500;
const MAX_PATH_LENGTH      = 200;
const GITHUB_MAX_RETRIES   = 3;
const REPO                 = 'gabengangola-ui/sphera-room';
const ALLOWED_PREFIXES     = ['artifacts/', 'sessions/'];

// ── Main Worker ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (err) {
      return json({ error: 'Internal error', detail: err.message }, 500);
    }
  }
};

async function route(request, env) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  const principal = authenticate(request, env);
  if (!principal) return json({ error: 'Unauthorized' }, 401);

  const url = new URL(request.url);

  if (request.method === 'POST' && url.pathname === '/message')  return handlePostMessage(request, principal, env);
  if (request.method === 'POST' && url.pathname === '/artifact') return handlePublishArtifact(request, principal, env);
  if (request.method === 'GET'  && url.pathname === '/events')   return handleReadEvents(url, env);

  return json({ error: 'Not found' }, 404);
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function authenticate(request, env) {
  const header = request.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return null;
  const token = header.slice(7).trim();
  if (token === env.CLAUDE_KEY)   return 'claude';
  if (token === env.SOBA_KEY)     return 'soba';
  if (token === env.ARCHIVES_KEY) return 'archives';
  return null;
}

// ── Handlers ──────────────────────────────────────────────────────────────────

async function handlePostMessage(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  if (raw === null) return json({ error: `Message exceeds ${MAX_MESSAGE_BYTES} byte limit` }, 413);

  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { content } = body;
  if (!content || typeof content !== 'string' || !content.trim()) {
    return json({ error: '"content" must be a non-empty string' }, 400);
  }

  const event = await doAppend({ principal, type: 'message', content: content.trim() }, env);
  return json({ ok: true, event_id: event.id, seq: event.seq }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_ARTIFACT_BYTES + 4_000);
  if (raw === null) return json({ error: 'Artifact payload exceeds size limit' }, 413);

  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { path, content, commit_message, idempotency_key: rawKey } = body;

  if (!path    || typeof path    !== 'string') return json({ error: '"path" is required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" is required' }, 400);

  if (utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
    return json({ error: `Content exceeds ${MAX_ARTIFACT_BYTES} byte limit (UTF-8)` }, 413);
  }
  if (commit_message !== undefined && commit_message !== null) {
    if (typeof commit_message !== 'string') return json({ error: '"commit_message" must be a string' }, 400);
    if (commit_message.length > MAX_COMMIT_MSG_CHARS) {
      return json({ error: `"commit_message" exceeds ${MAX_COMMIT_MSG_CHARS} character limit` }, 400);
    }
  }

  const safePath = sanitizePath(path);
  if (!safePath) {
    return json({
      error: `Path rejected. Must begin with: ${ALLOWED_PREFIXES.join(', ')}`,
      hint:  '"." and ".." segments are rejected outright.'
    }, 400);
  }

  // Caller-supplied idempotency key, or auto-generated for this request.
  // Retry with the same key to avoid creating duplicate intent events.
  const ikey    = (typeof rawKey === 'string' && rawKey.trim()) ? rawKey.trim() : crypto.randomUUID();
  const message = (typeof commit_message === 'string' ? commit_message.trim() : '') ||
                  `${principal}: publish ${safePath}`;

  // ── Step 1: Get or create intent (idempotent) ────────────────────────────
  const { intent, completion } = await doArtifactBegin({
    principal,
    path:            safePath,
    commit_message:  message,
    content,          // stored in the ledger — the bridge can recover without re-asking the caller
    idempotency_key: ikey,
  }, env);

  // ── Already completed on a prior call — return idempotently ──────────────
  if (completion) {
    const ok = completion.type === 'artifact_committed';
    return json({
      ok,
      idempotent: true,
      event_id:   completion.id,
      seq:        completion.seq,
      intent_seq: intent.seq,
      ...(ok ? { sha: completion.sha } : { error: completion.error })
    }, ok ? 200 : 502);
  }

  // ── Step 2: Sync to GitHub ───────────────────────────────────────────────
  const result = await commitWithRetry(safePath, content, message, env);

  // ── Step 3: Record outcome (committed or failed) — both reference intent ─
  if (result.ok) {
    const committed = await doArtifactEnd({
      type:       'artifact_committed',
      principal,
      intent_id:  intent.id,
      intent_seq: intent.seq,
      path:       safePath,
      sha:        result.sha,
    }, env);
    return json({
      ok:         true,
      event_id:   committed.id,
      seq:        committed.seq,
      intent_seq: intent.seq,
      sha:        result.sha,
    }, 201);
  } else {
    const failed = await doArtifactEnd({
      type:       'artifact_failed',
      principal,
      intent_id:  intent.id,
      intent_seq: intent.seq,
      path:       safePath,
      error:      result.detail,
    }, env);
    return json({
      ok:              false,
      error:           'GitHub sync failed. Intent is in the ledger — retry with the same idempotency_key.',
      detail:          result.detail,
      idempotency_key: ikey,
      event_id:        failed.id,
      seq:             failed.seq,
      intent_seq:      intent.seq,
    }, 502);
  }
}

async function handleReadEvents(url, env) {
  const afterParam = url.searchParams.get('after');
  const after      = afterParam !== null ? parseInt(afterParam, 10) : 0;
  if (isNaN(after) || after < 0) return json({ error: '"after" must be a non-negative integer' }, 400);

  const { events } = await doReadSince(after, env);
  const cursor      = events.length > 0 ? events.at(-1).seq : after;
  return json({ events, count: events.length, cursor });
}

// ── Durable Object — EventLedger ─────────────────────────────────────────────
//
// Internal storage layout:
//   __seq__                   → current sequence counter (integer)
//   event:{seq_10d}           → serialised event (all types)
//   intent:{intent_id}        → serialised artifact_intent event (for lookup)
//   ikey:{idempotency_key}    → intent_id  (dedup index)
//   completion:{intent_id}    → serialised completion event (committed | failed)

export class EventLedger {
  constructor(state) { this.state = state; }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      if (request.method === 'POST' && url.pathname === '/append')          return this.handleAppend(request);
      if (request.method === 'POST' && url.pathname === '/artifact-begin')  return this.handleArtifactBegin(request);
      if (request.method === 'POST' && url.pathname === '/artifact-end')    return this.handleArtifactEnd(request);
      if (request.method === 'GET'  && url.pathname === '/events')          return this.handleRead(url);
      return new Response('Not found', { status: 404 });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500, headers: { 'Content-Type': 'application/json' }
      });
    }
  }

  // Generic append — used for messages
  async handleAppend(request) {
    const partial = await request.json();
    const event   = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), ...partial };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      return complete;
    });
    return doResponse(event, 201);
  }

  // Idempotent intent creation
  async handleArtifactBegin(request) {
    const { idempotency_key, ...partial } = await request.json();

    // Check for existing intent via idempotency key
    const existingId = await this.state.storage.get(`ikey:${idempotency_key}`);
    if (existingId) {
      const intent     = JSON.parse(await this.state.storage.get(`intent:${existingId}`));
      const compRaw    = await this.state.storage.get(`completion:${existingId}`);
      const completion = compRaw ? JSON.parse(compRaw) : null;
      return doResponse({ intent, completion, is_retry: true }, 200);
    }

    // New intent — atomically assign seq, persist event, index by ikey
    const intent = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id:              crypto.randomUUID(),
        seq,
        timestamp:       new Date().toISOString(),
        type:            'artifact_intent',
        idempotency_key,
        ...partial,      // principal, path, commit_message, content
      };
      const ekey = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(ekey,                      JSON.stringify(complete));
      await txn.put(`intent:${complete.id}`,   JSON.stringify(complete));
      await txn.put(`ikey:${idempotency_key}`, complete.id);
      return complete;
    });

    return doResponse({ intent, completion: null, is_retry: false }, 201);
  }

  // Append completion event — idempotent (safe to call twice)
  async handleArtifactEnd(request) {
    const { intent_id, ...partial } = await request.json();

    // Already completed? Return existing completion without creating a duplicate
    const existing = await this.state.storage.get(`completion:${intent_id}`);
    if (existing) return doResponse(JSON.parse(existing), 200);

    const completion = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id:        crypto.randomUUID(),
        seq,
        timestamp: new Date().toISOString(),
        intent_id,
        ...partial, // type (artifact_committed|artifact_failed), principal, intent_seq, path, sha|error
      };
      const ekey = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(ekey,                        JSON.stringify(complete));
      await txn.put(`completion:${intent_id}`,   JSON.stringify(complete));
      return complete;
    });

    return doResponse(completion, 201);
  }

  async handleRead(url) {
    const after    = parseInt(url.searchParams.get('after') ?? '0', 10);
    const startKey = `event:${String(after + 1).padStart(10, '0')}`;
    const entries  = await this.state.storage.list({ prefix: 'event:', start: startKey });
    const events   = [...entries.values()].map(v => JSON.parse(v));
    return doResponse({ events, count: events.length }, 200);
  }
}

function doResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' }
  });
}

// ── DO client helpers ─────────────────────────────────────────────────────────

function getLedger(env) {
  return env.LEDGER.get(env.LEDGER.idFromName('global'));
}

async function doAppend(partial, env) {
  const r = await getLedger(env).fetch('http://internal/append', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(partial)
  });
  if (!r.ok) throw new Error(`Ledger append failed: ${await r.text()}`);
  return r.json();
}

async function doArtifactBegin(partial, env) {
  const r = await getLedger(env).fetch('http://internal/artifact-begin', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(partial)
  });
  if (!r.ok) throw new Error(`Ledger artifact-begin failed: ${await r.text()}`);
  return r.json();
}

async function doArtifactEnd(partial, env) {
  const r = await getLedger(env).fetch('http://internal/artifact-end', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(partial)
  });
  if (!r.ok) throw new Error(`Ledger artifact-end failed: ${await r.text()}`);
  return r.json();
}

async function doReadSince(after, env) {
  const r = await getLedger(env).fetch(`http://internal/events?after=${after}`);
  if (!r.ok) throw new Error(`Ledger read failed: ${await r.text()}`);
  return r.json();
}

// ── Path sanitization — strict ────────────────────────────────────────────────

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH)    return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }

  const segments = decoded.split('/');

  if (segments.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;
  if (segments.some(s => s === '.' || s === '..'))     return null; // reject, not filter

  const clean = segments.filter((s, i) =>
    !(i === 0 && s === '') && !(i === segments.length - 1 && s === '')
  );

  if (clean.length === 0 || clean.some(s => s === '')) return null;

  const joined = clean.join('/');
  if (!ALLOWED_PREFIXES.some(p => joined.startsWith(p))) return null;

  return joined;
}

// ── GitHub — bounded retry, explicit failure ──────────────────────────────────

async function commitWithRetry(path, content, message, env) {
  for (let attempt = 1; attempt <= GITHUB_MAX_RETRIES; attempt++) {
    if (attempt > 1) await sleep(Math.random() * 400 + 200 * attempt);
    const result = await commitToGitHub(path, content, message, env);
    if (result.ok)             return result;
    if (result.status !== 409) return result;
  }
  return {
    ok:     false,
    status: 409,
    detail: `GitHub write failed after ${GITHUB_MAX_RETRIES} attempts (SHA conflict on "${path}"). Retry shortly.`
  };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = {
    'Authorization': `token ${env.GITHUB_TOKEN}`,
    'Content-Type':  'application/json',
    'User-Agent':    'sphera-bridge/0.0.6'
  };

  let sha;
  const existing = await fetch(url, { headers });
  if (existing.ok) { const d = await existing.json(); sha = d.sha; }

  const body = {
    message,
    content: btoa(unescape(encodeURIComponent(content))),
    ...(sha ? { sha } : {})
  };

  const response = await fetch(url, { method: 'PUT', headers, body: JSON.stringify(body) });
  if (!response.ok) {
    return { ok: false, status: response.status, detail: await response.text() };
  }
  const result = await response.json();
  return { ok: true, sha: result.content?.sha };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function utf8ByteLength(str) {
  return new TextEncoder().encode(str).length;
}

async function readBodyWithLimit(request, limit) {
  const reader = request.body?.getReader();
  if (!reader) return '';
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) { reader.cancel(); return null; }
    chunks.push(value);
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  return new TextDecoder().decode(merged);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type'
  };
}
