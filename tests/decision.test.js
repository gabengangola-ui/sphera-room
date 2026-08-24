/**
 * SPHERA Decision Queue — Adversarial Tests
 * Seven cases required by Soba before merge.
 * Usage: BRIDGE_URL=https://... CLAUDE_KEY=... ARCIDES_KEY=... node tests/decision.test.js
 */

const BASE        = process.env.BRIDGE_URL  || 'http://localhost:8787';
const CLAUDE_KEY  = process.env.CLAUDE_KEY  || '';
const SOBA_KEY    = process.env.SOBA_KEY    || '';
const ARCIDES_KEY = process.env.ARCIDES_KEY || '';

let passed = 0; let failed = 0;

async function call(path, method, body, key) {
  const opts = { method, headers: { 'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(`${BASE}${path}`, opts);
  let data; try { data = await r.json(); } catch { data = {}; }
  return { status: r.status, data };
}

function assert(label, condition, detail = '') {
  if (condition) { console.log(`  ✓ ${label}`); passed++; }
  else           { console.error(`  ✗ ${label}${detail ? ': ' + detail : ''}`); failed++; }
}

async function newDecision(params = {}) {
  return call('/decision', 'POST', {
    scope: params.scope || 'deploy bridge', target: params.target || 'cloudflare',
    params: params.params || { version: 'v0.0.9' },
    ...(params.deadline ? { deadline: params.deadline } : {})
  }, CLAUDE_KEY);
}

// 1. Concurrent claim
async function test1() {
  console.log('\nTEST 1: Concurrent claim — only one must win');
  const { data: req } = await newDecision();
  await call(`/decision/${req.request_id}/approve`, 'POST', {}, ARCIDES_KEY);
  const [r1, r2] = await Promise.all([
    call(`/decision/${req.request_id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY),
    call(`/decision/${req.request_id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY),
  ]);
  assert('exactly one claim wins',      [r1,r2].filter(r=>r.status===201).length === 1);
  assert('exactly one claim conflicts', [r1,r2].filter(r=>r.status===409).length === 1);
}

// 2. Digest mismatch
async function test2() {
  console.log('\nTEST 2: Digest mismatch — mutated params rejected');
  const { data: req } = await newDecision({ params: { version: 'v0.0.9' } });
  await call(`/decision/${req.request_id}/approve`, 'POST', {}, ARCIDES_KEY);
  const { status } = await call(`/decision/${req.request_id}/claim`, 'POST', { params: { version: 'MUTATED' } }, CLAUDE_KEY);
  assert('mutated params → 422', status === 422, `got ${status}`);
}

// 3. Replay after consume
async function test3() {
  console.log('\nTEST 3: Replay after consume — second claim rejected');
  const { data: req } = await newDecision();
  const id = req.request_id;
  await call(`/decision/${id}/approve`, 'POST', {}, ARCIDES_KEY);
  await call(`/decision/${id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY);
  await call(`/decision/${id}/consume`, 'POST', {}, CLAUDE_KEY);
  const { status } = await call(`/decision/${id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY);
  assert('second claim → 409', status === 409, `got ${status}`);
}

// 4. Expired approval
async function test4() {
  console.log('\nTEST 4: Expired approval — claim after deadline rejected');
  const { data: req } = await newDecision({ deadline: new Date(Date.now() - 1000).toISOString() });
  await call(`/decision/${req.request_id}/approve`, 'POST', {}, ARCIDES_KEY);
  const { status } = await call(`/decision/${req.request_id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY);
  assert('expired → 409 or 410', [409,410].includes(status), `got ${status}`);
}

// 5. Non-arcides approval
async function test5() {
  console.log('\nTEST 5: Non-arcides approval — must be 403');
  const { data: req } = await newDecision();
  const { status: s1 } = await call(`/decision/${req.request_id}/approve`, 'POST', {}, CLAUDE_KEY);
  const { status: s2 } = await call(`/decision/${req.request_id}/approve`, 'POST', {}, SOBA_KEY);
  assert('claude approve → 403', s1 === 403, `got ${s1}`);
  assert('soba approve → 403',   s2 === 403, `got ${s2}`);
}

// 6. Malformed events
async function test6() {
  console.log('\nTEST 6: Malformed event — missing required fields → 400');
  for (const [body, label] of [
    [{},                            'empty body'],
    [{ scope: 'x' },               'missing target+params'],
    [{ scope: 'x', target: 'y' },  'missing params'],
  ]) {
    const { status } = await call('/decision', 'POST', body, CLAUDE_KEY);
    assert(`${label} → 400`, status === 400, `got ${status}`);
  }
}

// 7. Retry after execution failure
async function test7() {
  console.log('\nTEST 7: Retry after execution_failed — resets to approved');
  const { data: req } = await newDecision();
  const id = req.request_id;
  await call(`/decision/${id}/approve`, 'POST', {}, ARCIDES_KEY);
  await call(`/decision/${id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY);
  const { status: failStatus } = await call(`/decision/${id}/fail`, 'POST', { error: 'timeout' }, CLAUDE_KEY);
  assert('execution_failed accepted', failStatus === 201, `got ${failStatus}`);
  const { data: state } = await call(`/decision/${id}`, 'GET', null, CLAUDE_KEY);
  assert('status reset to approved', state.status === 'approved', `got ${state.status}`);
  const { status: claimStatus } = await call(`/decision/${id}/claim`, 'POST', { params: { version: 'v0.0.9' } }, CLAUDE_KEY);
  assert('second claim succeeds', claimStatus === 201, `got ${claimStatus}`);
}

async function main() {
  console.log('SPHERA Decision Queue — Adversarial Tests');
  console.log(`Bridge: ${BASE}`);
  if (!CLAUDE_KEY || !ARCIDES_KEY) { console.error('Set BRIDGE_URL, CLAUDE_KEY, SOBA_KEY, ARCIDES_KEY'); process.exit(1); }
  await test1(); await test2(); await test3(); await test4();
  await test5(); await test6(); await test7();
  console.log(`\n${'─'.repeat(40)}\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

main().catch(e => { console.error(e); process.exit(1); });
