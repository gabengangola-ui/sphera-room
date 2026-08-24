/**
 * SPHERA Bridge v0.0.2
 *
 * Fixed from v0.0.1 (issues raised by Soba):
 *   - Concurrent-safe event log: each event is its own KV key
 *     (eliminates read-modify-write race under concurrent writes)
 *   - Proper path sanitization: percent-decode before checking,
 *     split/filter/rejoin to prevent traversal bypass via ....// etc.
 *   - Payload size limits: 10 KB messages, 100 KB artifacts
 *   - GitHub artifact race: retry with jitter on 409 SHA conflict
 *
 * Three operations only:
 *   POST /message       - post a message to the room
 *   POST /artifact      - publish a file artifact to GitHub
 *   GET  /events        - read the event log
 *
 * Identity is derived from the Bearer token. Callers never declare who they are.
 */

const MAX_MESSAGE_BYTES  = 10_000;   // 10 KB
const MAX_ARTIFACT_BYTES = 100_000;  // 100 KB
const MAX_PATH_LENGTH    = 200;
const GITHUB_MAX_RETRIES = 3;
const REPO               = 'gabengangola-ui/sphera-room';

// Allowed path prefixes — artifacts must live under one of these
const ALLOWED_PREFIXES = ['artifacts/', 'sessions/', 'bridge/'];

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (err) {
      return json({ error: 'Internal error', detail: err.message }, 500);
    }
  }
};

// ── Routing ───────────────────────────────────────────────────────────────────

async function route(request, env) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  const principal = authenticate(request, env);
  if (!principal) return json({ error: 'Unauthorized' }, 401);

  const { pathname } = new URL(request.url);

  if (request.method === 'POST' && pathname === '/message')  return handlePostMessage(request, principal, env);
  if (request.method === 'POST' && pathname === '/artifact') return handlePublishArtifact(request, principal, env);
  if (request.method === 'GET'  && pathname === '/events')   return handleReadEvents(env);

  return json({ error: 'Not found' }, 404);
}

// ── Auth ──────────────────────────────────────────────────────────────────────
// Identity is always derived from the credential, never from the request body.

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
  try { body = JSON.parse(raw); }
  catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { content } = body;
  if (!content || typeof content !== 'string' || !content.trim()) {
    return json({ error: '"content" must be a non-empty string' }, 400);
  }

  const event = buildEvent(principal, 'message', { content: content.trim() });
  await writeEvent(event, env);
  return json({ ok: true, event_id: event.id }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_ARTIFACT_BYTES + 2_000); // +2 KB JSON overhead
  if (raw === null) return json({ error: `Artifact exceeds size limit` }, 413);

  let body;
  try { body = JSON.parse(raw); }
  catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { path, content, commit_message } = body;

  if (!path    || typeof path    !== 'string') return json({ error: '"path" is required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" is required' }, 400);
  if (content.length > MAX_ARTIFACT_BYTES)     return json({ error: `Content exceeds ${MAX_ARTIFACT_BYTES} byte limit` }, 413);

  const safePath = sanitizePath(path);
  if (!safePath) return json({ error: 'Invalid or disallowed path' }, 400);

  const message = commit_message?.trim() || `${principal}: publish ${safePath}`;
  const result  = await commitWithRetry(safePath, content, message, env);
  if (!result.ok) return json({ error: 'GitHub commit failed', detail: result.detail }, 502);

  const event = buildEvent(principal, 'artifact', { path: safePath, commit_message: message, sha: result.sha });
  await writeEvent(event, env);
  return json({ ok: true, event_id: event.id, sha: result.sha }, 201);
}

async function handleReadEvents(env) {
  const events = await readAllEvents(env);
  return json({ events, count: events.length });
}

// ── Event log — concurrent-safe ───────────────────────────────────────────────
// Each event is its own KV entry: event:{ISO-timestamp}:{uuid}
// No read-modify-write → no lost updates when two principals write simultaneously.
// Reading lists all keys with the event: prefix, fetches in parallel, sorts by timestamp.

function buildEvent(principal, type, data) {
  const id = crypto.randomUUID();
  return { id, timestamp: new Date().toISOString(), principal, type, ...data };
}

async function writeEvent(event, env) {
  const key = `event:${event.timestamp}:${event.id}`;
  await env.EVENTS.put(key, JSON.stringify(event));
}

async function readAllEvents(env) {
  const list = await env.EVENTS.list({ prefix: 'event:' });
  if (!list.keys.length) return [];
  const values = await Promise.all(list.keys.map(k => env.EVENTS.get(k.name)));
  return values
    .filter(Boolean)
    .map(v => JSON.parse(v))
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
}

// ── Path sanitization ─────────────────────────────────────────────────────────
// 1. Reject if too long.
// 2. Percent-decode BEFORE any checks (prevents ....// and %2e%2e bypasses).
// 3. Split on /, filter empty segments, dots, and double-dots.
// 4. Reject control characters.
// 5. Enforce an allowed-prefix allowlist — unknown prefixes get artifacts/ prepended.

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH) return null;

  let decoded;
  try { decoded = decodeURIComponent(raw); }
  catch { return null; }

  const segments = decoded.split('/').map(s => s.trim());
  const safe = segments.filter(s => s.length > 0 && s !== '.' && s !== '..');
  if (safe.length === 0) return null;

  // Reject control characters and null bytes
  if (safe.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;

  const joined = safe.join('/');

  // Must start with an allowed prefix; if not, prefix with artifacts/
  if (!ALLOWED_PREFIXES.some(p => joined.startsWith(p))) {
    return `artifacts/${joined}`;
  }

  return joined;
}

// ── GitHub — retry on 409 SHA conflict ───────────────────────────────────────
// Concurrent artifact writes can both read the same SHA and then one fails.
// We retry up to GITHUB_MAX_RETRIES times with random jitter between attempts.

async function commitWithRetry(path, content, message, env) {
  for (let attempt = 1; attempt <= GITHUB_MAX_RETRIES; attempt++) {
    if (attempt > 1) await sleep(Math.random() * 400 + 200 * attempt);
    const result = await commitToGitHub(path, content, message, env);
    if (result.ok)             return result;
    if (result.status !== 409) return result; // non-retryable error
    // 409 = SHA conflict → loop to re-fetch SHA
  }
  return { ok: false, detail: 'Max retries exceeded on GitHub conflict' };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = {
    'Authorization': `token ${env.GITHUB_TOKEN}`,
    'Content-Type':  'application/json',
    'User-Agent':    'sphera-bridge/0.0.2'
  };

  // Fetch current SHA (required for updates; absent = new file)
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
  const reader  = request.body?.getReader();
  if (!reader) return '';
  const chunks  = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) { reader.cancel(); return null; }
    chunks.push(value);
  }
  return new TextDecoder().decode(
    chunks.reduce((acc, c) => { const n = new Uint8Array(acc.length + c.length); n.set(acc); n.set(c, acc.length); return n; }, new Uint8Array(0))
  );
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
