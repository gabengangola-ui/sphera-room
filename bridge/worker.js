/**
 * SPHERA Bridge v0.0.9
 *
 * Changes from v0.0.8:
 *
 * 1. DECISION QUEUE implemented (typed endpoints, not through /message)
 *    Lifecycle: pending → approved → claimed → consumed
 *               pending → rejected (terminal)
 *               pending → expired (terminal)
 *               claimed → execution_failed → approved (retryable)
 *
 * 2. ATOMIC CLAIM — entire check-and-set inside DO storage.transaction()
 *    Concurrent claim attempts: only one wins, atomically.
 *
 * 3. CANONICAL JSON + SHA-256 payload digest
 *    Sorted keys, UTF-8, no whitespace. Parameter change = digest change = invalid approval.
 *
 * 4. ARCIDES identity — canonical name is 'arcides', not 'archives'
 *    ARCHIVES_KEY env var maps to 'arcides' principal.
 *
 * 5. TYPED VALIDATION — decision events have dedicated endpoints with strict schema.
 *    Generic /message path cannot create decision events.
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

  const url    = new URL(request.url);
  const method = request.method;

  // MCP — auth handled inside
  if (url.pathname === '/mcp') return handleMCP(request, env);

  // All other routes require auth
  const principal = authenticate(request, env);
  if (!principal) return json({ error: 'Unauthorized' }, 401);

  // ── Bridge endpoints ──────────────────────────────────────────────────────
  if (method === 'POST' && url.pathname === '/message')  return handlePostMessage(request, principal, env);
  if (method === 'POST' && url.pathname === '/artifact') return handlePublishArtifact(request, principal, env);
  if (method === 'GET'  && url.pathname === '/events')   return handleReadEvents(url, env);

  const artifactMatch = url.pathname.match(/^\/artifact\/([a-f0-9-]{36})$/);
  if (method === 'GET' && artifactMatch) return handleGetArtifact(artifactMatch[1], env);

  // ── Decision endpoints ────────────────────────────────────────────────────
  if (method === 'POST' && url.pathname === '/decision')
    return handleDecisionRequest(request, principal, env);

  const decMatch = url.pathname.match(/^\/decision\/([a-f0-9-]{36})(\/\w+)?$/);
  if (decMatch) {
    const requestId = decMatch[1];
    const action    = decMatch[2] || '';
    if (method === 'GET'  && action === '')          return handleGetDecision(requestId, env);
    if (method === 'POST' && action === '/approve')  return handleDecisionApprove(request, principal, requestId, env);
    if (method === 'POST' && action === '/reject')   return handleDecisionReject(request, principal, requestId, env);
    if (method === 'POST' && action === '/claim')    return handleDecisionClaim(request, principal, requestId, env);
    if (method === 'POST' && action === '/consume')  return handleDecisionConsume(request, principal, requestId, env);
    if (method === 'POST' && action === '/fail')     return handleDecisionFail(request, principal, requestId, env);
  }

  return json({ error: 'Not found' }, 404);
}

// ── Auth ──────────────────────────────────────────────────────────────────────
// Canonical identity for Archives is 'arcides'. Never accepted from caller content.

function authenticate(request, env) {
  const header = request.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return null;
  const token = header.slice(7).trim();
  if (token === env.CLAUDE_KEY)   return 'claude';
  if (token === env.SOBA_KEY)     return 'soba';
  if (token === env.ARCHIVES_KEY) return 'arcides';  // canonical: arcides
  return null;
}

// ── Decision handlers ─────────────────────────────────────────────────────────

async function handleDecisionRequest(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  if (!raw) return json({ error: 'Empty body' }, 400);
  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON' }, 400); }

  const { scope, target, params, deadline } = body;
  if (!scope  || typeof scope  !== 'string') return json({ error: '"scope" required'  }, 400);
  if (!target || typeof target !== 'string') return json({ error: '"target" required' }, 400);
  if (!params || typeof params !== 'object') return json({ error: '"params" must be an object' }, 400);

  // Bind digest to exact action — parameter change = new request required
  const action         = { scope, target, principal, params };
  const payload_digest = await sha256(canonicalJSON(action));

  const deadline_ts = deadline ? new Date(deadline).toISOString() : null;
  if (deadline && isNaN(Date.parse(deadline))) {
    return json({ error: '"deadline" must be a valid ISO timestamp' }, 400);
  }

  const event = await doDecisionRequest({
    principal, scope, target, params, payload_digest, deadline: deadline_ts
  }, env);

  return json({ ok: true, request_id: event.request_id, seq: event.seq, payload_digest }, 201);
}

async function handleDecisionApprove(request, principal, requestId, env) {
  // Only arcides may approve
  if (principal !== 'arcides') return json({ error: 'Forbidden: only arcides may approve decisions' }, 403);

  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  let body = {};
  try { if (raw) body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON' }, 400); }

  const { note } = body;
  const result = await doDecisionApprove({ principal, request_id: requestId, note: note || null }, env);
  if (!result.ok) return json({ error: result.error }, result.status || 409);
  return json({ ok: true, seq: result.seq, request_id: requestId }, 201);
}

async function handleDecisionReject(request, principal, requestId, env) {
  if (principal !== 'arcides') return json({ error: 'Forbidden: only arcides may reject decisions' }, 403);

  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  let body = {};
  try { if (raw) body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON' }, 400); }

  const { reason } = body;
  const result = await doDecisionReject({ principal, request_id: requestId, reason: reason || null }, env);
  if (!result.ok) return json({ error: result.error }, result.status || 409);
  return json({ ok: true, seq: result.seq, request_id: requestId }, 201);
}

async function handleDecisionClaim(request, principal, requestId, env) {
  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  let body = {};
  try { if (raw) body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON' }, 400); }

  // Caller must supply the params they intend to act on — digest verified atomically in DO
  const { params } = body;
  if (!params || typeof params !== 'object') {
    return json({ error: '"params" required to verify payload digest' }, 400);
  }

  const result = await doDecisionClaim({ principal, request_id: requestId, params }, env);
  if (!result.ok) return json({ error: result.error }, result.status || 409);
  return json({ ok: true, seq: result.seq, request_id: requestId }, 201);
}

async function handleDecisionConsume(request, principal, requestId, env) {
  const result = await doDecisionConsume({ principal, request_id: requestId }, env);
  if (!result.ok) return json({ error: result.error }, result.status || 409);
  return json({ ok: true, seq: result.seq, request_id: requestId }, 201);
}

async function handleDecisionFail(request, principal, requestId, env) {
  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  let body = {};
  try { if (raw) body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON' }, 400); }

  const { error: reason } = body;
  const result = await doDecisionFail({ principal, request_id: requestId, reason: reason || 'unknown' }, env);
  if (!result.ok) return json({ error: result.error }, result.status || 409);
  return json({ ok: true, seq: result.seq, request_id: requestId, status: 'approved' }, 201);
}

async function handleGetDecision(requestId, env) {
  const result = await doGetDecision(requestId, env);
  if (!result.found) return json({ error: 'Decision not found', request_id: requestId }, 404);
  return json(result);
}

// ── MCP (two tools: read_events, post_message) ────────────────────────────────

const MCP_TOOLS = [
  {
    name: 'read_events',
    description: 'Read messages and events from the SPHERA room ledger.',
    inputSchema: {
      type: 'object',
      properties: {
        after_cursor: { type: 'integer', description: 'Return events with seq > after_cursor. Default 0.', default: 0 }
      },
      required: []
    }
  },
  {
    name: 'post_message',
    description: 'Post a message to the SPHERA room. Identity from authenticated connection — no principal field.',
    inputSchema: {
      type: 'object',
      properties: {
        content: { type: 'string', description: 'Message content.' }
      },
      required: ['content']
    }
  }
];

async function handleMCP(request, env) {
  if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

  const principal = authenticate(request, env);
  if (!principal) return mcpError(null, -32001, 'Unauthorized: provide a valid Bearer token');

  let msg;
  try { msg = await request.json(); } catch { return mcpError(null, -32700, 'Parse error'); }

  const { jsonrpc, id, method, params } = msg;
  if (jsonrpc !== '2.0') return mcpError(id ?? null, -32600, 'Invalid Request');
  if (id === undefined || id === null) return new Response(null, { status: 204 });

  switch (method) {
    case 'initialize':
      return mcpResult(id, {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'sphera-mcp', version: '0.0.9' }
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
  const args = params?.arguments ?? {};

  if (name === 'read_events') {
    const after  = typeof args.after_cursor === 'number' && args.after_cursor >= 0 ? Math.floor(args.after_cursor) : 0;
    const { events, count, cursor } = await doReadSince(after, env);
    const body   = events.length === 0
      ? `No events after cursor ${after}.`
      : `SPHERA room — ${count} event(s), cursor: ${cursor}\n\n` + events.map(formatEvent).join('\n─────\n');
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
    const event = await doAppend({ principal, type: 'message', content: content.trim() }, env);
    return mcpResult(id, { content: [{ type: 'text', text: `Posted. seq: ${event.seq}, id: ${event.id}` }] });
  }

  return mcpError(id, -32601, `Unknown tool: ${name}`);
}

function formatEvent(e) {
  const h = `[seq:${e.seq}] [${e.timestamp}] [${e.principal}] [${e.type}]`;
  if (e.type === 'message')              return `${h}\n${e.content}`;
  if (e.type === 'artifact_intent')      return `${h}\npath: ${e.path}`;
  if (e.type === 'artifact_committed')   return `${h}\npath: ${e.path}  sha: ${e.sha}`;
  if (e.type === 'artifact_failed')      return `${h}\npath: ${e.path}  error: ${e.error}`;
  if (e.type === 'decision_requested')   return `${h}\nscope: ${e.scope}  digest: ${e.payload_digest}`;
  if (e.type === 'decision_approved')    return `${h}\nrequest_id: ${e.request_id}`;
  if (e.type === 'decision_rejected')    return `${h}\nrequest_id: ${e.request_id}  reason: ${e.reason}`;
  if (e.type === 'decision_claimed')     return `${h}\nrequest_id: ${e.request_id}`;
  if (e.type === 'decision_consumed')    return `${h}\nrequest_id: ${e.request_id}`;
  if (e.type === 'execution_failed')     return `${h}\nrequest_id: ${e.request_id}  reason: ${e.reason}`;
  return `${h}\n${JSON.stringify(e)}`;
}

function mcpResult(id, result) {
  return new Response(JSON.stringify({ jsonrpc: '2.0', id, result }), {
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}
function mcpError(id, code, message) {
  return new Response(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }), {
    status: 200, headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

// ── Bridge handlers ───────────────────────────────────────────────────────────

async function handlePostMessage(request, principal, env) {
  const raw = await readBodyWithLimit(request, MAX_MESSAGE_BYTES);
  if (raw === null) return json({ error: `Message exceeds ${MAX_MESSAGE_BYTES} byte limit` }, 413);
  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: 'Invalid JSON body' }, 400); }
  const { content } = body;
  if (!content || typeof content !== 'string' || !content.trim()) {
    return json({ error: '"content" must be a non-empty string' }, 400);
  }
  // Guard: message content cannot smuggle decision event types
  let parsed;
  try { parsed = JSON.parse(content); } catch { parsed = null; }
  if (parsed && typeof parsed === 'object' && String(parsed.type || '').startsWith('decision_')) {
    return json({ error: 'Decision events must use /decision endpoints, not /message' }, 400);
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
  if (!path    || typeof path    !== 'string') return json({ error: '"path" required'    }, 400);
  if (!content || typeof content !== 'string') return json({ error: '"content" required' }, 400);
  if (utf8ByteLength(content) > MAX_ARTIFACT_BYTES) return json({ error: 'Content too large' }, 413);
  if (commit_message !== undefined && commit_message !== null) {
    if (typeof commit_message !== 'string') return json({ error: '"commit_message" must be a string' }, 400);
    if (commit_message.length > MAX_COMMIT_MSG_CHARS) return json({ error: '"commit_message" too long' }, 400);
  }
  const safePath = sanitizePath(path);
  if (!safePath) return json({ error: `Path rejected. Must begin with: ${ALLOWED_PREFIXES.join(', ')}` }, 400);

  const callerKey = (typeof rawKey === 'string' && rawKey.trim()) ? rawKey.trim() : crypto.randomUUID();
  const ikey      = `${principal}:${callerKey}`;
  const message   = (typeof commit_message === 'string' ? commit_message.trim() : '') || `${principal}: publish ${safePath}`;

  const { intent, completion } = await doArtifactBegin({ principal, path: safePath, commit_message: message, content, idempotency_key: ikey }, env);

  if (completion) {
    if (completion.type === 'artifact_committed') {
      return json({ ok: true, idempotent: true, event_id: completion.id, seq: completion.seq, intent_seq: intent.seq, sha: completion.sha }, 200);
    }
    return json({ ok: false, error: 'Intent ended in artifact_failed. Use new idempotency_key to retry.', intent_seq: intent.seq }, 409);
  }

  const result = await commitWithRetry(safePath, content, message, env);

  if (result.ok) {
    const committed = await doArtifactEnd({ type: 'artifact_committed', principal, intent_id: intent.id, intent_seq: intent.seq, path: safePath, sha: result.sha }, env);
    return json({ ok: true, event_id: committed.id, seq: committed.seq, intent_seq: intent.seq, sha: result.sha }, 201);
  } else {
    const failed = await doArtifactEnd({ type: 'artifact_failed', principal, intent_id: intent.id, intent_seq: intent.seq, path: safePath, error: result.detail }, env);
    return json({ ok: false, error: 'GitHub sync failed. Intent recorded. Use new idempotency_key to retry.', event_id: failed.id, seq: failed.seq, intent_seq: intent.seq }, 502);
  }
}

async function handleReadEvents(url, env) {
  const afterParam = url.searchParams.get('after');
  const after      = afterParam !== null ? parseInt(afterParam, 10) : 0;
  if (isNaN(after) || after < 0) return json({ error: '"after" must be non-negative integer' }, 400);
  const { events } = await doReadSince(after, env);
  return json({ events, count: events.length, cursor: events.at(-1)?.seq ?? after });
}

async function handleGetArtifact(intentId, env) {
  const result = await doGetArtifact(intentId, env);
  if (!result.intent) return json({ error: 'Intent not found', intent_id: intentId }, 404);
  return json(result);
}

// ── Durable Object — EventLedger ─────────────────────────────────────────────

export class EventLedger {
  constructor(state) { this.state = state; }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      const m = request.method;
      const p = url.pathname;
      if (m === 'POST' && p === '/append')                    return this.handleAppend(request);
      if (m === 'POST' && p === '/artifact-begin')            return this.handleArtifactBegin(request);
      if (m === 'POST' && p === '/artifact-end')              return this.handleArtifactEnd(request);
      if (m === 'GET'  && p === '/events')                    return this.handleRead(url);
      if (m === 'GET'  && p.startsWith('/artifact/'))         return this.handleGetArtifact(url);
      // Decision DO handlers
      if (m === 'POST' && p === '/decision/request')          return this.handleDecisionRequest(request);
      if (m === 'POST' && p === '/decision/approve')          return this.handleDecisionApprove(request);
      if (m === 'POST' && p === '/decision/reject')           return this.handleDecisionReject(request);
      if (m === 'POST' && p === '/decision/claim')            return this.handleDecisionClaim(request);
      if (m === 'POST' && p === '/decision/consume')          return this.handleDecisionConsume(request);
      if (m === 'POST' && p === '/decision/fail')             return this.handleDecisionFail(request);
      if (m === 'GET'  && p.startsWith('/decision/get/'))     return this.handleGetDecision(url);
      return new Response('Not found', { status: 404 });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), { status: 500, headers: { 'Content-Type': 'application/json' } });
    }
  }

  // ── Generic append (messages) ───────────────────────────────────────────────
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

  // ── Artifact handlers ───────────────────────────────────────────────────────
  async handleArtifactBegin(request) {
    const { idempotency_key, content, ...partial } = await request.json();
    if (content && utf8ByteLength(content) > MAX_ARTIFACT_BYTES) {
      return new Response(JSON.stringify({ error: 'Content too large' }), { status: 413, headers: { 'Content-Type': 'application/json' } });
    }
    const existingId = await this.state.storage.get(`ikey:${idempotency_key}`);
    if (existingId) {
      const intent     = JSON.parse(await this.state.storage.get(`intent:${existingId}`));
      const compRaw    = await this.state.storage.get(`completion:${existingId}`);
      return doResponse({ intent, completion: compRaw ? JSON.parse(compRaw) : null, is_retry: true }, 200);
    }
    const intent = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), type: 'artifact_intent', idempotency_key, content, ...partial };
      const ekey     = `event:${String(seq).padStart(10, '0')}`;
      await txn.put('__seq__', seq);
      await txn.put(ekey, JSON.stringify(complete));
      await txn.put(`intent:${complete.id}`, JSON.stringify(complete));
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
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
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
    const intentId   = url.pathname.replace('/artifact/', '');
    const intentRaw  = await this.state.storage.get(`intent:${intentId}`);
    if (!intentRaw) return doResponse({ error: 'Not found' }, 404);
    const intent     = JSON.parse(intentRaw);
    const compRaw    = await this.state.storage.get(`completion:${intentId}`);
    const completion = compRaw ? JSON.parse(compRaw) : null;
    const state      = completion ? (completion.type === 'artifact_committed' ? 'committed' : 'failed') : 'pending';
    return doResponse({ intent, completion, state }, 200);
  }

  // ── Decision handlers (all state transitions in DO) ─────────────────────────

  async handleDecisionRequest(request) {
    const data = await request.json();
    const { principal, scope, target, params, payload_digest, deadline } = data;

    const request_id = crypto.randomUUID();
    const event      = await this.state.storage.transaction(async (txn) => {
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(),
        type: 'decision_requested', request_id, principal, scope, target, params, payload_digest, deadline
      };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision:${request_id}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'pending');
      return complete;
    });
    return doResponse({ ok: true, request_id, seq: event.seq }, 201);
  }

  async handleDecisionApprove(request) {
    const { principal, request_id, note } = await request.json();
    // principal check is enforced at HTTP layer (arcides only gets here)
    const result = await this.state.storage.transaction(async (txn) => {
      const status = await txn.get(`decision_status:${request_id}`);
      if (!status)           return { ok: false, error: 'Decision not found',   status: 404 };
      if (status !== 'pending') return { ok: false, error: `Cannot approve: status is '${status}'`, status: 409 };
      // Check deadline
      const decRaw = await txn.get(`decision:${request_id}`);
      const dec    = JSON.parse(decRaw);
      if (dec.deadline && new Date() > new Date(dec.deadline)) {
        await txn.put(`decision_status:${request_id}`, 'expired');
        return { ok: false, error: 'Decision has expired', status: 410 };
      }
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = {
        id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(),
        type: 'decision_approved', request_id, principal, note,
        bound_digest: dec.payload_digest  // bind approval to exact digest
      };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision_approval:${request_id}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'approved');
      return { ok: true, seq };
    });
    return doResponse(result, result.ok ? 201 : (result.status || 409));
  }

  async handleDecisionReject(request) {
    const { principal, request_id, reason } = await request.json();
    const result = await this.state.storage.transaction(async (txn) => {
      const status = await txn.get(`decision_status:${request_id}`);
      if (!status)              return { ok: false, error: 'Decision not found',   status: 404 };
      if (status !== 'pending') return { ok: false, error: `Cannot reject: status is '${status}'`, status: 409 };
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), type: 'decision_rejected', request_id, principal, reason };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'rejected');
      return { ok: true, seq };
    });
    return doResponse(result, result.ok ? 201 : (result.status || 409));
  }

  async handleDecisionClaim(request) {
    // ATOMIC: verify approval, digest match, caller authority, unused state — all inside transaction
    const { principal, request_id, params } = await request.json();
    const result = await this.state.storage.transaction(async (txn) => {
      const status = await txn.get(`decision_status:${request_id}`);
      if (!status)              return { ok: false, error: 'Decision not found',   status: 404 };
      if (status !== 'approved') return { ok: false, error: `Cannot claim: status is '${status}'`, status: 409 };

      const decRaw      = await txn.get(`decision:${request_id}`);
      const dec         = JSON.parse(decRaw);
      const approvalRaw = await txn.get(`decision_approval:${request_id}`);
      const approval    = JSON.parse(approvalRaw);

      // Verify caller is the original requester
      if (dec.principal !== principal) {
        return { ok: false, error: 'Only the requesting principal may claim this decision', status: 403 };
      }

      // Verify deadline not passed
      if (dec.deadline && new Date() > new Date(dec.deadline)) {
        await txn.put(`decision_status:${request_id}`, 'expired');
        return { ok: false, error: 'Decision has expired', status: 410 };
      }

      // CRITICAL: verify payload digest matches exactly — prevents TOCTOU
      const claimAction   = { scope: dec.scope, target: dec.target, principal, params };
      // Note: digest computed async outside transaction; we compare to bound_digest from approval
      // Caller must supply params that hash to the approved digest
      // (async hashing happens before this call in doDecisionClaim)
      if (params.__verified_digest !== approval.bound_digest) {
        return { ok: false, error: 'Payload digest mismatch: params do not match approved action', status: 422 };
      }

      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), type: 'decision_claimed', request_id, principal };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision_claim:${request_id}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'claimed');
      return { ok: true, seq };
    });
    return doResponse(result, result.ok ? 201 : (result.status || 409));
  }

  async handleDecisionConsume(request) {
    const { principal, request_id } = await request.json();
    const result = await this.state.storage.transaction(async (txn) => {
      const status = await txn.get(`decision_status:${request_id}`);
      if (!status)             return { ok: false, error: 'Decision not found',   status: 404 };
      if (status !== 'claimed') return { ok: false, error: `Cannot consume: status is '${status}'`, status: 409 };
      const decRaw = await txn.get(`decision:${request_id}`);
      const dec    = JSON.parse(decRaw);
      if (dec.principal !== principal) return { ok: false, error: 'Only the requesting principal may consume', status: 403 };
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), type: 'decision_consumed', request_id, principal };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'consumed');
      return { ok: true, seq };
    });
    return doResponse(result, result.ok ? 201 : (result.status || 409));
  }

  async handleDecisionFail(request) {
    // execution_failed: reset claimed → approved for retry
    const { principal, request_id, reason } = await request.json();
    const result = await this.state.storage.transaction(async (txn) => {
      const status = await txn.get(`decision_status:${request_id}`);
      if (!status)             return { ok: false, error: 'Decision not found',   status: 404 };
      if (status !== 'claimed') return { ok: false, error: `Cannot fail: status is '${status}'`, status: 409 };
      const decRaw = await txn.get(`decision:${request_id}`);
      const dec    = JSON.parse(decRaw);
      if (dec.principal !== principal) return { ok: false, error: 'Only the requesting principal may report failure', status: 403 };
      const seq      = ((await txn.get('__seq__')) ?? 0) + 1;
      const complete = { id: crypto.randomUUID(), seq, timestamp: new Date().toISOString(), type: 'execution_failed', request_id, principal, reason };
      await txn.put('__seq__', seq);
      await txn.put(`event:${String(seq).padStart(10, '0')}`, JSON.stringify(complete));
      await txn.put(`decision_status:${request_id}`, 'approved'); // reset for retry
      return { ok: true, seq };
    });
    return doResponse(result, result.ok ? 201 : (result.status || 409));
  }

  async handleGetDecision(url) {
    const request_id = url.pathname.replace('/decision/get/', '');
    const decRaw     = await this.state.storage.get(`decision:${request_id}`);
    if (!decRaw) return doResponse({ found: false }, 404);
    const decision   = JSON.parse(decRaw);
    const status     = await this.state.storage.get(`decision_status:${request_id}`);
    const appRaw     = await this.state.storage.get(`decision_approval:${request_id}`);
    const claimRaw   = await this.state.storage.get(`decision_claim:${request_id}`);
    return doResponse({
      found: true, decision, status,
      approval: appRaw ? JSON.parse(appRaw) : null,
      claim:    claimRaw ? JSON.parse(claimRaw) : null
    }, 200);
  }
}

function doResponse(data, status) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

// ── DO client helpers ─────────────────────────────────────────────────────────

function getLedger(env) { return env.LEDGER.get(env.LEDGER.idFromName('global')); }

const doFetch = async (path, method, body, env) => {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await getLedger(env).fetch(`http://internal${path}`, opts);
  if (!r.ok) throw new Error(`DO ${path} failed: ${await r.text()}`);
  return r.json();
};

async function doAppend(partial, env)         { return doFetch('/append', 'POST', partial, env); }
async function doArtifactBegin(p, env)        { return doFetch('/artifact-begin', 'POST', p, env); }
async function doArtifactEnd(p, env)          { return doFetch('/artifact-end', 'POST', p, env); }
async function doReadSince(after, env)        { return doFetch(`/events?after=${after}`, 'GET', null, env); }
async function doGetArtifact(id, env)         { return doFetch(`/artifact/${id}`, 'GET', null, env); }
async function doDecisionRequest(p, env)      { return doFetch('/decision/request', 'POST', p, env); }
async function doDecisionApprove(p, env)      { return doFetch('/decision/approve', 'POST', p, env); }
async function doDecisionReject(p, env)       { return doFetch('/decision/reject', 'POST', p, env); }
async function doDecisionConsume(p, env)      { return doFetch('/decision/consume', 'POST', p, env); }
async function doDecisionFail(p, env)         { return doFetch('/decision/fail', 'POST', p, env); }
async function doGetDecision(id, env)         { return doFetch(`/decision/get/${id}`, 'GET', null, env); }

// Claim: compute digest externally then inject as verified_digest sentinel inside params
async function doDecisionClaim({ principal, request_id, params }, env) {
  // Get the decision to find the approved digest
  const state = await doGetDecision(request_id, env);
  if (!state.found)     throw new Error('Decision not found');
  if (!state.approval)  throw new Error('Decision not yet approved');

  const decision = state.decision;
  const action   = { scope: decision.scope, target: decision.target, principal, params };
  const digest   = await sha256(canonicalJSON(action));

  // Inject verified digest as sentinel so DO can compare atomically
  const augmented = { ...params, __verified_digest: digest };
  return doFetch('/decision/claim', 'POST', { principal, request_id, params: augmented }, env);
}

// ── Canonical JSON + SHA-256 ──────────────────────────────────────────────────

function canonicalJSON(obj) {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return '[' + obj.map(canonicalJSON).join(',') + ']';
  const keys = Object.keys(obj).sort();
  return '{' + keys.map(k => `${JSON.stringify(k)}:${canonicalJSON(obj[k])}`).join(',') + '}';
}

async function sha256(str) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

// ── Path sanitization ─────────────────────────────────────────────────────────

function sanitizePath(raw) {
  if (!raw || typeof raw !== 'string') return null;
  if (raw.length > MAX_PATH_LENGTH) return null;
  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }
  const segments = decoded.split('/');
  if (segments.some(s => /[\x00-\x1f\x7f]/.test(s))) return null;
  if (segments.some(s => s === '.' || s === '..')) return null;
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
    if (result.ok) return result;
    if (result.status !== 409) return result;
  }
  return { ok: false, status: 409, detail: `GitHub write failed after ${GITHUB_MAX_RETRIES} attempts.` };
}

async function commitToGitHub(path, content, message, env) {
  const url     = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = { 'Authorization': `token ${env.GITHUB_TOKEN}`, 'Content-Type': 'application/json', 'User-Agent': 'sphera-bridge/0.0.9' };
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
  return new Response(JSON.stringify(data, null, 2), { status, headers: { 'Content-Type': 'application/json', ...corsHeaders() } });
}

function corsHeaders() {
  return { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Authorization, Content-Type' };
}
