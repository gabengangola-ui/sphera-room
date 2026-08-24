/**
 * SPHERA Bridge v0.0.6
 *
 * Changes from v0.0.5 — artifact lifecycle only:
 *
 * 1. artifact_intent stores recoverable content
 *    The intent event now includes the actual artifact bytes (capped at 100 KB).
 *    A reconciliation process can retry the GitHub sync from the ledger alone,
 *    without asking any principal to resend content.
 *
 * 2. Completion events close the lifecycle
 *    After GitHub write, the bridge appends a second ledger event:
 *      artifact_committed  { intent_id, intent_seq, path, sha }
 *      artifact_failed     { intent_id, intent_seq, path, error }
 *    Both reference the originating intent. Inspecting the ledger now tells
 *    you unambiguously whether a GitHub sync succeeded or not.
 *
 * 3. Idempotency key prevents duplicate intents on retry
 *    Callers may supply idempotency_key. If omitted, the bridge derives one
 *    from SHA-256(principal + path + content). A second POST /artifact with
 *    the same key finds the existing intent and proceeds to the GitHub step
 *    without creating a new intent event.
 *
 * Nothing else changed from v0.0.5.
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

  const event = await ledgerAppend({ principal, type: 'message', content: content.trim() }, env);
  return json({ ok: true, event_id: event.id, seq: event.seq }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_ARTIFACT_BYTES + 2_000);
  if (raw === null) return json({ error: 'Artifact exceeds size limit' }, 413);

  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { path, content, commit_message, idempotency_key } = body;

  if (!path    || typeof path    !== 'string') return json({ error: '"path" is required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" is required' }, 400);

  if (utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
    return json({ error: `Content exceeds ${MAX_ARTIFACT_BYTES} byte limit (UTF-8 bytes)` }, 413);
  }

  if (commit_message !== undefined && commit_message !== null) {
    if (typeof commit_message !== 'string') return json({ error: '"commit_message" must be a string' }, 400);
    if (commit_message.length > MAX_COMMIT_MSG_CHARS) {
      return json({ error: `"commit_message" must not exceed ${MAX_COMMIT_MSG_CHARS} characters` }, 400);
    }
  }

  if (idempotency_key !== undefined && idempotency_key !== null &&
      (typeof idempotency_key !== 'string' || idempotency_key.length > 128)) {
    return json({ error: '"idempotency_key" must be a string under 128 characters' }, 400);
  }

  const safePath = sanitizePath(path);
  if (!safePath) {
    return json({
      error: `Path rejected. Must begin with one of: ${ALLOWED_PREFIXES.join(', ')}`,
      hint:  'Paths with "." or ".." segments are also rejected.'
    }, 400);
  }

  const message  = (typeof commit_message === 'string' ? commit_message.trim() : '') ||
                   `${principal}: publish ${safePath}`;
  const idemKey  = (typeof idempotency_key === 'string' && idempotency_key.trim())
                   ? idempotency_key.trim()
                   : await deriveIdempotencyKey(principal, safePath, content);

  // Step 1 — Idempotent intent.
  // If this key already has an intent, returns the existing one.
  // Retry calls never create duplicate intent events.
  const { intent, is_new } = await ledgerIntentStart({
    principal,
    path:           safePath,
    commit_message: message,
    content,          // stored so reconciliation can retry without re-upload
    idempotency_key: idemKey,
  }, env);

  // Step 2 — GitHub sync (best-effort).
  const result = await commitWithRetry(safePath, content, message, env);

  // Step 3 — Completion event closes the lifecycle.
  // artifact_committed or artifact_failed always references the intent.
  if (result.ok) {
    const committed = await ledgerIntentComplete({
      principal,
      outcome:    'artifact_committed',
      intent_id:  intent.id,
      intent_seq: intent.seq,
      path:       safePath,
      sha:        result.sha,
    }, env);

    return json({
      ok:            true,
      is_new_intent: is_new,
      intent_id:     intent.id,
      intent_seq:    intent.seq,
      committed_seq: committed.seq,
      sha:           result.sha,
    }, 201);

  } else {
    const failed = await ledgerIntentComplete({
      principal,
      outcome:    'artifact_failed',
      intent_id:  intent.id,
      intent_seq: intent.seq,
      path:       safePath,
      error:      result.detail,
    }, env);

    return json({
      ok:            false,
      error:         'GitHub sync failed. Intent is recorded; retry will not create a duplicate.',
      intent_id:     intent.id,
      intent_seq:    intent.seq,
      failed_seq:    failed.seq,
      idempotency_key: idemKey,
      detail:        result.detail,
    }, 502);
  }
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

export class EventLedger {
  constructor(state) { this.state = state; }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      if (request.method === 'POST' && url.pathname === '/append')           return this.handleAppend(request);
      if (request.method === 'POST' && url.pathname === '/artifact-start')   return this.handleArtifactStart(request);
      if (request.method === 'POST' && url.pathname === '/artifact-complete') return this.handleArtifactComplete(request);
      if (request.method === 'GET'  && url.pathname === '/events')            return this.handleRead(url);
      return new Response('Not found', { status: 404 });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500, headers: { 'Content-Type': 'application/json' }
      });
    }
  }

  // Generic append — used for message events
  async handleAppend(request) {
    const partial = await request.json();
    const event   = await this._appendOne(partial);
    return new Response(JSON.stringify(event), {
      status: 201, headers: { 'Content-Type': 'application/json' }
    });
  }

  // Idempotent artifact intent.
  // If idempotency_key already maps to an existing intent, return it (is_new: false).
  // Otherwise create and persist the intent with full content (is_new: true).
  async handleArtifactStart(request) {
    const { principal, path, commit_message, content, idempotency_key } = await request.json();

    const result = await this.state.storage.transaction(async (txn) => {
      const existingId = await txn.get(`idem:${idempotency_key}`);

      if (existingId) {
        const existing = JSON.parse(await txn.get(`intent:${existingId}`));
        return { intent: existing, is_new: false };
      }

      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const intent   = {
        id:              crypto.randomUUID(),
        seq,
        timestamp:       new Date().toISOString(),
        principal,
        type:            'artifact_intent',
        path,
        commit_message,
        content,          // recoverable — reconciliation can retry from this
        idempotency_key,
      };

      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(intent));
      await txn.put(`intent:${intent.id}`, JSON.stringify(intent));
      await txn.put(`idem:${idempotency_key}`, intent.id);

      return { intent, is_new: true };
    });

    return new Response(JSON.stringify(result), {
      status: 201, headers: { 'Content-Type': 'application/json' }
    });
  }

  // Append artifact_committed or artifact_failed, both referencing the intent.
  async handleArtifactComplete(request) {
    const partial = await request.json();
    const event   = await this._appendOne(partial);
    return new Response(JSON.stringify(event), {
      status: 201, headers: { 'Content-Type': 'application/json' }
    });
  }

  async handleRead(url) {
    const after    = parseInt(url.searchParams.get('after') ?? '0', 10);
    const startKey = `event:${String(after + 1).padStart(10, '0')}`;
    const entries  = await this.state.storage.list({ prefix: 'event:', start: startKey });
    const events   = [...entries.values()].map(v => JSON.parse(v));
    return new Response(JSON.stringify({ events, count: events.length }), {
      headers: { 'Content-Type': 'application/json' }
    });
  }

  async _appendOne(partial) {
    return this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id:        crypto.randomUUID(),
        seq,
        timestamp: new Date().toISOString(),
        ...partial,
      };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      return complete;
    });
  }
}

// ── DO client helpers ─────────────────────────────────────────────────────────

function getLedgerStub(env) {
  return env.LEDGER.get(env.LEDGER.idFromName('global'));
}

async function ledgerAppend(partial, env) {
  const resp = await getLedgerStub(env).fetch('http://internal/append', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(partial)
  });
  if (!resp.ok) throw new Error(`Ledger append failed: ${await resp.text()}`);
  return resp.json();
}

async function ledgerIntentStart(data, env) {
  const resp = await getLedgerStub(env).fetch('http://internal/artifact-start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
  });
  if (!resp.ok) throw new Error(`Ledger intent-start failed: ${await resp.text()}`);
  return resp.json();
}

async function ledgerIntentComplete(data, env) {
  const resp = await getLedgerStub(env).fetch('http://internal/artifact-complete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
  });
  if (!resp.ok) throw new Error(`Ledger intent-complete failed: ${await resp.text()}`);
  return resp.json();
}

async function doReadSince(after, env) {
  const resp = await getLedgerStub(env).fetch(`http://internal/events?after=${after}`);
  if (!resp.ok) throw new Error(`Ledger read failed: ${await resp.text()}`);
  return resp.json();
}

// ── Idempotency key derivation ────────────────────────────────────────────────
// Default: SHA-256(principal|path|content), base64url, first 22 chars.
// Caller-supplied keys are preferred when provided.

async function deriveIdempotencyKey(principal, path, content) {
  const data   = new TextEncoder().encode(`${principal}|${path}|${content}`);
  const hash   = await crypto.subtle.digest('SHA-256', data);
  const bytes  = new Uint8Array(hash);
  const b64    = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
  return b64.slice(0, 22);
}

// ── Path sanitization ─────────────────────────────────────────────────────────

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH)    return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }

  const segments = decoded.split('/');
  if (segments.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;
  if (segments.some(s => s === '.' || s === '..'))     return null;

  const clean = segments.filter((s, i) => {
    if (i === 0 && s === '')                        return false;
    if (i === segments.length - 1 && s === '')      return false;
    return true;
  });

  if (clean.length === 0 || clean.some(s => s === '')) return null;

  const joined = clean.join('/');
  if (!ALLOWED_PREFIXES.some(p => joined.startsWith(p))) return null;

  return joined;
}

// ── GitHub — bounded retry ────────────────────────────────────────────────────

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

function utf8ByteLength(str) { return new TextEncoder().encode(str).length; }

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
