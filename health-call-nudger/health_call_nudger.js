'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const axios = require('axios');
require('dotenv').config({ quiet: true });

const ROOT = __dirname;
const CONFIG_PATH = path.join(ROOT, 'config.json');
const STATE_PATH = path.join(ROOT, 'state.json');
const API_PATH = path.join(ROOT, 'api.json');

const ELEVENLABS_OUTBOUND_CALL_URL =
  'https://api.elevenlabs.io/v1/convai/twilio/outbound-call';

const REQUIRED_ENV = [
  'ELEVENLABS_API_KEY',
  'ELEVENLABS_AGENT_ID',
  'ELEVENLABS_PHONE_NUMBER_ID',
  'TO_NUMBER',
];

const DEFAULT_CONFIG = {
  stressThreshold: 75,
  cooldownSeconds: 1800,
  listenerPort: 4300,
};

const CONFIG_KEYS = Object.keys(DEFAULT_CONFIG);

class CallError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'CallError';
    this.stage = 'elevenlabs_call';
    this.details = details;
  }
}

function log(level, stage, message, extra) {
  const line = {
    ts: new Date().toISOString(),
    level,
    stage,
    message,
    ...(extra || {}),
  };
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
let state = { nudgeCount: 0, lastNudgeAt: null };

function loadConfig() {
  config = { ...DEFAULT_CONFIG, ...readJson(CONFIG_PATH, {}) };
  const missing = REQUIRED_ENV.filter((name) => !process.env[name]);
  if (missing.length > 0) {
    log('warn', 'startup', 'Calling is not configured; /nudge and /test-call will fail', {
      missingEnv: missing,
    });
  }
  return { config, missingEnv: missing };
}

function saveConfig() {
  writeJsonAtomic(CONFIG_PATH, config);
}

function loadState() {
  const loaded = readJson(STATE_PATH, {});
  state = {
    nudgeCount: Number.isFinite(loaded.nudgeCount) ? loaded.nudgeCount : 0,
    lastNudgeAt: typeof loaded.lastNudgeAt === 'string' ? loaded.lastNudgeAt : null,
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

function missingEnvVars() {
  return REQUIRED_ENV.filter((name) => !process.env[name]);
}

// Template-based for now. TODO: replace with LLM-generated coaching copy.
function buildNudgeMessage(score, context) {
  const rounded = Math.round(score);
  const band = rounded >= 90 ? 'very high' : rounded >= 80 ? 'high' : 'elevated';
  const detail = context ? ` I'm seeing ${context}.` : '';
  return (
    `Hey, quick check-in — your stress score is ${rounded}, which is ${band}.` +
    `${detail} Let's take two minutes right now to slow your breathing down and reset. ` +
    `Ready when you are.`
  );
}

async function placeCall(message) {
  const missing = missingEnvVars();
  if (missing.length > 0) {
    throw new CallError('ElevenLabs call skipped: missing configuration', {
      missingEnv: missing,
    });
  }

  const body = {
    agent_id: process.env.ELEVENLABS_AGENT_ID,
    agent_phone_number_id: process.env.ELEVENLABS_PHONE_NUMBER_ID,
    to_number: process.env.TO_NUMBER,
    conversation_initiation_client_data: {
      conversation_config_override: {
        agent: { first_message: message },
      },
      dynamic_variables: { nudge_message: message },
    },
  };

  log('info', 'elevenlabs_call', 'Placing outbound call', {
    to: process.env.TO_NUMBER,
    messageLength: message.length,
  });

  try {
    const res = await axios.post(ELEVENLABS_OUTBOUND_CALL_URL, body, {
      headers: {
        'xi-api-key': process.env.ELEVENLABS_API_KEY,
        'Content-Type': 'application/json',
      },
      timeout: 20000,
    });
    log('info', 'elevenlabs_call', 'Outbound call accepted by ElevenLabs', {
      conversationId: res.data && res.data.conversation_id,
      callSid: res.data && res.data.callSid,
    });
    return res.data;
  } catch (err) {
    const details = err.response
      ? { status: err.response.status, body: err.response.data }
      : { error: err.message };
    throw new CallError('ElevenLabs outbound call failed', details);
  }
}

async function deliverNudge(score, context) {
  let message;
  try {
    message = buildNudgeMessage(score, context);
  } catch (err) {
    log('error', 'message_generation', 'Failed to build nudge message', {
      error: err.message,
    });
    throw err;
  }
  return placeCall(message);
}

function sendJson(res, statusCode, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  res.writeHead(statusCode, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req, limitBytes = 64 * 1024) {
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
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw.trim()) {
        resolve({});
        return;
      }
      try {
        const parsed = JSON.parse(raw);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          reject(new Error('Body must be a JSON object'));
          return;
        }
        resolve(parsed);
      } catch (err) {
        reject(new Error('Body must be valid JSON'));
      }
    });
    req.on('error', reject);
  });
}

