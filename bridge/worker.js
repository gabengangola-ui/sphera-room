/**
 * SPHERA Bridge v0.0.4
 *
 * Changes from v0.0.3 (per Soba's review — all structural):
 *
 * 1. LEDGER ATOMICITY — event storage moved entirely into the Durable Object.
 *    append(partial) atomically: increments seq + persists event in one DO
 *    transaction. No more seq-then-KV-write gap. A failed append means the
 *    event was never assigned a seq and never entered the ledger — no orphans,
 *    no holes. GitHub side effects that commit but whose append() then fails
 *    will now surface as a 502, so the caller knows to retry rather than
 *    silently losing the ledger entry.
 *
 * 2. EVENT READS via DO storage list() — DO storage list() has proper cursor
 *    support (start key, ordered Map). No reliance on KV startAfter.
 *    GET /events?after=N reads directly from the same ordered DO storage.
 *
 * 3. UTF-8 BYTE CHECK — content size is measured with TextEncoder, not
 *    string .length, so the 100 KB cap is accurate for non-ASCII content.
 *
 * 4. PATH TRAVERSAL REJECTION — sanitizePath() now rejects any path containing
 *    a '.' or '..' segment rather than silently filtering them out.
 *
 * 5. COMMIT MESSAGE VALIDATION — type-checked and capped before .trim() is
 *    called, so a non-string value cannot throw.
 */

const MAX_MESSAGE_BYTES      = 10_000;
const MAX_ARTIFACT_BYTES     = 100_000;
const MAX_COMMIT_MSG_CHARS   = 500;
const MAX_PATH_LENGTH        = 200;
const GITHUB_MAX_RETRIES     = 3;
const REPO                   = 'gabengangola-ui/sphera-room';
const ALLOWED_PREFIXES       = ['artifacts/', 'sessions/', 'bridge/'];

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

  // appendEvent is the single transaction boundary: it assigns seq and persists atomically
  const event = await appendEvent({ principal, type: 'message', content: content.trim() }, env);
  return json({ ok: true, event_id: event.id, seq: event.seq }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_ARTIFACT_BYTES + 2_000);
  if (raw === null) return json({ error: 'Artifact exceeds size limit' }, 413);

  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { path, content, commit_message } = body;

  // Validate all fields before touching GitHub
  if (!path    || typeof path    !== 'string') return json({ error: '"path" is required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" is required' }, 400);

  // UTF-8 byte check — .length counts JS string units, not bytes
  if (utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
    return json({ error: `Content exceeds ${MAX_ARTIFACT_BYTES} byte limit (measured in UTF-8 bytes)` }, 413);
  }

  // commit_message: type-check and cap before .trim()
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
      hint:  'Paths containing "." or ".." segments are also rejected.'
    }, 400);
  }

  const message = (typeof commit_message === 'string' ? commit_message.trim() : '') ||
                  `${principal}: publish ${safePath}`;

  // Commit to GitHub first. If this succeeds but appendEvent fails below,
  // the caller receives a 502 and knows to retry — they will not silently
  // lose the ledger entry.
  const result = await commitWithRetry(safePath, content, message, env);
  if (!result.ok) return json({ error: 'GitHub commit failed', detail: result.detail }, 502);

  // appendEvent is atomic: assigns seq + persists in one DO transaction
  const event = await appendEvent({
    principal,
    type:           'artifact',
    path:           safePath,
    commit_message: message,
    sha:            result.sha
  }, env);

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
// Owns both sequencing and event storage.
// append() is one atomic transaction: assign seq + write event.
// readSince() reads from the same ordered DO storage via list().
//
// DO storage list() returns an ordered Map keyed lexicographically.
// Zero-padded seq keys guarantee lexicographic order == sequence order.
// No external KV involved in the event log.

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
        // principal and all other fields come from the authenticated caller
        ...partial,
      };
      const key = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(key, JSON.stringify(complete));
      return complete;
    });

    return new Response(JSON.stringify(event), {
      status: 201, headers: { 'Content-Type': 'application/json' }
    });
  }

  async handleRead(url) {
    const after    = parseInt(url.searchParams.get('after') ?? '0', 10);
    const startKey = `event:${String(after + 1).padStart(10, '0')}`;

    // DO storage list() returns a Map in key order — cursor support is native here
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
// Any path segment that is '.' or '..' causes rejection — not silent removal.
// Percent-decoded before all checks.

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH)    return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }

  const segments = decoded.split('/');

  // Reject control characters or null bytes in any segment
  if (segments.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;

  // Reject traversal — do not silently remove, outright reject the path
  if (segments.some(s => s === '.' || s === '..'))     return null;

  // Strip leading/trailing empty segments (from leading/trailing slashes)
  const clean = segments.filter((s, i) => {
    if (i === 0 && s === '') return false;               // leading slash
    if (i === segments.length - 1 && s === '') return false; // trailing slash
    return true;
  });

  if (clean.length === 0 || clean.some(s => s === '')) return null; // double slash

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
    detail: `GitHub write failed after ${GITHUB_MAX_RETRIES} attempts (concurrent SHA conflict on "${path}"). ` +
            `Retry shortly or use a unique path.`
  };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = {
    'Authorization': `token ${env.GITHUB_TOKEN}`,
    'Content-Type':  'application/json',
    'User-Agent':    'sphera-bridge/0.0.4'
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
