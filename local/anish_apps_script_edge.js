/**
 * ANISH_APPS_SCRIPT_EDGE Worker (Production Gate 5 Aligned)
 * Subordinate proxy worker for the Anish Principal in SPHERA.
 * Implements: work_item_id JSON dedup, structured result blocks, zero-cost ceiling.
 * Deploy in Google Apps Script with 15-minute time trigger.
 *
 * PropertiesService keys required:
 *   GEMINI_API_KEY     — Gemini REST API key
 *   SPHERA_INBOX_ID    — Google Doc ID for SPHERA_INBOX_ANISH
 *   SPHERA_OUTBOX_ID   — Google Doc ID for SPHERA_OUTBOX_ANISH (default: 1n19MZ5QPNbsmTJFXM1PlLnnkSNyRyIZ4b8k4McINYbo)
 *
 * Edge identity: anish-apps-script-edge-01 / delegated_for=anish
 * NOT native Anish. Continuity class: scheduled_context_reconstruction.
 */

function pollAndExecuteAnishEdge() {
  const props = PropertiesService.getScriptProperties();

  // 1. Zero-Cost Free-Tier Circuit Breaker (Safety Cap: 200 calls/day)
  const DAILY_MAX_CALLS = 200;
  const todayStr = new Date().toISOString().slice(0, 10);
  const lastResetDate = props.getProperty('API_CALL_DATE') || '';
  let callCount = parseInt(props.getProperty('DAILY_API_CALL_COUNT') || '0', 10);

  if (lastResetDate !== todayStr) {
    callCount = 0;
    props.setProperty('API_CALL_DATE', todayStr);
    props.setProperty('DAILY_API_CALL_COUNT', '0');
  }

  if (callCount >= DAILY_MAX_CALLS) {
    console.warn(`[SAFETY CEILING REACHED] ${callCount}/${DAILY_MAX_CALLS} calls used today. Execution skipped.`);
    return;
  }

  // 2. Load Environment Properties
  const API_KEY = props.getProperty('GEMINI_API_KEY');
  const INBOX_ID = props.getProperty('SPHERA_INBOX_ID');
  const OUTBOX_ID = props.getProperty('SPHERA_OUTBOX_ID') || '1n19MZ5QPNbsmTJFXM1PlLnnkSNyRyIZ4b8k4McINYbo';

  if (!API_KEY || !INBOX_ID || !OUTBOX_ID) {
    console.error('Missing required script properties in PropertiesService.');
    return;
  }

  // 3. Parse Inbox Content
  const inboxDoc = DocumentApp.openById(INBOX_ID);
  const rawText = inboxDoc.getBody().getText().trim();
  if (!rawText) return;

  // Extract work_item_id
  const idMatch = rawText.match(/work_item_id:\s*([^\r\n]+)/i);
  if (!idMatch) {
    console.log('No work_item_id found in Inbox. Skipping.');
    return;
  }
  const currentWorkItemId = idMatch[1].trim();

  // 4. Dedup: work_item_id JSON array in PropertiesService
  let processedIds = [];
  try {
    processedIds = JSON.parse(props.getProperty('PROCESSED_WORK_ITEM_IDS') || '[]');
  } catch (e) { processedIds = []; }

  if (processedIds.includes(currentWorkItemId)) {
    console.log(`work_item_id "${currentWorkItemId}" already processed. Skipping.`);
    return;
  }

  // 5. Simple system instruction — no cachedContents
  const systemInstruction =
    "You are Anish, SPHERA team member. Work with Soba and Claude to complete the mission assigned by Boss. Be direct and useful.";

  const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${API_KEY}`;

  const payload = {
    system_instruction: { parts: [{ text: systemInstruction }] },
    contents: [{ role: "user", parts: [{ text: rawText }] }],
    generationConfig: { temperature: 0.2, maxOutputTokens: 2048 }
  };

  const response = UrlFetchApp.fetch(endpoint, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  callCount++;
  props.setProperty('DAILY_API_CALL_COUNT', callCount.toString());

  if (response.getResponseCode() !== 200) {
    console.error(`Gemini API error (${response.getResponseCode()}): ${response.getContentText()}`);
    return;
  }

  const json = JSON.parse(response.getContentText());
  const replyText = json.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!replyText) return;

  // 6. Write structured result block to outbox
  const outboxDoc = DocumentApp.openById(OUTBOX_ID);
  const body = outboxDoc.getBody();
  const formattedBlock =
`---
work_item_id: ${currentWorkItemId}
edge_id: anish-apps-script-edge-01
delegated_for: anish
continuity_class: scheduled_context_reconstruction
status: done
result:
${replyText.trim()}
`;
  body.appendParagraph(formattedBlock);

  // 7. Update processed IDs (retain last 200)
  processedIds.push(currentWorkItemId);
  if (processedIds.length > 200) processedIds = processedIds.slice(-200);
  props.setProperty('PROCESSED_WORK_ITEM_IDS', JSON.stringify(processedIds));
  console.log(`Committed work_item_id "${currentWorkItemId}". Total processed: ${processedIds.length}`);
}
