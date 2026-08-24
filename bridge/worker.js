/**
 * SPHERA Bridge v0.0.3
 *
 * Changes from v0.0.2 (per Soba's review):
 *
 * 1. MONOTONIC SEQUENCING via Durable Object
 *    - A single DO instance ('global') is the sole issuer of sequence numbers
 *    - Transactional storage guarantees strictly monotonic, gap-free integers
 *    - Events are stored as event:{seq_10digit}:{uuid} — lex sort == insertion order
 *    - GET /events?after=N returns only events with seq > N (cursor-based pagination)
 *
 * 2. STRICT PATH VALIDATION
 *    - Paths outside allowed roots are REJECTED, not silently redirected
 *    - Callers must be explicit: artifacts/, sessions/, or bridge/
 *
 * 3. EXPLICIT RETRY FAILURE
 *    - After GITHUB_MAX_RETRIES exhausted, fails loudly with a clear message
 *    - Non-409 errors fail immediately without retry
 */

const MAX_MESSAGE_BYTES  = 10_000;
const MAX_ARTIFACT_BYTES = 100_000;
const MAX_PATH_LENGTH    = 200;
const GITHUB_MAX_RETRIES = 3;
const REPO               = 'gabengangola-ui/sphera-room';
const ALLOWED_PREFIXES   = ['artifacts/', 'sessions/', 'bridge/'];

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

  const seq   = await nextSequence(env);
  const event = buildEvent(seq, principal, 'message', { content: content.trim() });
  await writeEvent(event, env);

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
  if (content.length > MAX_ARTIFACT_BYTES)     return json({ error: 'Content exceeds size limit' }, 413);

  const safePath = sanitizePath(path);
  if (!safePath) {
    return json({
      error: `Path rejected. Must begin with one of: ${ALLOWED_PREFIXES.join(', ')}`,
      hint:  'Example: "artifacts/my-file.md"'
    }, 400);
  }

  const message = commit_message?.trim() || `${principal}: publish ${safePath}`;
  const result  = await commitWithRetry(safePath, content, message, env);
  if (!result.ok) return json({ error: 'GitHub commit failed', detail: result.detail }, 502);

  const seq   = await nextSequence(env);
  const event = buildEvent(seq, principal, 'artifact', { path: safePath, commit_message: message, sha: result.sha });
  await writeEvent(event, env);

  return json({ ok: true, event_id: event.id, seq: event.seq, sha: result.sha }, 201);
}

async function handleReadEvents(url, env) {
  const afterParam = url.searchParams.get('after');
  const after      = afterParam !== null ? parseInt(afterParam, 10) : 0;

  if (afterParam !== null && (isNaN(after) || after < 0)) {
    return json({ error: '"after" must be a non-negative integer' }, 400);
  }

  const events = await readEventsSince(after, env);
  const cursor  = events.length > 0 ? events.at(-1).seq : after;

  return json({ events, count: events.length, cursor });
}

// ── Event Sequencer — Durable Object ─────────────────────────────────────────
// One global DO instance is the single writer for sequence numbers.
// storage.transaction() makes increment atomic — no two events share a seq.
// This gives the room a canonical total order with no ambiguity.

export class EventSequencer {
  constructor(state) { this.state = state; }

  async fetch() {
    const seq = await this.state.storage.transaction(async (txn) => {
      const current = (await txn.get('seq')) ?? 0;
      const next    = current + 1;
      await txn.put('seq', next);
      return next;
    });
    return new Response(JSON.stringify({ seq }), {
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function nextSequence(env) {
  const id      = env.SEQUENCER.idFromName('global');
  const stub    = env.SEQUENCER.get(id);
  const resp    = await stub.fetch('http://internal/next');
  const { seq } = await resp.json();
  return seq;
}

// ── Event Log — KV, ordered by sequence ──────────────────────────────────────
// Key: event:{seq_zero_padded_10}:{uuid}
// Zero-padding ensures lexicographic order == sequence order.
// GET /events?after=N fetches only keys beyond that cursor.

function buildEvent(seq, principal, type, data) {
  return {
    id:        crypto.randomUUID(),
    seq,                              // monotonic cursor; never reused
    timestamp: new Date().toISOString(),
    principal,                        // set by auth layer only
    type,
    ...data
  };
}

async function writeEvent(event, env) {
  const key = `event:${String(event.seq).padStart(10, '0')}:${event.id}`;
  await env.EVENTS.put(key, JSON.stringify(event));
}

async function readEventsSince(after, env) {
  const cursorKey = `event:${String(after).padStart(10, '0')}`;
  const list      = await env.EVENTS.list({ prefix: 'event:', startAfter: cursorKey });

  if (!list.keys.length) return [];

  const values = await Promise.all(list.keys.map(k => env.EVENTS.get(k.name)));
  return values
    .filter(Boolean)
    .map(v => JSON.parse(v))
    .filter(e => e.seq > after)
    .sort((a, b) => a.seq - b.seq);
}

// ── Path Sanitization — strict allowed-root ───────────────────────────────────
// Paths outside allowed roots are REJECTED — no silent redirect.
// Percent-decoded before all checks to block bypass via %2e%2e, ....// etc.

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH)    return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }

  const segments = decoded.split('/').map(s => s.trim());
  const safe     = segments.filter(s => s.length > 0 && s !== '.' && s !== '..');
  if (safe.length === 0) return null;

  // Reject control characters and null bytes
  if (safe.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;

  const joined = safe.join('/');

  // Strict: reject if not under a known root
  if (!ALLOWED_PREFIXES.some(p => joined.startsWith(p))) return null;

  return joined;
}

// ── GitHub — bounded retry, explicit failure ──────────────────────────────────

async function commitWithRetry(path, content, message, env) {
  for (let attempt = 1; attempt <= GITHUB_MAX_RETRIES; attempt++) {
    if (attempt > 1) await sleep(Math.random() * 400 + 200 * attempt);

    const result = await commitToGitHub(path, content, message, env);
    if (result.ok)             return result;
    if (result.status !== 409) return result; // non-retryable error; fail immediately
    // 409 = SHA conflict; re-fetch SHA on next iteration
  }

  // Loud failure after exhausting retries — caller should surface this clearly
  return {
    ok:     false,
    status: 409,
    detail: `GitHub write failed after ${GITHUB_MAX_RETRIES} attempts due to concurrent SHA conflict. ` +
            `Another principal may be writing to "${path}" simultaneously. ` +
            `Retry shortly or use a unique path.`
  };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = {
    'Authorization': `token ${env.GITHUB_TOKEN}`,
    'Content-Type':  'application/json',
    'User-Agent':    'sphera-bridge/0.0.3'
  };

  let sha;
  const existing = await fetch(url, { headers });
  if (existing.ok) {
    const data = await existing.json();
    sha = data.sha;
  }

  const body = {
    message,
    content: btoa(unescape(encodeURIComponent(content))),
    ...(sha ? { sha } : {})
  };

  const response = await fetch(url, { method: 'PUT', headers, body: JSON.stringify(body) });
  if (!response.ok) {
    const detail = await response.text();
    return { ok: false, status: response.status, detail };
  }

  const result = await response.json();
  return { ok: true, sha: result.content?.sha };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

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
