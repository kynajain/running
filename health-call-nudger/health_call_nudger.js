'use strict';

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const axios = require('axios');
require('dotenv').config({ quiet: true });

const ROOT = __dirname;
const CONFIG_PATH = path.join(ROOT, 'config.json');
// Overridable so a container can keep state on a mounted volume.
const STATE_PATH = process.env.STATE_PATH || path.join(ROOT, 'state.json');
const API_PATH = path.join(ROOT, 'api.json');

const ELEVENLABS_BASE_URL = 'https://api.elevenlabs.io/v1/convai';
const ELEVENLABS_OUTBOUND_CALL_URL = `${ELEVENLABS_BASE_URL}/twilio/outbound-call`;

const CALL_ENV = [
  'ELEVENLABS_API_KEY',
  'ELEVENLABS_AGENT_ID',
  'ELEVENLABS_PHONE_NUMBER_ID',
  'TO_NUMBER',
];
const WEBHOOK_ENV = ['TERRA_SIGNING_SECRET'];
const ESCALATION_ENV = [
  'ELEVENLABS_ESCALATION_AGENT_ID',
  'EMERGENCY_CONTACT_NUMBER',
  'EMERGENCY_CONTACT_NAME',
];

// Terra rejects a signature whose timestamp is further than this from now.
const TERRA_TIMESTAMP_TOLERANCE_SECONDS = 300;
const MAX_CALL_DURATION_SECONDS = 90;
const OUTCOME_POLL_INTERVAL_MS = 5000;
const OUTCOME_POLL_TIMEOUT_MS = (MAX_CALL_DURATION_SECONDS + 60) * 1000;
const TERRA_PAYLOAD_TYPES = ['activity', 'daily', 'body'];
const MAX_TRACKED_RECORDS = 500;
const MAX_ESCALATION_LOG = 50;

// The webhook is the only endpoint that is safe to expose publicly, so it is
// the only one that needs a body cap and a rate limit. Terra's own guidance is
// payloads of a few MB.
const WEBHOOK_MAX_BODY_BYTES = 3 * 1024 * 1024;
const WEBHOOK_RATE_LIMIT = 60;
const WEBHOOK_RATE_WINDOW_MS = 60 * 1000;

// These place real phone calls or change who gets called. They are never open.
const ADMIN_ROUTES = ['POST /nudge', 'POST /test-call', 'POST /acknowledge', 'PATCH /config'];
const LOOPBACK_ADDRESSES = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);

const START_TIME = Date.now();

const DEFAULT_CONFIG = {
  stressThreshold: 75,
  cooldownSeconds: 1800,
  listenerPort: 4300,
  // Off until someone deliberately turns it on: escalation calls a third party.
  escalationEnabled: false,
  escalationDelaySeconds: 120,
};

const CONFIG_KEYS = Object.keys(DEFAULT_CONFIG);
const BOOLEAN_CONFIG_KEYS = ['escalationEnabled'];

class StagedError extends Error {
  constructor(stage, message, details) {
    super(message);
    this.name = 'StagedError';
    this.stage = stage;
    this.details = details;
  }
}

function log(level, stage, message, extra) {
  const line = { ts: new Date().toISOString(), level, stage, message, ...(extra || {}) };
  const out = level === 'error' ? process.stderr : process.stdout;
  out.write(`${JSON.stringify(line)}\n`);
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (err) {
    if (err.code !== 'ENOENT') {
      log('warn', 'startup', `Could not parse ${path.basename(filePath)}, using defaults`, {
        error: err.message,
      });
    }
    return fallback;
  }
}

function writeJsonAtomic(filePath, value) {
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(tmp, filePath);
}

let config = { ...DEFAULT_CONFIG };
let state = {
  nudgeCount: 0,
  lastNudgeAt: null,
  lastCallOutcome: null,
  lastEscalationAt: null,
  acknowledgedAt: null,
  escalationLog: [],
  records: {},
};

function missingEnv(names) {
  return names.filter((name) => !process.env[name]);
}

function loadConfig() {
  config = { ...DEFAULT_CONFIG, ...readJson(CONFIG_PATH, {}) };
  const missingCall = missingEnv(CALL_ENV);
  if (missingCall.length > 0) {
    log('warn', 'startup', 'Calling is not configured; /nudge and /test-call will fail', {
      missingEnv: missingCall,
    });
  }
  const missingWebhook = missingEnv(WEBHOOK_ENV);
  if (missingWebhook.length > 0) {
    log('warn', 'startup', 'Terra webhook is not configured; /webhook/terra will reject everything', {
      missingEnv: missingWebhook,
    });
  }
  const missingEscalation = missingEnv(ESCALATION_ENV);
  if (config.escalationEnabled && missingEscalation.length > 0) {
    log('warn', 'startup', 'Escalation is enabled but not configured; it cannot fire', {
      missingEnv: missingEscalation,
    });
  } else if (!config.escalationEnabled) {
    log('info', 'startup', 'Escalation is disabled; an unanswered call contacts nobody', {
      escalationEnabled: false,
    });
  }
  if (!LOOPBACK_ADDRESSES.has(bindAddress()) && !process.env.ADMIN_TOKEN) {
    log('warn', 'startup', 'Bound to a non-loopback address without ADMIN_TOKEN; /nudge, /test-call, /acknowledge and PATCH /config will reject every remote request', {
      bindAddress: bindAddress(),
      adminRoutes: ADMIN_ROUTES,
    });
  }
  return { config, missingEnv: [...missingCall, ...missingWebhook] };
}

