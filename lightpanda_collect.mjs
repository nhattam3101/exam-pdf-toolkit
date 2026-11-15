#!/usr/bin/env node
// Zero-dependency CDP client for Lightpanda. Node 22+ provides WebSocket.
import fs from 'node:fs/promises';

const endpoint = process.env.LIGHTPANDA_CDP || 'ws://127.0.0.1:9222';
const targetUrl = process.argv[2];
const outputPath = process.argv[3];
const cookiePath = process.argv[4] || '';
if (!targetUrl || !outputPath) {
  console.error('usage: node lightpanda_collect.mjs URL OUTPUT_JSON [COOKIES_JSON]');
  process.exit(2);
}

let nextId = 0;
const pending = new Map();
const events = [];
const ws = new WebSocket(endpoint);

function command(method, params = {}, sessionId = undefined) {
  const id = ++nextId;
  const payload = {id, method, params};
  if (sessionId) payload.sessionId = sessionId;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    ws.send(JSON.stringify(payload));
  });
}

ws.addEventListener('message', event => {
  const msg = JSON.parse(String(event.data));
  if (msg.id && pending.has(msg.id)) {
    const waiter = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) waiter.reject(new Error(`${msg.error.code}: ${msg.error.message}`));
    else waiter.resolve(msg.result || {});
    return;
  }
  if (msg.method === 'Network.responseReceived') {
    const r = msg.params.response;
    events.push({url: r.url, status: r.status, mimeType: r.mimeType});
  }
});

await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, {once: true});
  ws.addEventListener('error', reject, {once: true});
});

try {
  const {browserContextId} = await command('Target.createBrowserContext');
  const {targetId} = await command('Target.createTarget', {url: 'about:blank', browserContextId});
  const attached = await command('Target.attachToTarget', {targetId, flatten: true});
  const sid = attached.sessionId;
  await command('Page.enable', {}, sid);
  await command('Runtime.enable', {}, sid);
  await command('Network.enable', {}, sid);

  if (cookiePath) {
    const raw = JSON.parse(await fs.readFile(cookiePath, 'utf8'));
    const list = Array.isArray(raw) ? raw : (raw.cookies || []);
    const cookies = list
      .filter(c => c && c.name && c.value && (c.domain || c.url))
      .map(c => ({
        name: c.name, value: c.value,
        ...(c.url ? {url: c.url} : {}),
        ...(c.domain ? {domain: c.domain} : {}),
        path: c.path || '/', secure: Boolean(c.secure), httpOnly: Boolean(c.httpOnly),
        ...((Number.isFinite(c.expires) || Number.isFinite(c.expirationDate)) && (c.expires || c.expirationDate) > 0
          ? {expires: c.expires || c.expirationDate} : {}),
        ...((() => {
          const aliases = {strict: 'Strict', lax: 'Lax', none: 'None', no_restriction: 'None', unspecified: undefined};
          const sameSite = aliases[String(c.sameSite || '').toLowerCase()];
          return sameSite ? {sameSite} : {};
        })()),
      }));
    if (cookies.length) await command('Network.setCookies', {cookies}, sid);
  }

  await command('Page.navigate', {url: targetUrl}, sid);
  await new Promise(resolve => setTimeout(resolve, 5000));
  const evaluated = await command('Runtime.evaluate', {
    expression: `JSON.stringify({
      url: location.href,
      title: document.title,
      html: document.documentElement.outerHTML,
      links: Array.from(document.querySelectorAll('a')).map(a => ({
        href: a.href, text: (a.textContent || '').trim(),
        download: a.getAttribute('download') || '',
        dataSrc: a.getAttribute('data-src') || ''
      })),
      images: Array.from(document.images).map(i => ({
        src: i.currentSrc || i.src, dataSrc: i.getAttribute('data-src') || '',
        alt: i.alt || '', width: i.naturalWidth, height: i.naturalHeight
      }))
    })`, returnByValue: true, awaitPromise: true
  }, sid);
  const dom = JSON.parse(evaluated.result.value);
  await fs.writeFile(outputPath, JSON.stringify({dom, network: events}, null, 2));
  await command('Target.closeTarget', {targetId});
  await command('Target.disposeBrowserContext', {browserContextId});
} finally {
  ws.close();
}