function recordNudge() {
  state.nudgeCount += 1;
  state.lastNudgeAt = new Date().toISOString();
  saveState();
}

async function handleNudge(req, res) {
  const body = await readBody(req);
  const score = body.score;
  if (typeof score !== 'number' || !Number.isFinite(score) || score < 0 || score > 100) {
    sendJson(res, 400, { status: 'error', error: 'score must be a number between 0 and 100' });
    return;
  }
  const context = typeof body.context === 'string' ? body.context : '';

  if (score < config.stressThreshold) {
    sendJson(res, 200, { status: 'skipped', reason: 'below_threshold' });
    return;
  }

  const remaining = cooldownRemainingSeconds();
  if (remaining > 0) {
    sendJson(res, 200, { status: 'skipped', reason: 'cooldown', retryAfterSeconds: remaining });
    return;
  }

  recordNudge();
  sendJson(res, 200, { status: 'ok' });

  deliverNudge(score, context).catch((err) => {
    log('error', err.stage || 'unknown', err.message, err.details || { error: err.message });
  });
}

async function handleTestCall(req, res) {
  const body = await readBody(req);
  if (typeof body.message !== 'string' || !body.message.trim()) {
    sendJson(res, 400, { status: 'error', error: 'message must be a non-empty string' });
    return;
  }
  try {
    const result = await placeCall(body.message.trim());
    sendJson(res, 200, { status: 'ok', result });
  } catch (err) {
    log('error', err.stage || 'unknown', err.message, err.details || { error: err.message });
    sendJson(res, 502, {
      status: 'error',
      stage: err.stage || 'unknown',
      error: err.message,
      details: err.details,
    });
  }
}

function handleStatus(res) {
  sendJson(res, 200, {
    nudgeCount: state.nudgeCount,
    lastNudgeAt: state.lastNudgeAt,
    cooldownActive: cooldownRemainingSeconds() > 0,
    callingConfigured: missingEnvVars().length === 0,
    missingEnv: missingEnvVars(),
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
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
      sendJson(res, 400, { status: 'error', error: `${key} must be a non-negative number` });
      return;
    }
    next[key] = value;
  }
  config = next;
  saveConfig();
  log('info', 'config', 'Config updated', { config });
  sendJson(res, 200, { status: 'ok', config });
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

  switch (route) {
    case 'POST /nudge':
      return handleNudge(req, res);
    case 'POST /test-call':
      return handleTestCall(req, res);
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
    log('error', 'request', 'Request failed', { route: `${req.method} ${req.url}`, error: err.message });
    if (!res.headersSent) {
      sendJson(res, 400, { status: 'error', error: err.message });
    }
  });
});

function start() {
  loadConfig();
  loadState();
  server.listen(config.listenerPort, () => {
    log('info', 'startup', 'health-call-nudger listening', {
      port: config.listenerPort,
      stressThreshold: config.stressThreshold,
      cooldownSeconds: config.cooldownSeconds,
    });
  });
}

if (require.main === module) {
  start();
}

// TODO(out of scope): health-data ingestion (Terra / Apple Health / wearables) feeding /nudge.
// TODO(out of scope): LLM-generated coaching messages replacing buildNudgeMessage().
// TODO(out of scope): decision-maker / monitor service registration.

module.exports = {
  loadConfig,
  loadState,
  buildNudgeMessage,
  placeCall,
  deliverNudge,
  server,
  start,
  CallError,
  ELEVENLABS_OUTBOUND_CALL_URL,
};