function bindAddress() {
  return process.env.BIND_ADDRESS || '127.0.0.1';
}

function saveConfig() {
  writeJsonAtomic(CONFIG_PATH, config);
}

function loadState() {
  const loaded = readJson(STATE_PATH, {});
  state = {
    nudgeCount: Number.isFinite(loaded.nudgeCount) ? loaded.nudgeCount : 0,
    lastNudgeAt: typeof loaded.lastNudgeAt === 'string' ? loaded.lastNudgeAt : null,
    lastCallOutcome:
      loaded.lastCallOutcome && typeof loaded.lastCallOutcome === 'object'
        ? loaded.lastCallOutcome
        : null,
    lastEscalationAt: typeof loaded.lastEscalationAt === 'string' ? loaded.lastEscalationAt : null,
    acknowledgedAt: typeof loaded.acknowledgedAt === 'string' ? loaded.acknowledgedAt : null,
    escalationLog: Array.isArray(loaded.escalationLog) ? loaded.escalationLog : [],
    records: loaded.records && typeof loaded.records === 'object' ? loaded.records : {},
  };
  return state;
}

function saveState() {
  writeJsonAtomic(STATE_PATH, state);
}

function cooldownRemainingSeconds() {
  if (!state.lastNudgeAt) return 0;
  const elapsed = (Date.now() - new Date(state.lastNudgeAt).getTime()) / 1000;
  if (!Number.isFinite(elapsed)) return 0;
  return Math.max(0, Math.ceil(config.cooldownSeconds - elapsed));
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------

// Reads Terra's own enrichment only. Returns 0-100, or null when Terra has not
// (yet) derived a stress value for this payload.
//
// TODO: exertion-aware scoring. Raw heart rate is not a stress signal for a
// runner — elevated HR is the expected state mid-run. Real logic needs HRV
// against a personal baseline, or HR relative to pace. Do not add raw HR
// thresholds here.
function computeStressScore(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const enrichment = payload.data_enrichment;
  if (!enrichment || typeof enrichment !== 'object') return null;

  const raw = enrichment.stress;
  let value = null;
  if (typeof raw === 'number') {
    value = raw;
  } else if (raw && typeof raw === 'object') {
    for (const key of ['score', 'level', 'value']) {
      if (typeof raw[key] === 'number') {
        value = raw[key];
        break;
      }
    }
  }
  if (value === null || !Number.isFinite(value)) return null;
  return Math.min(100, Math.max(0, value));
}

// ---------------------------------------------------------------------------
// Terra webhook
// ---------------------------------------------------------------------------

// Mirrors terra-api's verifyTerraWebhookSignature: header is
// `t=<unix_seconds>,v1=<hex>`, signed payload is `${t}.${rawBody}`, HMAC-SHA256
// with the signing secret, compared in constant time within a 300s window.
function verifyTerraSignature(rawBody, signatureHeader, signingSecret) {
  if (!signingSecret) return { valid: false, reason: 'TERRA_SIGNING_SECRET not set' };
  if (!signatureHeader) return { valid: false, reason: 'terra-signature header missing' };

  let timestamp = null;
  const signatures = [];
  for (const element of signatureHeader.split(',')) {
    const trimmed = element.trim();
    if (!trimmed.includes('=')) continue;
    const [prefix, ...valueParts] = trimmed.split('=');
    const value = valueParts.join('=').trim();
    if (prefix.trim() === 't') timestamp = value;
    else if (prefix.trim() === 'v1') signatures.push(value);
  }

  if (!timestamp) return { valid: false, reason: 'no timestamp in signature header' };
  if (signatures.length === 0) return { valid: false, reason: 'no v1 signature in header' };

  const expected = crypto
    .createHmac('sha256', signingSecret)
    .update(`${timestamp}.${rawBody}`, 'utf8')
    .digest('hex');
  const expectedBuffer = Buffer.from(expected, 'hex');

  const matched = signatures.some((signature) => {
    try {
      const received = Buffer.from(signature, 'hex');
      return (
        received.length === expectedBuffer.length &&
        crypto.timingSafeEqual(expectedBuffer, received)
      );
    } catch {
      return false;
    }
  });
  if (!matched) return { valid: false, reason: 'no matching signature' };

  const age = Math.floor(Date.now() / 1000) - Number.parseInt(timestamp, 10);
  if (!Number.isFinite(age)) return { valid: false, reason: 'invalid timestamp' };
  if (Math.abs(age) > TERRA_TIMESTAMP_TOLERANCE_SECONDS) {
    return { valid: false, reason: `timestamp outside ${TERRA_TIMESTAMP_TOLERANCE_SECONDS}s window` };
  }
  return { valid: true };
}

function recordKey(type, userId, entry) {
  const metadata = (entry && entry.metadata) || {};
  const period =
    metadata.start_time || metadata.summary_id || metadata.end_time || metadata.date || 'unknown';
  return `${type}:${userId || 'unknown'}:${period}`;
}

function pruneRecords() {
  const keys = Object.keys(state.records);
  if (keys.length <= MAX_TRACKED_RECORDS) return;
  keys
    .sort((a, b) => String(state.records[a].updatedAt).localeCompare(state.records[b].updatedAt))
    .slice(0, keys.length - MAX_TRACKED_RECORDS)
    .forEach((key) => delete state.records[key]);
}

// Terra resends payloads for the same period as more data arrives, and
// data_enrichment fields are latest-known — they can come back null on a
// resend. Keep the highest-confidence value we have ever seen for a period and
// never let a null overwrite it, and only ever call once per period.
function mergeRecord(key, score) {
  const existing = state.records[key];
  const previousScore = existing && typeof existing.score === 'number' ? existing.score : null;
  const effectiveScore = score === null ? previousScore : score;

  state.records[key] = {
    score: effectiveScore,
    called: Boolean(existing && existing.called),
    updatedAt: new Date().toISOString(),
  };
  pruneRecords();

  return {
    effectiveScore,
    previousScore,
    alreadyCalled: state.records[key].called,
    keptPreviousScore: score === null && previousScore !== null,
  };
}

function markRecordCalled(key) {
  if (state.records[key]) state.records[key].called = true;
}

function processTerraPayload(body) {
  const type = typeof body.type === 'string' ? body.type : 'unknown';
  const userId = (body.user && body.user.user_id) || null;
  const entries = Array.isArray(body.data) ? body.data : [];

  if (!TERRA_PAYLOAD_TYPES.includes(type)) {
    log('info', 'terra_webhook', 'Ignoring payload type', { type, entries: entries.length });
    return;
  }

  // Verbose on purpose while we are still learning the real Oura shapes.
  log('info', 'terra_webhook', 'Normalised payload received', {
    type,
    userId,
    provider: (body.user && body.user.provider) || null,
    entryCount: entries.length,
    payload: body,
  });

  for (const entry of entries) {
    const key = recordKey(type, userId, entry);
    const score = computeStressScore(entry);
    const merged = mergeRecord(key, score);
    saveState();

    if (merged.keptPreviousScore) {
      log('info', 'terra_webhook', 'Resend had no stress value; keeping known score', {
        key,
        score: merged.previousScore,
      });
    }
    if (merged.effectiveScore === null) {
      log('info', 'terra_webhook', 'No Terra stress enrichment; skipping', { key, type });
      continue;
    }
    if (merged.alreadyCalled) {
      log('info', 'terra_webhook', 'Already called for this period; skipping', {
        key,
        score: merged.effectiveScore,
      });
      continue;
    }

    const decision = maybeNudge(merged.effectiveScore, `${type} data from ${key}`);
    log('info', 'terra_webhook', 'Nudge decision', { key, score: merged.effectiveScore, ...decision });
    if (decision.status === 'ok') {
      markRecordCalled(key);
      saveState();
      return;
    }
  }
}

// ---------------------------------------------------------------------------
// Calling
// ---------------------------------------------------------------------------

// Template-based on purpose: LLM-generated coaching copy is out of scope.
function buildNudgeMessage(score, context) {
  const rounded = Math.round(score);
  const band = rounded >= 90 ? 'very high' : rounded >= 80 ? 'high' : 'elevated';
  const detail = context ? ` I'm seeing ${context}.` : '';
  return (
    `Hey, it's just a check-in. Your stress reading is ${rounded}, which is ${band}.` +
    `${detail} Nothing to worry about — let's slow your breathing down together for a minute. ` +
    `Are you doing okay out there?`
  );
}

// Deliberately factual and flat. This person is not the runner, has not asked
// to be called, and must not be told an emergency has been confirmed.
function buildEscalationMessage(score, contactName) {
  const rounded = Math.round(score);
  const greeting = contactName ? `Hello ${contactName}.` : 'Hello.';
  return (
    `${greeting} This is an automated check-in call, not a live person. ` +
    `You are listed as the contact for a runner using a wellbeing check-in service. ` +
    `Her wearable reported an elevated stress reading of ${rounded}, and she did not answer ` +
    `an automated check-in call. This may well be nothing: the reading is not a confirmed ` +
    `emergency, and the data can lag behind real events by several minutes. ` +
    `If you would like to, you may want to try contacting her directly. ` +
    `This call will not be repeated.`
  );
}

function elevenLabsHeaders() {
  return {
    'xi-api-key': process.env.ELEVENLABS_API_KEY,
    'Content-Type': 'application/json',
  };
}

// role 'escalation' uses its own agent: the script for a third party is not the
// runner's script and the two personas must not be shared.
async function placeCall(message, role = 'primary') {
  const required =
    role === 'escalation'
      ? ['ELEVENLABS_API_KEY', 'ELEVENLABS_PHONE_NUMBER_ID', ...ESCALATION_ENV]
      : CALL_ENV;
  const missing = missingEnv(required);
  if (missing.length > 0) {
    throw new StagedError('elevenlabs_call', 'ElevenLabs call skipped: missing configuration', {
      missingEnv: missing,
      role,
    });
  }

  const agentId =
    role === 'escalation'
      ? process.env.ELEVENLABS_ESCALATION_AGENT_ID
      : process.env.ELEVENLABS_AGENT_ID;
  const toNumber =
    role === 'escalation' ? process.env.EMERGENCY_CONTACT_NUMBER : process.env.TO_NUMBER;

  const body = {
    agent_id: agentId,
    agent_phone_number_id: process.env.ELEVENLABS_PHONE_NUMBER_ID,
    to_number: toNumber,
    conversation_initiation_client_data: {
      conversation_config_override: {
        agent: { first_message: message },
        conversation: { max_duration_seconds: MAX_CALL_DURATION_SECONDS },
      },
      dynamic_variables: { nudge_message: message },
    },
  };

  log('info', 'elevenlabs_call', 'Placing outbound call', {
    role,
    to: toNumber,
    messageLength: message.length,
    maxDurationSeconds: MAX_CALL_DURATION_SECONDS,
  });

  try {
    const res = await axios.post(ELEVENLABS_OUTBOUND_CALL_URL, body, {
      headers: elevenLabsHeaders(),
      timeout: 20000,
    });
    log('info', 'elevenlabs_call', 'Outbound call accepted by ElevenLabs', {
      conversationId: res.data && res.data.conversation_id,
      callSid: res.data && res.data.callSid,
    });
    return res.data || {};
  } catch (err) {
    const details = err.response
      ? { status: err.response.status, body: err.response.data }
      : { error: err.message };
    throw new StagedError('elevenlabs_call', 'ElevenLabs outbound call failed', details);
  }
}

function recordOutcome(outcome) {
  state.lastCallOutcome = { at: new Date().toISOString(), ...outcome };
  saveState();
  return state.lastCallOutcome;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchConversation(conversationId) {
  const res = await axios.get(`${ELEVENLABS_BASE_URL}/conversations/${conversationId}`, {
    headers: elevenLabsHeaders(),
    timeout: 15000,
  });
  return res.data || {};
}

function conversationWasAccepted(conversation) {
  const metadata = (conversation && conversation.metadata) || {};
  return Boolean(metadata.accepted_time_unix_secs) || Number(metadata.call_duration_secs) > 0;
}

// ---------------------------------------------------------------------------
// Escalation
// ---------------------------------------------------------------------------

function escalationCooldownRemainingSeconds() {
  if (!state.lastEscalationAt) return 0;
  const elapsed = (Date.now() - new Date(state.lastEscalationAt).getTime()) / 1000;
  if (!Number.isFinite(elapsed)) return 0;
  return Math.max(0, Math.ceil(config.cooldownSeconds - elapsed));
}

// The audit trail. One line per attempt, whatever the verdict, including the
// ones that never dialled — "why did nobody get called" matters as much as
// "who got called".
function auditEscalation(entry) {
  const record = { at: new Date().toISOString(), ...entry };
  state.escalationLog.push(record);
  if (state.escalationLog.length > MAX_ESCALATION_LOG) {
    state.escalationLog = state.escalationLog.slice(-MAX_ESCALATION_LOG);
  }
  saveState();
  log('warn', 'escalation', `ESCALATION ${record.verdict}: ${record.reason}`, record);
  return record;
}

function acknowledge(source) {
  state.acknowledgedAt = new Date().toISOString();
  saveState();
  log('info', 'escalation', 'Check-in acknowledged; pending escalation will be cancelled', {
    source,
    at: state.acknowledgedAt,
  });
  return state.acknowledgedAt;
}

// Did the runner turn up after the missed call — either by picking up late, or
// by acknowledging through /acknowledge?
async function userRespondedSince(sinceMs, conversationId) {
  if (state.acknowledgedAt && new Date(state.acknowledgedAt).getTime() >= sinceMs) {
    return { responded: true, via: 'acknowledge_endpoint' };
  }
  if (conversationId) {
    try {
      if (conversationWasAccepted(await fetchConversation(conversationId))) {
        return { responded: true, via: 'call_answered_late' };
      }
    } catch (err) {
      log('warn', 'escalation', 'Could not re-check conversation before escalating', {
        conversationId,
        error: err.message,
      });
    }
  }
  return { responded: false };
}

async function escalate(trigger) {
  const missing = missingEnv(ESCALATION_ENV);
  if (missing.length > 0) {
    return auditEscalation({
      verdict: 'failed',
      reason: 'escalation is enabled but not configured',
      triggeringScore: trigger.score,
      missingEnv: missing,
    });
  }

  const remaining = escalationCooldownRemainingSeconds();
  if (remaining > 0) {
    return auditEscalation({
      verdict: 'skipped',
      reason: `already escalated within the ${config.cooldownSeconds}s window`,
      triggeringScore: trigger.score,
      retryAfterSeconds: remaining,
    });
  }

  // Claim the window before waiting: more Terra payloads for the same period
  // will arrive while we sleep and must not queue a second escalation.
  const claimedAt = Date.now();
  state.lastEscalationAt = new Date(claimedAt).toISOString();
  saveState();

  auditEscalation({
    verdict: 'pending',
    reason: `unanswered check-in; waiting ${config.escalationDelaySeconds}s before contacting ${process.env.EMERGENCY_CONTACT_NAME}`,
    triggeringScore: trigger.score,
    primaryConversationId: trigger.conversationId || null,
  });

  await sleep(config.escalationDelaySeconds * 1000);

  const response = await userRespondedSince(claimedAt, trigger.conversationId);
  if (response.responded) {
    // Nobody was called, so release the window for a genuine later miss.
    state.lastEscalationAt = null;
    saveState();
    return auditEscalation({
      verdict: 'cancelled',
      reason: `runner responded during the delay (${response.via})`,
      triggeringScore: trigger.score,
      primaryConversationId: trigger.conversationId || null,
    });
  }

  const message = buildEscalationMessage(trigger.score, process.env.EMERGENCY_CONTACT_NAME);
  try {
    const result = await placeCall(message, 'escalation');
    const record = auditEscalation({
      verdict: 'called',
      reason: `contacted ${process.env.EMERGENCY_CONTACT_NAME} after an unanswered check-in`,
      triggeringScore: trigger.score,
      primaryConversationId: trigger.conversationId || null,
      escalationConversationId: result.conversation_id || null,
    });
    trackCallOutcome(result.conversation_id, { role: 'escalation' })
      .then((outcome) => {
        auditEscalation({
          verdict: 'outcome',
          reason: `escalation call ${outcome.outcome}`,
          triggeringScore: trigger.score,
          escalationConversationId: result.conversation_id || null,
          outcome: outcome.outcome,
        });
      })
      .catch((err) => {
        log('error', 'escalation', 'Escalation outcome tracking failed', { error: err.message });
      });
    return record;
  } catch (err) {
    return auditEscalation({
      verdict: 'failed',
      reason: `escalation call to ${process.env.EMERGENCY_CONTACT_NAME} failed`,
      triggeringScore: trigger.score,
      primaryConversationId: trigger.conversationId || null,
      stage: err.stage || 'unknown',
      error: err.message,
      details: err.details,
    });
  }
}

async function handleUnansweredCall(outcome, trigger = {}) {
  log('warn', 'call_outcome', 'Check-in call went unanswered', outcome);

  if (!config.escalationEnabled) {
    log('warn', 'escalation', 'Escalation is disabled; nobody will be contacted', {
      conversationId: outcome.conversationId || null,
      triggeringScore: trigger.score === undefined ? null : trigger.score,
    });
    return null;
  }

  return escalate({
    score: trigger.score === undefined ? null : trigger.score,
    context: trigger.context || '',
    conversationId: outcome.conversationId || null,
  });
}

// ElevenLabs reports the conversation lifecycle as
// initiated -> in-progress -> processing -> done | failed. A call that was
// picked up has an accepted timestamp and a non-zero duration; one that rang
// out reaches a terminal state with neither.
async function trackCallOutcome(conversationId, trigger = {}) {
  const role = trigger.role || 'primary';
  if (!conversationId) {
    return recordOutcome({
      outcome: 'unknown',
      stage: 'call_outcome',
      role,
      reason: 'ElevenLabs returned no conversation_id',
    });
  }

  const deadline = Date.now() + OUTCOME_POLL_TIMEOUT_MS;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await fetchConversation(conversationId);
    } catch (err) {
      const details = err.response
        ? { status: err.response.status, body: err.response.data }
        : { error: err.message };
      log('warn', 'call_outcome', 'Could not read conversation status', {
        conversationId,
        ...details,
      });
    }

    const status = last && last.status;
    if (status === 'done' || status === 'failed') break;
    await sleep(OUTCOME_POLL_INTERVAL_MS);
  }

  if (!last || !last.status) {
    return recordOutcome({
      outcome: 'unknown',
      stage: 'call_outcome',
      conversationId,
      reason: 'conversation status unavailable',
    });
  }

  const metadata = last.metadata || {};
  const durationSecs = Number(metadata.call_duration_secs) || 0;
  const accepted = Boolean(metadata.accepted_time_unix_secs) || durationSecs > 0;

  if (last.status === 'failed') {
    const outcome = recordOutcome({
      outcome: 'failed',
      stage: 'elevenlabs_call',
      conversationId,
      status: last.status,
      terminationReason: metadata.termination_reason || null,
      error: metadata.error || null,
    });
    log('error', 'call_outcome', 'Call failed', outcome);
    return outcome;
  }

  if (accepted) {
    const outcome = recordOutcome({
      outcome: 'answered',
      stage: 'complete',
      conversationId,
      status: last.status,
      callDurationSecs: durationSecs,
    });
    log('info', 'call_outcome', 'Call answered', outcome);
    return outcome;
  }

  const outcome = recordOutcome({
    outcome: 'unanswered',
    stage: 'complete',
    conversationId,
    status: last.status,
    terminationReason: metadata.termination_reason || null,
  });
  // Only a missed call to the runner escalates; a missed escalation call stops here.
  if (role === 'escalation') {
    log('warn', 'call_outcome', 'Escalation call went unanswered', outcome);
  } else {
    handleUnansweredCall(outcome, trigger).catch((err) => {
      log('error', 'escalation', 'Escalation handling failed', { error: err.message });
    });
  }
  return outcome;
}

