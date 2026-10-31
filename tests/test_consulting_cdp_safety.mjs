import assert from 'node:assert/strict';
import { test } from 'node:test';
import childProcess from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import { join, resolve } from 'node:path';
import { syncBuiltinESMExports } from 'node:module';
import timers from 'node:timers/promises';
import { pathToFileURL } from 'node:url';

const skill = process.env.CONSULTING_SKILL_DIR || resolve('dotfiles/.agent/skills/consulting-pptx-skill');
const originalSpawn = childProcess.spawn;
const originalDelay = timers.setTimeout;
const originalFetch = globalThis.fetch;
const originalWebSocket = globalThis.WebSocket;
const originalChrome = process.env.CHROME_PATH;
const originalWrite = fs.writeFileSync;
let mode, profile, child, connected, requests;
fs.writeFileSync = (file, ...args) => {
  if (mode === 'dump-error' && String(file).endsWith('dump.json')) throw new Error('fixture write failed');
  return originalWrite(file, ...args);
};
childProcess.spawn = (_binary, args) => {
  profile = args.find(arg => arg.startsWith('--user-data-dir=')).split('=')[1];
  child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.kill = signal => {
    child.signalCode = signal || 'SIGTERM';
    child.emit('exit', null, child.signalCode);
    child.emit('close', null, child.signalCode);
    return true;
  };
  if (mode === 'exit') {
    child.exitCode = 1;
    queueMicrotask(() => child.emit('exit', 1));
  } else if (mode !== 'missing') {
    fs.writeFileSync(join(profile, 'DevToolsActivePort'), mode === 'malformed'
      ? '12345\nws://remote.invalid/browser\n'
      : '12345\n/devtools/browser/owned-fixture\n');
  }
  return child;
};
timers.setTimeout = async () => {};
syncBuiltinESMExports();
process.env.CHROME_PATH = '/fixture/chrome';
globalThis.fetch = async url => {
  requests.push(String(url));
  return { ok: true, json: async () => ({ webSocketDebuggerUrl: 'ws://127.0.0.1:9999/devtools/browser/unrelated' }) };
};
globalThis.WebSocket = class {
  constructor(url) {
    connected.push(url);
    queueMicrotask(() => mode === 'socket-error' ? this.onerror?.(new Error('fixture connection failed')) : this.onopen?.());
  }
  close() { this.onclose?.(); }
  send(raw) {
    const { id, method } = JSON.parse(raw);
    queueMicrotask(() => {
      this.onmessage?.({ data: JSON.stringify({ id, result: { targetId: 'owned-target', sessionId: 'owned-session', result: { value: { slides: [], captures: [] } } } }) });
      if (method === 'Page.navigate') this.onmessage?.({ data: JSON.stringify({ method: 'Page.loadEventFired' }) });
    });
  }
};
const { openPage } = await import(pathToFileURL(join(skill, 'scripts/lib_cdp.mjs')));

function reset(nextMode) { mode = nextMode; connected = []; requests = []; profile = null; child = null; }
function cleanup() { if (profile) fs.rmSync(profile, { recursive: true, force: true }); }

test('CDP uses only the endpoint written into its own new profile', async () => {
  reset('ready');
  let page;
  try {
    page = await openPage('/fixture/deck.html');
    assert.deepEqual(connected, ['ws://127.0.0.1:12345/devtools/browser/owned-fixture']);
    assert.deepEqual(requests, []);
    await page.close();
    assert.equal(fs.existsSync(profile), false);
  } finally { await page?.close(); cleanup(); }
});

for (const state of ['missing', 'malformed', 'exit']) {
  test(`CDP refuses ${state} startup even when another endpoint responds`, async () => {
    reset(state);
    let page;
    try {
      await assert.rejects(async () => { page = await openPage('/fixture/deck.html'); });
      assert.deepEqual(connected, []);
      assert.deepEqual(requests, []);
      assert.equal(fs.existsSync(profile), false);
    } finally { await page?.close(); cleanup(); }
  });
}

test('CDP connection failure cleans up its child and profile', async () => {
  reset('socket-error');
  try {
    await assert.rejects(openPage('/fixture/deck.html'));
    assert.notEqual(child.signalCode, null);
    assert.equal(fs.existsSync(profile), false);
  } finally { cleanup(); }
});


test('HTML dump write failure closes the owned page', async () => {
  reset('dump-error');
  const output = fs.mkdtempSync('/tmp/cdp-dump-test-');
  const argv = process.argv;
  const exitCode = process.exitCode;
  process.argv = [process.execPath, 'html_dump.mjs', '/fixture/deck.html', output];
  try {
    try { await import(pathToFileURL(join(skill, 'scripts/html_dump.mjs'))); } catch {}
    assert.notEqual(child.signalCode, null);
    assert.equal(fs.existsSync(profile), false);
  } finally {
    process.argv = argv;
    process.exitCode = exitCode;
    fs.rmSync(output, { recursive: true, force: true });
    cleanup();
  }
});

test.after(() => {
  fs.writeFileSync = originalWrite;
  childProcess.spawn = originalSpawn;
  timers.setTimeout = originalDelay;
  syncBuiltinESMExports();
  globalThis.fetch = originalFetch;
  globalThis.WebSocket = originalWebSocket;
  if (originalChrome === undefined) delete process.env.CHROME_PATH;
  else process.env.CHROME_PATH = originalChrome;
});
