/**
 * SPHERA Bridge v0.0.1
 * 
 * Three operations only:
 *   POST /message       - post a message to the room
 *   POST /artifact      - publish a file artifact to GitHub
 *   GET  /events        - read the event log
 * 
 * Identity is derived from the Bearer token used to connect.
 * Callers never declare who they are — the bridge decides.
 */

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (err) {
      return json({ error: 'Internal error', detail: err.message }, 500);
    }
  }
};

// ── Routing ──────────────────────────────────────────────────────────────────

async function route(request, env) {
  // CORS preflight
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  // Auth: identity comes from the credential, never from the request body
  const principal = authenticate(request, env);
  if (!principal) {
    return json({ error: 'Unauthorized' }, 401);
  }

  const url = new URL(request.url);

  if (request.method === 'POST' && url.pathname === '/message') {
    return handlePostMessage(request, principal, env);
  }

  if (request.method === 'POST' && url.pathname === '/artifact') {
    return handlePublishArtifact(request, principal, env);
  }

  if (request.method === 'GET' && url.pathname === '/events') {
    return handleReadEvents(env);
  }

  return json({ error: 'Not found' }, 404);
}

// ── Auth ─────────────────────────────────────────────────────────────────────

function authenticate(request, env) {
  const header = request.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return null;

  const token = header.slice(7).trim();

  // Map tokens to principals server-side. Callers cannot self-declare identity.
  if (token === env.CLAUDE_KEY)   return 'claude';
  if (token === env.SOBA_KEY)     return 'soba';
  if (token === env.ARCHIVES_KEY) return 'archives';

  return null;
}

// ── Handlers ─────────────────────────────────────────────────────────────────

async function handlePostMessage(request, principal, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'Invalid JSON body' }, 400);
  }

  const { content } = body;
  if (!content || typeof content !== 'string' || !content.trim()) {
    return json({ error: '"content" is required and must be a non-empty string' }, 400);
  }

  const event = buildEvent(principal, 'message', { content: content.trim() });
  await appendEvent(event, env);

  return json({ ok: true, event_id: event.id }, 201);
}

async function handlePublishArtifact(request, principal, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'Invalid JSON body' }, 400);
  }

  const { path, content, commit_message } = body;

  if (!path || typeof path !== 'string') {
    return json({ error: '"path" is required (e.g. "notes/design.md")' }, 400);
  }
  if (!content || typeof content !== 'string') {
    return json({ error: '"content" is required' }, 400);
  }

  // Sanitize path: no leading slash, no path traversal
  const safePath = path.replace(/^\/+/, '').replace(/\.\./g, '');

  const message = commit_message?.trim() || `${principal}: publish ${safePath}`;
  const githubResult = await commitToGitHub(safePath, content, message, env);

  if (!githubResult.ok) {
    return json({ error: 'GitHub commit failed', detail: githubResult.detail }, 502);
  }

  const event = buildEvent(principal, 'artifact', {
    path: safePath,
    commit_message: message,
    sha: githubResult.sha
  });
  await appendEvent(event, env);

  return json({ ok: true, event_id: event.id, sha: githubResult.sha }, 201);
}

async function handleReadEvents(env) {
  const events = await loadEvents(env);
  return json({ events, count: events.length });
}

// ── Event log (Cloudflare KV) ─────────────────────────────────────────────────

function buildEvent(principal, type, data) {
  return {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    principal,   // set by auth layer, never by caller
    type,
    ...data
  };
}

async function loadEvents(env) {
  const raw = await env.EVENTS.get('log');
  return raw ? JSON.parse(raw) : [];
}

async function appendEvent(event, env) {
  const events = await loadEvents(env);
  events.push(event);
  await env.EVENTS.put('log', JSON.stringify(events));
}

// ── GitHub integration ────────────────────────────────────────────────────────

async function commitToGitHub(path, content, message, env) {
  const REPO = 'gabengangola-ui/sphera-room';
  const url = `https://api.github.com/repos/${REPO}/contents/${path}`;
  const headers = {
    'Authorization': `token ${env.GITHUB_TOKEN}`,
    'Content-Type': 'application/json',
    'User-Agent': 'sphera-bridge/0.0.1'
  };

  // Check if file already exists (need SHA to update)
  let sha;
  const existing = await fetch(url, { headers });
  if (existing.ok) {
    const data = await existing.json();
    sha = data.sha;
  }

  // Base64 encode content (handles unicode)
  const encoded = btoa(unescape(encodeURIComponent(content)));

  const body = { message, content: encoded };
  if (sha) body.sha = sha;

  const response = await fetch(url, {
    method: 'PUT',
    headers,
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    const detail = await response.text();
    return { ok: false, detail };
  }

  const result = await response.json();
  return { ok: true, sha: result.content?.sha };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type'
  };
}
