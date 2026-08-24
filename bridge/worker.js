/**
 * SPHERA Bridge + MCP v0.0.8
 *
 * Adds: POST /mcp — SPHERA MCP V0 for Soba access
 *
 * Two tools only:
 *   read_events(after_cursor?)  — read room events since cursor
 *   post_message(content)       — post a message to the room
 *
 * Identity comes from the Bearer token on the MCP connection.
 * No principal field in tool arguments. Ever.
 * Both tools read/write the same EventLedger DO as the HTTP bridge.
 *
 * MCP transport: Streamable HTTP (JSON-RPC 2.0 over POST)
 * Auth: Authorization: Bearer {principal_key}
 * Protocol version: 2024-11-05
 *
 * All bridge endpoints from v0.0.7 are preserved unchanged.
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

  const url = new URL(request.url);

  // ── MCP endpoint — auth handled inside handleMCP ──────────────────────────
  if (url.pathname === '/mcp') return handleMCP(request, env);

  // ── Bridge endpoints — auth required ─────────────────────────────────────
  const principal = authenticate(request, env);
  if (!principal) return json({ error: 'Unauthorized' }, 401);

  if (request.method === 'POST' && url.pathname === '/message')  return handlePostMessage(request, principal, env);
  if (request.method === 'POST' && url.pathname === '/artifact') return handlePublishArtifact(request, principal, env);
  if (request.method === 'GET'  && url.pathname === '/events')   return handleReadEvents(url, env);

  const artifactMatch = url.pathname.match(/^\/artifact\/([a-f0-9-]{36})$/);
  if (request.method === 'GET' && artifactMatch) return handleGetArtifact(artifactMatch[1], env);

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

// ── MCP Handler ───────────────────────────────────────────────────────────────
// Implements MCP Streamable HTTP transport (JSON-RPC 2.0).
// Identity is derived from the Bearer token on the connection — same as bridge.
// Tool arguments never contain a principal field.

const MCP_TOOLS = [
  {
    name:        'read_events',
    description: 'Read messages and events from the SPHERA room ledger, in sequence order.',
    inputSchema: {
      type:       'object',
      properties: {
        after_cursor: {
          type:        'integer',
          description: 'Return only events with seq > after_cursor. Omit or use 0 for all events.',
          default:     0
        }
      },
      required: []
    }
  },
  {
    name:        'post_message',
    description: 'Post a message to the SPHERA room. Your identity is derived from your authenticated connection — do not include a principal field.',
    inputSchema: {
      type:       'object',
      properties: {
        content: {
          type:        'string',
          description: 'The message content to post.'
        }
      },
      required: ['content']
    }
  }
];

async function handleMCP(request, env) {
  if (request.method !== 'POST') {
    return new Response('Method Not Allowed', { status: 405 });
  }

  // Identity from the connection credential — never from the request body
  const principal = authenticate(request, env);
  if (!principal) {
    return mcpError(null, -32001, 'Unauthorized: provide a valid Bearer token');
  }

  let msg;
  try {
    msg = await request.json();
  } catch {
    return mcpError(null, -32700, 'Parse error: invalid JSON');
  }

  const { jsonrpc, id, method, params } = msg;

  if (jsonrpc !== '2.0') {
    return mcpError(id ?? null, -32600, 'Invalid Request: jsonrpc must be "2.0"');
  }

  // Notifications (no id) expect no response
  if (id === undefined || id === null) {
    return new Response(null, { status: 204 });
  }

  switch (method) {
    case 'initialize':
      return mcpResult(id, {
        protocolVersion: '2024-11-05',
        capabilities:    { tools: {} },
        serverInfo:      { name: 'sphera-mcp', version: '0.0.1' }
      });

    case 'tools/list':
      return mcpResult(id, { tools: MCP_TOOLS });

    case 'tools/call':
      return handleToolCall(id, params, principal, env);

    default:
      return mcpError(id, -32601, `Method not found: ${method}`);
  }
}

async function handleToolCall(id, params, principal, env) {
  const name = params?.name;
  const args  = params?.arguments ?? {};

  if (name === 'read_events') {
    const after = (typeof args.after_cursor === 'number' && args.after_cursor >= 0)
      ? Math.floor(args.after_cursor)
      : 0;

    const { events, count, cursor } = await doReadSince(after, env);

    const body = events.length === 0
      ? `No events after cursor ${after}.`
      : `SPHERA room — ${count} event(s), cursor now: ${cursor}\n\n` +
        events.map(formatEvent).join('\n─────\n');

    return mcpResult(id, { content: [{ type: 'text', text: body }] });
  }

  if (name === 'post_message') {
    const { content } = args;
    if (!content || typeof content !== 'string' || !content.trim()) {
      return mcpError(id, -32602, '"content" must be a non-empty string');
    }
    if (utf8ByteLength(content) > MAX_MESSAGE_BYTES) {
      return mcpError(id, -32602, `Content exceeds ${MAX_MESSAGE_BYTES} byte limit`);
    }
    // principal is from the auth layer — args cannot override it
    const event = await doAppend({ principal, type: 'message', content: content.trim() }, env);
    return mcpResult(id, {
      content: [{ type: 'text', text: `Posted. seq: ${event.seq}, id: ${event.id}, principal: ${event.principal}` }]
    });
  }

  return mcpError(id, -32601, `Unknown tool: ${name}`);
}

function formatEvent(e) {
  const header = `[seq:${e.seq}] [${e.timestamp}] [${e.principal}] [${e.type}]`;
  if (e.type === 'message')            return `${header}\n${e.content}`;
  if (e.type === 'artifact_intent')    return `${header}\npath: ${e.path}`;
  if (e.type === 'artifact_committed') return `${header}\npath: ${e.path}  sha: ${e.sha}`;
  if (e.type === 'artifact_failed')    return `${header}\npath: ${e.path}  error: ${e.error}`;
  return `${header}\n${JSON.stringify(e)}`;
}

function mcpResult(id, result) {
  return new Response(JSON.stringify({ jsonrpc: '2.0', id, result }), {
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

function mcpError(id, code, message) {
  return new Response(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }), {
    status:  200, // JSON-RPC errors use 200 with error body
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

// ── Bridge handlers (unchanged from v0.0.7) ───────────────────────────────────

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
    return json({ error: `Path rejected. Must begin with: ${ALLOWED_PREFIXES.join(', ')}` }, 400);
  }

  const callerKey = (typeof rawKey === 'string' && rawKey.trim()) ? rawKey.trim() : crypto.randomUUID();
  const ikey      = `${principal}:${callerKey}`;
  const message   = (typeof commit_message === 'string' ? commit_message.trim() : '') ||
                    `${principal}: publish ${safePath}`;

  const { intent, completion } = await doArtifactBegin({
    principal, path: safePath, commit_message: message, content, idempotency_key: ikey,
  }, env);

  if (completion) {
    if (completion.type === 'artifact_committed') {
      return json({ ok: true, idempotent: true, event_id: completion.id, seq: completion.seq,
                    intent_seq: intent.seq, sha: completion.sha }, 200);
    }
    if (completion.type === 'artifact_failed') {
      return json({ ok: false, error: 'This intent ended in artifact_failed.',
                    resolution: 'Use a new idempotency_key to create a fresh intent.',
                    failed_event_id: completion.id, failed_seq: completion.seq,
                    intent_seq: intent.seq }, 409);
    }
  }

  const result = await commitWithRetry(safePath, content, message, env);

  if (result.ok) {
    const committed = await doArtifactEnd({
      type: 'artifact_committed', principal, intent_id: intent.id,
      intent_seq: intent.seq, path: safePath, sha: result.sha,
    }, env);
    return json({ ok: true, event_id: committed.id, seq: committed.seq,
                  intent_seq: intent.seq, sha: result.sha }, 201);
  } else {
    const failed = await doArtifactEnd({
      type: 'artifact_failed', principal, intent_id: intent.id,
      intent_seq: intent.seq, path: safePath, error: result.detail,
    }, env);
    return json({ ok: false, error: 'GitHub sync failed. Intent is recorded.',
                  detail: result.detail, event_id: failed.id, seq: failed.seq,
                  intent_seq: intent.seq }, 502);
  }
}

async function handleReadEvents(url, env) {
  const afterParam = url.searchParams.get('after');
  const after      = afterParam !== null ? parseInt(afterParam, 10) : 0;
  if (isNaN(after) || after < 0) return json({ error: '"after" must be a non-negative integer' }, 400);
  const { events } = await doReadSince(after, env);
  return json({ events, count: events.length, cursor: events.at(-1)?.seq ?? after });
}

async function handleGetArtifact(intentId, env) {
  const result = await doGetArtifact(intentId, env);
  if (!result.intent) return json({ error: 'Intent not found', intent_id: intentId }, 404);
  return json(result);
}

// ── Durable Object — EventLedger (unchanged from v0.0.7) ─────────────────────

export class EventLedger {
  constructor(state) { this.state = state; }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      if (request.method === 'POST' && url.pathname === '/append')         return this.handleAppend(request);
      if (request.method === 'POST' && url.pathname === '/artifact-begin') return this.handleArtifactBegin(request);
      if (request.method === 'POST' && url.pathname === '/artifact-end')   return this.handleArtifactEnd(request);
      if (request.method === 'GET'  && url.pathname === '/events')         return this.handleRead(url);
      if (request.method === 'GET'  && url.pathname.startsWith('/artifact/')) return this.handleGetArtifact(url);
      return new Response('Not found', { status: 404 });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500, headers: { 'Content-Type': 'application/json' }
      });
    }
  }

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

  async handleArtifactBegin(request) {
    const { idempotency_key, content, ...partial } = await request.json();
    if (content && utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
      return new Response(JSON.stringify({ error: 'Content exceeds DO storage cap' }), {
        status: 413, headers: { 'Content-Type': 'application/json' }
      });
    }
    const existingId = await this.state.storage.get(`ikey:${idempotency_key}`);
    if (existingId) {
      const intent     = JSON.parse(await this.state.storage.get(`intent:${existingId}`));
      const compRaw    = await this.state.storage.get(`completion:${existingId}`);
      const completion = compRaw ? JSON.parse(compRaw) : null;
      return doResponse({ intent, completion, is_retry: true }, 200);
    }
    const intent = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(),
                         type: 'artifact_intent', idempotency_key, content, ...partial };
      const ekey = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(ekey,                      JSON.stringify(complete));
      await txn.put(`intent:${complete.id}`,   JSON.stringify(complete));
      await txn.put(`ikey:${idempotency_key}`, complete.id);
      return complete;
    });
    return doResponse({ intent, completion: null, is_retry: false }, 201);
  }

  async handleArtifactEnd(request) {
    const { intent_id, ...partial } = await request.json();
    const result = await this.state.storage.transaction(async (txn) => {
      const existing = await txn.get(`completion:${intent_id}`);
      if (existing) return { wrote: false, completion: JSON.parse(existing) };
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), intent_id, ...partial };
      const ekey     = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(ekey,                      JSON.stringify(complete));
      await txn.put(`completion:${intent_id}`, JSON.stringify(complete));
      return { wrote: true, completion: complete };
    });
    return doResponse(result.completion, result.wrote ? 201 : 200);
  }

  async handleRead(url) {
    const after    = parseInt(url.searchParams.get('after') ?? '0', 10);
    const startKey = `event:${String(after + 1).padStart(10, '0')}`;
    const entries  = await this.state.storage.list({ prefix: 'event:', start: startKey });
    const events   = [...entries.values()].map(v => JSON.parse(v));
    return doResponse({ events, count: events.length }, 200);
  }

  async handleGetArtifact(url) {
    const intentId  = url.pathname.replace('/artifact/', '');
    const intentRaw = await this.state.storage.get(`intent:${intentId}`);
    if (!intentRaw) return doResponse({ error: 'Not found' }, 404);
    const intent    = JSON.parse(intentRaw);
    const compRaw   = await this.state.storage.get(`completion:${intentId}`);
    const completion = compRaw ? JSON.parse(compRaw) : null;
    const state      = completion
      ? (completion.type === 'artifact_committed' ? 'committed' : 'failed')
      : 'pending';
    return doResponse({ intent, completion, state }, 200);
  }
}

function doResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' }
  });
}

// ── DO client helpers ─────────────────────────────────────────────────────────

function getLedger(env) { return env.LEDGER.get(env.LEDGER.idFromName('global')); }

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

async function doGetArtifact(intentId, env) {
  const r = await getLedger(env).fetch(`http://internal/artifact/${intentId}`);
  if (!r.ok) throw new Error(`Ledger get-artifact failed: ${await r.text()}`);
  return r.json();
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
  const clean = segments.filter((s, i) =>
    !(i === 0 && s === '') && !(i === segments.length - 1 && s === '')
  );
  if (clean.length === 0 || clean.some(s => s === '')) return null;
  const joined = clean.join('/');
  if (!ALLOWED_PREFIXES.some(p => joined.startsWith(p))) return null;
  return joined;
}

// ── GitHub ────────────────────────────────────────────────────────────────────

async function commitWithRetry(path, content, message, env) {
  for (let attempt = 1; attempt <= GITHUB_MAX_RETRIES; attempt++) {
    if (attempt > 1) await sleep(Math.random() * 400 + 200 * attempt);
    const result = await commitToGitHub(path, content, message, env);
    if (result.ok)             return result;
    if (result.status !== 409) return result;
  }
  return { ok: false, status: 409,
           detail: `GitHub write failed after ${GITHUB_MAX_RETRIES} attempts (SHA conflict on "${path}").` };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = { 'Authorization': `token ${env.GITHUB_TOKEN}`, 'Content-Type': 'application/json',
                    'User-Agent': 'sphera-bridge/0.0.8' };
  let sha;
  const existing = await fetch(url, { headers });
  if (existing.ok) { const d = await existing.json(); sha = d.sha; }
  const body     = { message, content: btoa(unescape(encodeURIComponent(content))), ...(sha ? { sha } : {}) };
  const response = await fetch(url, { method: 'PUT', headers, body: JSON.stringify(body) });
  if (!response.ok) return { ok: false, status: response.status, detail: await response.text() };
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
    status, headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type'
  };
}
