// AI Basics demo: zero-dependency Node server.
// Serves the lesson page and proxies to the Anthropic API so the key stays server-side.
// The model is pinned and max_tokens is capped so a demo can't run up a bill.

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

try {
  for (const line of fs.readFileSync(path.join(__dirname, '.env'), 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
} catch {}

const API_KEY = process.env.ANTHROPIC_API_KEY || '';
const MODEL = process.env.ANTHROPIC_MODEL || 'claude-haiku-5-5';
const BASE = (process.env.ANTHROPIC_BASE_URL || 'https://api.anthropic.com').replace(/\/+$/, '');
const MAX_TOKENS_CAP = 1024;
const PORT = parseInt(process.env.PORT || '3100', 10);

function readBody(req) {
  return new Promise((resolve, reject) => {
    const c = []; req.on('data', d => c.push(d)); req.on('end', () => resolve(Buffer.concat(c))); req.on('error', reject);
  });
}
function json(res, code, obj) { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)); }

function forward(res, suffix, bodyObj) {
  if (!API_KEY) return json(res, 500, { type: 'error', error: { message: 'ANTHROPIC_API_KEY is not set on the server. See README.' } });
  const url = new URL(BASE + suffix);
  const payload = Buffer.from(JSON.stringify(bodyObj));
  const lib = url.protocol === 'https:' ? https : http;
  const up = lib.request(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'content-length': payload.length, 'x-api-key': API_KEY, 'anthropic-version': '2023-06-01' },
  }, upRes => {
    res.writeHead(upRes.statusCode, { 'content-type': upRes.headers['content-type'] || 'application/json', 'cache-control': 'no-cache' });
    upRes.pipe(res);
  });
  up.on('error', e => { if (!res.headersSent) json(res, 502, { type: 'error', error: { message: 'Could not reach the Anthropic API: ' + e.message } }); else res.end(); });
  up.setTimeout(120000, () => up.destroy(new Error('upstream timeout')));
  up.end(payload);
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css' };

http.createServer(async (req, res) => {
  const p = new URL(req.url, 'http://x').pathname;
  try {
    if (p === '/api/info') return json(res, 200, { model: MODEL, hasKey: !!API_KEY });
    if (p === '/api/messages' && req.method === 'POST') {
      const b = JSON.parse((await readBody(req)).toString() || '{}');
      b.model = MODEL;
      b.max_tokens = Math.min(parseInt(b.max_tokens || 512, 10), MAX_TOKENS_CAP);
      return forward(res, '/v1/messages', b);
    }
    if (p === '/api/count' && req.method === 'POST') {
      const b = JSON.parse((await readBody(req)).toString() || '{}');
      const out = { model: MODEL, messages: b.messages };
      if (b.system) out.system = b.system;
      if (b.tools) out.tools = b.tools;
      return forward(res, '/v1/messages/count_tokens', out);
    }
    const file = path.join(__dirname, 'public', p === '/' ? 'story.html' : p === '/lab' ? 'index.html' : p);
    if (!file.startsWith(path.join(__dirname, 'public'))) { res.writeHead(403); return res.end(); }
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); return res.end('Not found'); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(data);
    });
  } catch (e) { json(res, 500, { type: 'error', error: { message: e.message } }); }
}).listen(PORT, process.env.HOST || '127.0.0.1', () => {
  console.log(`AI Basics demo: http://localhost:${PORT}  (model: ${MODEL})`);
  if (!API_KEY) console.log('WARNING: ANTHROPIC_API_KEY not set. Create a .env file (see .env.example).');
});
