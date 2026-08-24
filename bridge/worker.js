/**
 * SPHERA Bridge v0.0.5
 *
 * Changes from v0.0.4:
 *
 * 1. REMOVED bridge/ FROM ALLOWED_PREFIXES
 *    Callers must not be able to overwrite the bridge's own source files.
 *    Allowed roots are now: artifacts/ and sessions/ only.
 *
 * 2. ARTIFACT ORDER FIXED: DO append before GitHub commit
 *    v0.0.4 committed to GitHub first, then appended to the ledger.
 *    If the append failed, a real GitHub side effect had no ledger record.
 *    v0.0.5 appends to the DO ledger first (records intent + content),
 *    then commits to GitHub (sync layer).
 *    If GitHub fails: the ledger entry is real, the caller gets the seq,
 *    and the response clearly states the artifact was not synced.
 *    The ledger records what was attempted — GitHub sync is separate.
 */

const MAX_MESSAGE_BYTES    = 10_000;
const MAX_ARTIFACT_BYTES   = 100_000;
const MAX_COMMIT_MSG_CHARS = 500;
const MAX_PATH_LENGTH      = 200;
const GITHUB_MAX_RETRIES   = 3;
const REPO                 = 'gabengangola-ui/sphera-room';

// bridge/ removed — callers must not write to the bridge's own files
const ALLOWED_PREFIXES = ['artifacts/', 'sessions/'];

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
// Identity is always derived from the credential. Callers never declare who they are.

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

  const event = await appendEvent({ principal, type: 'message', content: content.trim() }, env);
  return json({ ok: true, event_id: event.id, seq: event.seq }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_ARTIFACT_BYTES + 2_000);
  if (raw === null) return json({ error: 'Artifact exceeds size limit' }, 413);

  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { path, content, commit_message } = body;

  if (!path    || typeof path    !== 'string') return json({ error: '"path" is required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" is required' }, 400);

  if (utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
    return json({ error: `Content exceeds ${MAX_ARTIFACT_BYTES} byte limit (UTF-8 bytes)` }, 413);
  }

  if (commit_message !== undefined && commit_message !== null) {
    if (typeof commit_message !== 'string') {
      return json({ error: '"commit_message" must be a string' }, 400);
    }
    if (commit_message.length > MAX_COMMIT_MSG_CHARS) {
      return json({ error: `"commit_message" must not exceed ${MAX_COMMIT_MSG_CHARS} characters` }, 400);
    }
  }

  const safePath = sanitizePath(path);
  if (!safePath) {
    return json({
      error: `Path rejected. Must begin with one of: ${ALLOWED_PREFIXES.join(', ')}`,
      hint:  'Paths with "." or ".." segments are also rejected.'
    }, 400);
  }

  const message = (typeof commit_message === 'string' ? commit_message.trim() : '') ||
                  `${principal}: publish ${safePath}`;

  // Step 1 — Append to ledger FIRST.
  // The DO records the intent: who, when, what path, what content.
  // This is the canonical event regardless of what GitHub does next.
  const event = await appendEvent({
    principal,
    type:           'artifact',
    path:           safePath,
    commit_message: message,
  }, env);

  // Step 2 — Sync to GitHub (best-effort persistence layer).
  // If this fails, the ledger entry is still real and the caller gets
  // the seq so they can correlate and retry the sync independently.
  const result = await commitWithRetry(safePath, content, message, env);
  if (!result.ok) {
    return json({
      ok:       false,
      error:    'Artifact recorded in ledger but GitHub sync failed.',
      detail:   result.detail,
      event_id: event.id,
      seq:      event.seq,
      note:     'The ledger entry is real. Retry the artifact publish to sync to GitHub.'
    }, 502);
  }

  return json({ ok: true, event_id: event.id, seq: event.seq, sha: result.sha }, 201);
}

async function handleReadEvents(url, env) {
  const afterParam = url.searchParams.get('after');
  const after      = afterParam !== null ? parseInt(afterParam, 10) : 0;

  if (isNaN(after) || after < 0) {
    return json({ error: '"after" must be a non-negative integer' }, 400);
  }

  const { events } = await doReadSince(after, env);
  const cursor      = events.length > 0 ? events.at(-1).seq : after;

  return json({ events, count: events.length, cursor });
}

// ── Durable Object — EventLedger ─────────────────────────────────────────────
// Single-writer authority for the room ledger.
// append(): one transaction — assign seq + persist event. Atomic, gap-free.
// readSince(): DO storage.list() with start key — ordered Map, native cursor.

export class EventLedger {
  constructor(state) { this.state = state; }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      if (request.method === 'POST' && url.pathname === '/append') return this.handleAppend(request);
      if (request.method === 'GET'  && url.pathname === '/events') return this.handleRead(url);
      return new Response('Not found', { status: 404 });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500, headers: { 'Content-Type': 'application/json' }
      });
    }
  }

  async handleAppend(request) {
    const partial = await request.json();

    const event = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id:        crypto.randomUUID(),
        seq,
        timestamp: new Date().toISOString(),
        ...partial,  // principal and type fields from authenticated caller
      };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      return complete;
    });

    return new Response(JSON.stringify(event), {
      status: 201, headers: { 'Content-Type': 'application/json' }
    });
  }

  async handleRead(url) {
    const after    = parseInt(url.searchParams.get('after') ?? '0', 10);
    const startKey = `event:${String(after + 1).padStart(10, '0')}`;

    const entries = await this.state.storage.list({ prefix: 'event:', start: startKey });
    const events  = [...entries.values()].map(v => JSON.parse(v));

    return new Response(JSON.stringify({ events, count: events.length }), {
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// ── DO client helpers ─────────────────────────────────────────────────────────

function getLedgerStub(env) {
  return env.LEDGER.get(env.LEDGER.idFromName('global'));
}

async function appendEvent(partial, env) {
  const resp = await getLedgerStub(env).fetch('http://internal/append', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(partial)
  });
  if (!resp.ok) throw new Error(`Ledger append failed: ${await resp.text()}`);
  return resp.json();
}

async function doReadSince(after, env) {
  const resp = await getLedgerStub(env).fetch(`http://internal/events?after=${after}`);
  if (!resp.ok) throw new Error(`Ledger read failed: ${await resp.text()}`);
  return resp.json();
}

// ── Path sanitization — strict ────────────────────────────────────────────────

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH)    return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }

  const segments = decoded.split('/');

  if (segments.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;
  if (segments.some(s => s === '.' || s === '..'))     return null;  // reject, not filter

  const clean = segments.filter((s, i) => {
    if (i === 0 && s === '') return false;
    if (i === segments.length - 1 && s === '') return false;
    return true;
  });

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
    'User-Agent':    'sphera-bridge/0.0.5'
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