async function deliverNudge(score, context) {
  let message;
  try {
    message = buildNudgeMessage(score, context);
  } catch (err) {
    const outcome = recordOutcome({
      outcome: 'failed',
      stage: 'message_generation',
      error: err.message,
    });
    log('error', 'message_generation', 'Failed to build nudge message', outcome);
    throw err;
  }

  let result;
  try {
    result = await placeCall(message);
  } catch (err) {
    const outcome = recordOutcome({
      outcome: 'failed',
      stage: err.stage || 'unknown',
      error: err.message,
      details: err.details,
    });
    log('error', outcome.stage, err.message, outcome);
    throw err;
  }

  return trackCallOutcome(result.conversation_id, { role: 'primary', score, context });
}

function recordNudge() {
  state.nudgeCount += 1;
  state.lastNudgeAt = new Date().toISOString();
  saveState();
}

// Shared by /nudge and the Terra webhook. Claims the cooldown slot before
// dialing so a slow ElevenLabs response cannot let a second nudge through.
function maybeNudge(score, context) {
  if (score < config.stressThreshold) {
    return { status: 'skipped', reason: 'below_threshold' };
  }
  const remaining = cooldownRemainingSeconds();
  if (remaining > 0) {
    return { status: 'skipped', reason: 'cooldown', retryAfterSeconds: remaining };
  }

  recordNudge();
  deliverNudge(score, context).catch(() => {
    // deliverNudge already recorded the failing stage in lastCallOutcome.
  });
  return { status: 'ok' };
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

function sendJson(res, statusCode, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  res.writeHead(statusCode, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

// Behind a tunnel or load balancer every request arrives from the proxy, so the
// per-IP rate limit is only meaningful if we trust its forwarded header.
function clientIp(req) {
  if (process.env.TRUST_PROXY === '1') {
    const forwarded = req.headers['x-forwarded-for'];
    if (typeof forwarded === 'string' && forwarded.trim()) {
      return forwarded.split(',')[0].trim();
    }
  }
  return req.socket.remoteAddress || 'unknown';
}

const webhookHits = new Map();

function rateLimited(ip) {
  const now = Date.now();
  const hits = (webhookHits.get(ip) || []).filter((at) => now - at < WEBHOOK_RATE_WINDOW_MS);
  hits.push(now);
  webhookHits.set(ip, hits);
  if (webhookHits.size > 1000) {
    for (const [key, times] of webhookHits) {
      if (times.every((at) => now - at >= WEBHOOK_RATE_WINDOW_MS)) webhookHits.delete(key);
    }
  }
  return hits.length > WEBHOOK_RATE_LIMIT;
}

// No dev bypass: without ADMIN_TOKEN these routes are loopback-only.
function adminAuthorised(req) {
  const token = process.env.ADMIN_TOKEN;
  if (!token) {
    return LOOPBACK_ADDRESSES.has(req.socket.remoteAddress || '')
      ? { ok: true }
      : { ok: false, reason: 'ADMIN_TOKEN is not set; admin routes are loopback-only' };
  }
  const provided = req.headers['x-admin-token'];
  if (typeof provided !== 'string' || provided.length !== token.length) {
    return { ok: false, reason: 'missing or malformed x-admin-token' };
  }
  const ok = crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(token));
  return ok ? { ok: true } : { ok: false, reason: 'invalid x-admin-token' };
}

function readRawBody(req, limitBytes = WEBHOOK_MAX_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limitBytes) {
        reject(new Error('Request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function parseJsonObject(raw) {
  if (!raw.trim()) return {};
  const parsed = JSON.parse(raw);
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Body must be a JSON object');
  }
  return parsed;
}

async function readBody(req) {
  const raw = await readRawBody(req, 64 * 1024);
  try {
    return parseJsonObject(raw);
  } catch (err) {
    throw new Error(err.message === 'Body must be a JSON object' ? err.message : 'Body must be valid JSON');
  }
}

async function handleTerraWebhook(req, res) {
  const ip = clientIp(req);
  if (rateLimited(ip)) {
    log('warn', 'terra_webhook', 'Rejected webhook: rate limit', { ip, limit: WEBHOOK_RATE_LIMIT });
    sendJson(res, 429, { status: 'error', error: 'rate limit exceeded' });
    return;
  }

  const declared = Number(req.headers['content-length']);
  if (Number.isFinite(declared) && declared > WEBHOOK_MAX_BODY_BYTES) {
    log('warn', 'terra_webhook', 'Rejected webhook: body too large', { ip, contentLength: declared });
    sendJson(res, 413, { status: 'error', error: 'request body too large' });
    return;
  }

  // Verification needs the raw, unaltered body — parse only after checking.
  let raw;
  try {
    raw = await readRawBody(req, WEBHOOK_MAX_BODY_BYTES);
  } catch (err) {
    log('warn', 'terra_webhook', 'Rejected webhook: oversized body', { ip, error: err.message });
    if (!res.headersSent) sendJson(res, 413, { status: 'error', error: err.message });
    return;
  }

  const verification = verifyTerraSignature(
    raw,
    req.headers['terra-signature'],
    process.env.TERRA_SIGNING_SECRET,
  );
  if (!verification.valid) {
    log('warn', 'terra_webhook', 'Rejected webhook: signature', { ip, reason: verification.reason });
    sendJson(res, 401, { status: 'error', error: verification.reason });
    return;
  }

  // Terra retries on any non-200, so acknowledge before doing any work.
  sendJson(res, 200, { status: 'received' });

  let body;
  try {
    body = parseJsonObject(raw);
  } catch (err) {
    log('error', 'terra_webhook', 'Verified webhook had unparseable body', { error: err.message });
    return;
  }
  try {
    processTerraPayload(body);
  } catch (err) {
    log('error', 'terra_webhook', 'Failed to process payload', { error: err.message });
  }
}

async function handleNudge(req, res) {
  const body = await readBody(req);
  const score = body.score;
  if (typeof score !== 'number' || !Number.isFinite(score) || score < 0 || score > 100) {
    sendJson(res, 400, { status: 'error', error: 'score must be a number between 0 and 100' });
    return;
  }
  const context = typeof body.context === 'string' ? body.context : '';
  sendJson(res, 200, maybeNudge(score, context));
}

async function handleTestCall(req, res) {
  const body = await readBody(req);
  if (typeof body.message !== 'string' || !body.message.trim()) {
    sendJson(res, 400, { status: 'error', error: 'message must be a non-empty string' });
    return;
  }
  try {
    const result = await placeCall(body.message.trim());
    sendJson(res, 200, { status: 'ok', conversationId: result.conversation_id || null });
    trackCallOutcome(result.conversation_id, { role: 'primary', score: null }).catch((err) => {
      log('error', 'call_outcome', 'Outcome tracking failed', { error: err.message });
    });
  } catch (err) {
    const outcome = recordOutcome({
      outcome: 'failed',
      stage: err.stage || 'unknown',
      error: err.message,
      details: err.details,
    });
    log('error', outcome.stage, err.message, outcome);
    sendJson(res, 502, {
      status: 'error',
      stage: outcome.stage,
      error: err.message,
      details: err.details,
    });
  }
}

function handleStatus(res) {
  sendJson(res, 200, {
    nudgeCount: state.nudgeCount,
    lastNudgeAt: state.lastNudgeAt,
    lastCallOutcome: state.lastCallOutcome,
    cooldownActive: cooldownRemainingSeconds() > 0,
    callingConfigured: missingEnv(CALL_ENV).length === 0,
    webhookConfigured: missingEnv(WEBHOOK_ENV).length === 0,
    missingEnv: missingEnv([...CALL_ENV, ...WEBHOOK_ENV]),
    escalation: {
      enabled: config.escalationEnabled,
      configured: missingEnv(ESCALATION_ENV).length === 0,
      missingEnv: missingEnv(ESCALATION_ENV),
      lastEscalationAt: state.lastEscalationAt,
      acknowledgedAt: state.acknowledgedAt,
      recent: state.escalationLog.slice(-10),
    },
  });
}

async function handleConfigPatch(req, res) {
  const body = await readBody(req);
  const unknown = Object.keys(body).filter((key) => !CONFIG_KEYS.includes(key));
  if (unknown.length > 0) {
    sendJson(res, 400, {
      status: 'error',
      error: `unsupported config keys: ${unknown.join(', ')}`,
      allowed: CONFIG_KEYS,
    });
    return;
  }
  const next = { ...config };
  for (const key of CONFIG_KEYS) {
    if (!(key in body)) continue;
    const value = body[key];
    if (BOOLEAN_CONFIG_KEYS.includes(key)) {
      if (typeof value !== 'boolean') {
        sendJson(res, 400, { status: 'error', error: `${key} must be a boolean` });
        return;
      }
    } else if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
      sendJson(res, 400, { status: 'error', error: `${key} must be a non-negative number` });
      return;
    }
    next[key] = value;
  }
  if (next.escalationEnabled && !config.escalationEnabled) {
    log('warn', 'config', 'Escalation ENABLED; an unanswered check-in will now call a third party', {
      escalationDelaySeconds: next.escalationDelaySeconds,
      contactConfigured: missingEnv(ESCALATION_ENV).length === 0,
    });
  }
  config = next;
  saveConfig();
  log('info', 'config', 'Config updated', { config });
  sendJson(res, 200, { status: 'ok', config });
}

// Separate from /status on purpose: platform health checks should not depend on
// call state, and /status is not safe to hand to an arbitrary load balancer.
function handleHealth(res) {
  const problems = [
    ...missingEnv(CALL_ENV).map((name) => `missing ${name}`),
    ...missingEnv(WEBHOOK_ENV).map((name) => `missing ${name}`),
    ...(config.escalationEnabled
      ? missingEnv(ESCALATION_ENV).map((name) => `escalation enabled but missing ${name}`)
      : []),
  ];
  sendJson(res, 200, {
    status: 'ok',
    uptimeSeconds: Math.floor((Date.now() - START_TIME) / 1000),
    configValid: problems.length === 0,
    problems,
  });
}

function handleApiDoc(res) {
  const doc = readJson(API_PATH, null);
  if (!doc) {
    sendJson(res, 500, { status: 'error', error: 'api.json not found' });
    return;
  }
  sendJson(res, 200, doc);
}

async function router(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const route = `${req.method} ${url.pathname}`;

  if (ADMIN_ROUTES.includes(route)) {
    const auth = adminAuthorised(req);
    if (!auth.ok) {
      log('warn', 'request', 'Rejected admin request', {
        route,
        ip: clientIp(req),
        reason: auth.reason,
      });
      return sendJson(res, 401, { status: 'error', error: 'unauthorised' });
    }
  }

  switch (route) {
    case 'POST /webhook/terra':
      return handleTerraWebhook(req, res);
    case 'POST /nudge':
      return handleNudge(req, res);
    case 'POST /test-call':
      return handleTestCall(req, res);
    case 'POST /acknowledge':
      return sendJson(res, 200, { status: 'ok', acknowledgedAt: acknowledge('endpoint') });
    case 'GET /health':
      return handleHealth(res);
    case 'GET /status':
      return handleStatus(res);
    case 'GET /config':
      return sendJson(res, 200, config);
    case 'PATCH /config':
      return handleConfigPatch(req, res);
    case 'GET /':
      return handleApiDoc(res);
    default:
      return sendJson(res, 404, { status: 'error', error: `no route for ${route}` });
  }
}

const server = http.createServer((req, res) => {
  Promise.resolve(router(req, res)).catch((err) => {
    log('error', 'request', 'Request failed', {
      route: `${req.method} ${req.url}`,
      error: err.message,
    });
    if (!res.headersSent) {
      sendJson(res, 400, { status: 'error', error: err.message });
    }
  });
});

function start() {
  loadConfig();
  loadState();
  server.listen(config.listenerPort, bindAddress(), () => {
    log('info', 'startup', 'health-call-nudger listening', {
      bindAddress: bindAddress(),
      port: config.listenerPort,
      stressThreshold: config.stressThreshold,
      cooldownSeconds: config.cooldownSeconds,
      escalationEnabled: config.escalationEnabled,
      adminAuth: process.env.ADMIN_TOKEN ? 'x-admin-token' : 'loopback-only',
    });
  });
}

if (require.main === module) {
  start();
}

// Out of scope by decision, do not add: Apple Health and Terra's Mobile SDK.
// Apple Health has no web API and would require an on-device app; all payloads
// are assumed to arrive from Oura via Terra's server-side integration.

module.exports = {
  loadConfig,
  loadState,
  computeStressScore,
  verifyTerraSignature,
  processTerraPayload,
  buildNudgeMessage,
  buildEscalationMessage,
  placeCall,
  trackCallOutcome,
  handleUnansweredCall,
  escalate,
  acknowledge,
  adminAuthorised,
  rateLimited,
  bindAddress,
  deliverNudge,
  maybeNudge,
  server,
  start,
  StagedError,
  ELEVENLABS_OUTBOUND_CALL_URL,
  MAX_CALL_DURATION_SECONDS,
};
