/**
 * Background-script network logging tests. No dependencies: a stub browser.*
 * is built here, extension/background.js is evaluated against it, and the
 * registered webRequest listeners are driven with synthetic request events.
 *
 * Run: node tests/background_script.test.mjs
 */

import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(here, '..', 'extension', 'background.js'), 'utf8');

/** A recordable webRequest event. Records the filter and extraInfoSpec too:
 *  filterResponseData() is only callable from a listener registered with
 *  "blocking", and details.requestBody / requestHeaders / responseHeaders are
 *  only populated when the matching extraInfoSpec entry is passed. A harness
 *  that drops those arguments cannot tell a working feature from a broken one. */
function makeEvent() {
  const listeners = new Set();
  const registrations = [];
  return {
    listeners,
    registrations,
    addListener: (fn, filter, extraInfoSpec) => {
      listeners.add(fn);
      registrations.push({ fn, filter, extraInfoSpec: extraInfoSpec || [] });
    },
    removeListener: (fn) => {
      listeners.delete(fn);
      const i = registrations.findIndex(r => r.fn === fn);
      if (i >= 0) registrations.splice(i, 1);
    },
    hasListeners: () => listeners.size > 0,
    spec: () => (registrations[0] ? registrations[0].extraInfoSpec : null),
    urls: () => (registrations[0] ? registrations[0].filter?.urls : null),
    fire: (details) => [...listeners].map(fn => fn(details))
  };
}

function loadBackground({ contentScriptReply = { success: true } } = {}) {
  const webRequest = {
    onBeforeRequest: makeEvent(),
    onBeforeSendHeaders: makeEvent(),
    onHeadersReceived: makeEvent(),
    onBeforeRedirect: makeEvent(),
    onCompleted: makeEvent(),
    onErrorOccurred: makeEvent()
  };

  const filters = [];
  webRequest.filterResponseData = (requestId) => {
    const filter = {
      requestId,
      written: [],
      closed: false,
      write(data) { this.written.push(data); },
      close() { this.closed = true; },
      disconnect() { this.disconnected = true; }
    };
    filters.push(filter);
    return filter;
  };

  const contentMessages = [];
  const tabsOnRemoved = makeEvent();

  const browserStub = {
    runtime: {
      connectNative: () => ({
        onMessage: { addListener() {} },
        onDisconnect: { addListener() {} },
        postMessage() {}
      }),
      onMessage: { addListener() {} },
      onMessageExternal: { addListener() {} },
      getURL: (p) => `moz-extension://stub/${p}`
    },
    tabs: {
      query: async () => [{ id: 7, windowId: 1, url: 'http://stub.test/', title: 'stub' }],
      get: async (id) => ({ id, windowId: 1, url: 'http://stub.test/', title: 'stub' }),
      sendMessage: async (tabId, message) => {
        contentMessages.push({ tabId, message });
        return contentScriptReply;
      },
      update: async () => ({}),
      reload: async () => ({}),
      remove: async () => ({}),
      captureVisibleTab: async () => 'data:image/png;base64,AAAA',
      onUpdated: { addListener() {}, removeListener() {} },
      onRemoved: tabsOnRemoved,
      executeScript: async () => ['ok']
    },
    windows: {
      getAll: async () => [{ id: 1, focused: true }],
      get: async (id) => ({ id, focused: true }),
      update: async () => ({})
    },
    webRequest,
    notifications: { create: async () => 'id' },
    contextMenus: { create() {}, onClicked: { addListener() {} } }
  };

  const sandbox = {
    browser: browserStub,
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    TextDecoder,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, Map, Set, RegExp, JSON, Math, Promise, Error
  };
  sandbox.globalThis = sandbox;

  const context = createContext(sandbox);
  runInContext(SOURCE, context, { filename: 'background.js' });

  // handleCommand is the native host's entry point into the extension.
  const command = (action, data, tabId) =>
    context.handleCommand({ action, data, tabId });

  return { context, command, webRequest, filters, contentMessages, tabsOnRemoved };
}

/** Drive one complete request through the webRequest lifecycle. */
function fireRequest(webRequest, {
  requestId = '1',
  tabId = 7,
  type = 'xmlhttprequest',
  method = 'GET',
  url = 'http://api.stub.test/items',
  requestHeaders = [],
  responseHeaders = [{ name: 'content-type', value: 'application/json' }],
  statusCode = 200,
  requestBody = null,
  complete = true
} = {}) {
  webRequest.onBeforeRequest.fire({ requestId, tabId, type, method, url, requestBody });
  webRequest.onBeforeSendHeaders.fire({ requestId, tabId, requestHeaders });
  webRequest.onHeadersReceived.fire({ requestId, tabId, statusCode, responseHeaders,
                                      statusLine: `HTTP/1.1 ${statusCode}` });
  if (complete) {
    webRequest.onCompleted.fire({ requestId, tabId, statusCode, fromCache: false });
  }
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --------------------------------------------------------------------------
// Listeners are attached only while logging

test('no webRequest listeners are attached until logging starts', async () => {
  const { webRequest } = loadBackground();

  for (const [name, event] of Object.entries(webRequest)) {
    if (typeof event === 'function') continue;
    assert.equal(event.hasListeners(), false, `${name} must have no listeners at load`);
  }
});

test('startLogging attaches listeners and stopLogging detaches them', async () => {
  const { command, webRequest } = loadBackground();

  await command('startLogging', {}, 7);
  assert.equal(webRequest.onBeforeRequest.hasListeners(), true);
  assert.equal(webRequest.onHeadersReceived.hasListeners(), true);
  assert.equal(webRequest.onCompleted.hasListeners(), true);

  await command('stopLogging', {}, 7);
  assert.equal(webRequest.onBeforeRequest.hasListeners(), false);
  assert.equal(webRequest.onHeadersReceived.hasListeners(), false);
  assert.equal(webRequest.onCompleted.hasListeners(), false);
});

test('listeners stay attached while another tab is still being logged', async () => {
  const { command, webRequest } = loadBackground();

  await command('startLogging', {}, 7);
  await command('startLogging', {}, 8);
  await command('stopLogging', {}, 7);

  assert.equal(webRequest.onBeforeRequest.hasListeners(), true,
    'tab 8 is still logging, so capture must continue');

  await command('stopLogging', {}, 8);
  assert.equal(webRequest.onBeforeRequest.hasListeners(), false);
});

// --------------------------------------------------------------------------
// Capture

test('a fetch-style request is captured', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { method: 'POST', url: 'http://api.stub.test/login' });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1);
  assert.equal(result.logs[0].method, 'POST');
  assert.equal(result.logs[0].url, 'http://api.stub.test/login');
  assert.equal(result.logs[0].status, 200);
  assert.equal(result.capturesFetch, true);
  assert.equal(result.source, 'webRequest');
});

test('requests from tabs that are not being logged are ignored', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { requestId: 'a', tabId: 7 });
  fireRequest(webRequest, { requestId: 'b', tabId: 99 });

  const logged = await command('getNetworkLogs', {}, 7);
  assert.equal(logged.logs.length, 1, 'only the logged tab should be captured');

  const other = await command('getNetworkLogs', {}, 99);
  assert.equal(other.logs.length, 0);
});

test('asset requests are skipped unless includeAllTypes is set', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { requestId: 'img', type: 'image',
                            url: 'http://stub.test/logo.png' });
  fireRequest(webRequest, { requestId: 'api', type: 'xmlhttprequest' });

  let result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1);
  assert.equal(result.logs[0].type, 'xmlhttprequest');

  await command('startLogging', { includeAllTypes: true, clearExisting: true }, 7);
  fireRequest(webRequest, { requestId: 'img2', type: 'image',
                            url: 'http://stub.test/logo.png' });

  result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1);
  assert.equal(result.logs[0].type, 'image');
});

test('credential-bearing headers are redacted in both directions', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, {
    requestHeaders: [
      { name: 'Authorization', value: 'Bearer sk-secret-value' },
      { name: 'Cookie', value: 'session=abcdef' },
      { name: 'X-Trace', value: 'keep-me' }
    ],
    responseHeaders: [
      { name: 'content-type', value: 'application/json' },
      { name: 'Set-Cookie', value: 'session=rotated' }
    ]
  });

  const result = await command('getNetworkLogs', {}, 7);
  const entry = result.logs[0];
  assert.equal(entry.requestHeaders.Authorization, '***');
  assert.equal(entry.requestHeaders.Cookie, '***');
  assert.equal(entry.requestHeaders['X-Trace'], 'keep-me');
  assert.equal(entry.responseHeaders['Set-Cookie'], '***');
  const serialized = JSON.stringify(result);
  assert.ok(!serialized.includes('sk-secret-value'), 'bearer token must not appear');
  assert.ok(!serialized.includes('abcdef'), 'cookie value must not appear');
  assert.ok(!serialized.includes('rotated'), 'set-cookie value must not appear');
});

test('a request body is decoded from raw bytes', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  const bytes = new TextEncoder().encode('{"q":"hello"}');
  fireRequest(webRequest, { method: 'POST', requestBody: { raw: [{ bytes }] } });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs[0].requestBody, '{"q":"hello"}');
});

test('an error response is recorded', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  webRequest.onBeforeRequest.fire({ requestId: 'e', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://api.stub.test/down' });
  webRequest.onErrorOccurred.fire({ requestId: 'e', tabId: 7,
                                    error: 'NS_ERROR_CONNECTION_REFUSED' });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs[0].error, 'NS_ERROR_CONNECTION_REFUSED');

  const errorsOnly = await command('getNetworkLogs', { errorsOnly: true }, 7);
  assert.equal(errorsOnly.logs.length, 1);
});

// --------------------------------------------------------------------------
// Response bodies: the stream filter must never alter the response

test('the response body filter passes every chunk through and closes', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { complete: false });
  assert.equal(filters.length, 1, 'a filter should be attached for JSON');

  const filter = filters[0];
  const chunks = [
    new TextEncoder().encode('{"items":'),
    new TextEncoder().encode('[1,2,3]}')
  ];
  chunks.forEach(chunk => filter.ondata({ data: chunk }));
  filter.onstop();

  assert.deepEqual(filter.written, chunks,
    'every chunk must be written back unmodified, in order');
  assert.equal(filter.closed, true, 'the filter must always be closed');

  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });
  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs[0].responseBody, '{"items":[1,2,3]}');
});

test('a chunk that cannot be decoded is still passed through', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { complete: false });
  const filter = filters[0];
  const bad = { byteLength: 4 };  // not a buffer: decode() will throw

  filter.ondata({ data: bad });
  assert.deepEqual(filter.written, [bad],
    'the page must receive the chunk even when logging cannot read it');
});

test('non-textual responses are not filtered at all', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'image/png' }]
  });

  assert.equal(filters.length, 0, 'binary responses must not be filtered');
  const result = await command('getNetworkLogs', {}, 7);
  assert.match(result.logs[0].responseBody, /non-textual/);
});

test('captureBodies: false attaches no filter', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', { captureBodies: false }, 7);

  fireRequest(webRequest);

  assert.equal(filters.length, 0);
  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1, 'metadata is still captured');
  assert.equal(result.logs[0].responseBody, undefined);
});

// --------------------------------------------------------------------------
// Housekeeping

test('clearLogs empties the network log for the tab', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest);

  await command('clearLogs', {}, 7);

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 0);
});

test('closing a tab discards its logs and detaches listeners', async () => {
  const { command, webRequest, tabsOnRemoved } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest);

  tabsOnRemoved.fire(7);

  assert.equal(webRequest.onBeforeRequest.hasListeners(), false);
  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 0);
});

// --------------------------------------------------------------------------
// Network idle, counted at the network layer rather than by hooking the page

test('waitForNetworkIdle resolves once the tab goes quiet', async () => {
  const { command, webRequest } = loadBackground();

  const waiting = command('waitForNetworkIdle', { idleTime: 150, timeout: 5000 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));

  // A request starts and finishes while we are waiting.
  webRequest.onBeforeRequest.fire({ requestId: 'r1', tabId: 7, type: 'image',
                                    method: 'GET', url: 'http://stub.test/a.png' });
  await new Promise(resolve => setTimeout(resolve, 50));
  webRequest.onCompleted.fire({ requestId: 'r1', tabId: 7, statusCode: 200 });

  const result = await waiting;
  assert.equal(result.idle, true);
  assert.equal(result.pendingRequests, 0);
});

test('waitForNetworkIdle times out while a request is still in flight', async () => {
  const { command, webRequest } = loadBackground();

  const waiting = command('waitForNetworkIdle', { idleTime: 100, timeout: 400 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  webRequest.onBeforeRequest.fire({ requestId: 'stuck', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://stub.test/slow' });

  const result = await waiting;
  assert.equal(result.idle, false);
  assert.equal(result.timedOut, true);
  assert.equal(result.pendingRequests, 1);
});

test('waitForNetworkIdle counts asset requests too', async () => {
  // A page still pulling images is not quiet, even though images are not logged.
  const { command, webRequest } = loadBackground();

  const waiting = command('waitForNetworkIdle', { idleTime: 100, timeout: 400 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  webRequest.onBeforeRequest.fire({ requestId: 'img', tabId: 7, type: 'image',
                                    method: 'GET', url: 'http://stub.test/big.png' });

  const result = await waiting;
  assert.equal(result.timedOut, true, 'an in-flight image must prevent idle');
});

test('waitForNetworkIdle takes its listeners away afterwards', async () => {
  const { command, webRequest } = loadBackground();

  await command('waitForNetworkIdle', { idleTime: 50, timeout: 300 }, 7);

  assert.equal(webRequest.onBeforeRequest.hasListeners(), false,
    'nothing else needed them, so they must be detached');
});

test('waitForNetworkIdle leaves an active logging session running', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  await command('waitForNetworkIdle', { idleTime: 50, timeout: 300 }, 7);

  assert.equal(webRequest.onBeforeRequest.hasListeners(), true,
    'logging still needs the listeners');
  fireRequest(webRequest);
  const logs = await command('getNetworkLogs', {}, 7);
  assert.equal(logs.logs.length, 1, 'logging must still be capturing');
});

test('waitForNetworkIdle does not pollute the network log', async () => {
  const { command, webRequest } = loadBackground();

  const waiting = command('waitForNetworkIdle', { idleTime: 100, timeout: 400 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  webRequest.onBeforeRequest.fire({ requestId: 'x', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://stub.test/api' });
  webRequest.onCompleted.fire({ requestId: 'x', tabId: 7, statusCode: 200 });
  await waiting;

  const logs = await command('getNetworkLogs', {}, 7);
  assert.equal(logs.logs.length, 0,
    'waiting for idle must not record entries; only logging does that');
});

// --------------------------------------------------------------------------

test('startLogging also starts console capture in the content script', async () => {
  const { command, contentMessages } = loadBackground();

  const result = await command('startLogging', {}, 7);

  assert.ok(contentMessages.some(m => m.message.action === 'startLogging'),
    'the content script must be told to start console capture');
  assert.equal(result.console.capturing, true);
  assert.equal(result.network.capturing, true);
  assert.equal(result.network.capturesFetch, true);
});

test('network logging survives a content script that is not reachable', async () => {
  const { command, webRequest } = loadBackground({
    contentScriptReply: { success: false, error: 'Receiving end does not exist' }
  });

  const result = await command('startLogging', {}, 7);
  assert.equal(result.network.capturing, true, 'network capture is independent');
  assert.equal(result.console.capturing, false);

  fireRequest(webRequest);
  const logs = await command('getNetworkLogs', {}, 7);
  assert.equal(logs.logs.length, 1);
});

// --------------------------------------------------------------------------
// The registration contract. details.requestBody / requestHeaders /
// responseHeaders are only populated when the matching extraInfoSpec entry is
// passed, and filterResponseData() is only callable from a "blocking"
// listener. Stripping these silently disables the feature in real Firefox.

test('listeners are registered with the extraInfoSpec the feature needs', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  assert.ok(webRequest.onBeforeRequest.spec().includes('requestBody'),
    'onBeforeRequest needs "requestBody" or details.requestBody is undefined');
  assert.ok(webRequest.onBeforeSendHeaders.spec().includes('requestHeaders'),
    'onBeforeSendHeaders needs "requestHeaders" or headers cannot be redacted');
  assert.ok(webRequest.onHeadersReceived.spec().includes('responseHeaders'),
    'onHeadersReceived needs "responseHeaders" or status/headers are undefined');
  assert.ok(webRequest.onHeadersReceived.spec().includes('blocking'),
    'onHeadersReceived needs "blocking" or filterResponseData() cannot be called');
  assert.deepEqual(Array.from(webRequest.onBeforeRequest.urls()), ['<all_urls>']);
});

// --------------------------------------------------------------------------
// The filter must always hand the stream back

test('the filter disconnects on error instead of holding the response open', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });

  const filter = filters[0];
  filter.error = 'NS_ERROR_ABORT';
  filter.onerror();

  assert.equal(filter.disconnected, true,
    'onerror must disconnect; Firefox keeps the response alive forever otherwise');
});

test('a failing write hands the stream back rather than truncating it', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });

  const filter = filters[0];
  filter.write = () => { throw new Error('not transferring data'); };
  filter.ondata({ data: new TextEncoder().encode('{"a":1}') });

  assert.equal(filter.disconnected, true,
    'a dropped chunk would truncate the page response; disconnect instead');
});

test('a filter is released exactly once', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });

  const filter = filters[0];
  let closes = 0;
  filter.close = () => { closes++; };
  filter.onstop();
  filter.onstop();
  filter.onerror();

  assert.equal(closes, 1, 'close must not be called twice');
});

test('an unfilterable request degrades without throwing', async () => {
  const { command, webRequest } = loadBackground();
  webRequest.filterResponseData = () => { throw new Error('no permission'); };
  await command('startLogging', {}, 7);

  fireRequest(webRequest);

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1, 'metadata must still be captured');
  assert.match(result.logs[0].responseBody, /unavailable/);
});

// --------------------------------------------------------------------------
// Redaction, in full

test('every credential header in the list is redacted', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  const names = [
    'Authorization', 'Proxy-Authorization', 'Cookie', 'X-API-Key',
    'X-Auth-Token', 'X-CSRF-Token', 'X-XSRF-Token', 'API-Key',
    'Auth-Token', 'X-Session-Token', 'X-Access-Token'
  ];
  fireRequest(webRequest, {
    requestHeaders: names.map((name, i) => ({ name, value: `secret-value-${i}` })),
    responseHeaders: [
      { name: 'content-type', value: 'application/json' },
      { name: 'Set-Cookie', value: 'secret-cookie' }
    ]
  });

  const result = await command('getNetworkLogs', {}, 7);
  const entry = result.logs[0];
  for (const name of names) {
    assert.equal(entry.requestHeaders[name], '***', `${name} must be redacted`);
  }
  assert.equal(entry.responseHeaders['Set-Cookie'], '***');
  assert.ok(!JSON.stringify(result).includes('secret-value'),
    'no redacted header value may survive anywhere in the result');
});

test('credential-shaped values inside bodies are scrubbed', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  const body = new TextEncoder().encode(
    '{"username":"albert","password":"hunter2","note":"keep"}');
  fireRequest(webRequest, { method: 'POST', requestBody: { raw: [{ bytes: body }] },
                            complete: false });

  filters[0].ondata({ data: new TextEncoder().encode(
    '{"access_token":"ey-secret","expires_in":3600}') });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const result = await command('getNetworkLogs', {}, 7);
  const entry = result.logs[0];
  assert.ok(!entry.requestBody.includes('hunter2'), 'request password must be scrubbed');
  assert.ok(entry.requestBody.includes('albert'), 'non-secret fields stay readable');
  assert.ok(!entry.responseBody.includes('ey-secret'), 'response token must be scrubbed');
  assert.ok(entry.responseBody.includes('expires_in'), 'the rest of the body stays');
});

test('capture_bodies: false suppresses request bodies too, not just responses', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', { captureBodies: false }, 7);

  const body = new TextEncoder().encode('{"password":"hunter2"}');
  fireRequest(webRequest, { method: 'POST', requestBody: { raw: [{ bytes: body }] } });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs[0].requestBody, undefined,
    'capture_bodies: false must mean no bodies in either direction');
  assert.ok(!JSON.stringify(result).includes('hunter2'));
});

// --------------------------------------------------------------------------
// State that used to survive a detach

test('wait_for_network_idle still works after a detach left requests in flight', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  // Two requests start and never finish, then logging stops.
  webRequest.onBeforeRequest.fire({ requestId: 'a', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://stub.test/a' });
  webRequest.onBeforeRequest.fire({ requestId: 'b', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://stub.test/b' });
  await command('stopLogging', {}, 7);

  const result = await command('waitForNetworkIdle', { idleTime: 50, timeout: 600 }, 7);
  assert.equal(result.idle, true,
    'stale in-flight ids must not poison the tab forever');
});

test('a persistent channel stops blocking idle once it is clearly long-lived', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  // A WebSocket that never closes: onCompleted will not fire until the page
  // tears it down, so counting it forever makes the tool useless on any SPA.
  webRequest.onBeforeRequest.fire({ requestId: 'ws', tabId: 7, type: 'websocket',
                                    method: 'GET', url: 'wss://stub.test/live' });
  await new Promise(resolve => setTimeout(resolve, 80));

  const result = await command(
    'waitForNetworkIdle', { idleTime: 30, timeout: 600, persistentAfter: 40 }, 7);
  assert.equal(result.idle, true,
    'a request older than persistentAfter must stop blocking idle');
  assert.equal(result.pendingRequests, 0);
});

test('a request younger than the threshold still blocks idle', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  webRequest.onBeforeRequest.fire({ requestId: 'fresh', tabId: 7,
                                    type: 'xmlhttprequest', method: 'GET',
                                    url: 'http://stub.test/slow' });

  const result = await command(
    'waitForNetworkIdle', { idleTime: 30, timeout: 300, persistentAfter: 10000 }, 7);
  assert.equal(result.timedOut, true, 'an in-flight request must still be waited for');
  assert.equal(result.pendingRequests, 1);
});

// --------------------------------------------------------------------------
// Redirects

test('each redirect hop is kept as its own log entry', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  webRequest.onBeforeRequest.fire({ requestId: 'r', tabId: 7, type: 'xmlhttprequest',
                                    method: 'POST', url: 'http://api.stub.test/login',
                                    requestBody: null });
  webRequest.onHeadersReceived.fire({ requestId: 'r', tabId: 7, statusCode: 302,
                                      responseHeaders: [], statusLine: 'HTTP/1.1 302' });
  webRequest.onBeforeRedirect.fire({ requestId: 'r', tabId: 7, statusCode: 302,
                                     redirectUrl: 'http://api.stub.test/session' });
  // The target hop reuses the same requestId.
  webRequest.onBeforeRequest.fire({ requestId: 'r', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://api.stub.test/session',
                                    requestBody: null });
  webRequest.onCompleted.fire({ requestId: 'r', tabId: 7, statusCode: 200 });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 2, 'the first hop must not be overwritten');
  assert.equal(result.logs[0].url, 'http://api.stub.test/login');
  assert.equal(result.logs[0].method, 'POST');
  assert.equal(result.logs[0].redirectedTo, 'http://api.stub.test/session');
  assert.equal(result.logs[1].url, 'http://api.stub.test/session');
});

test('a redirect does not attach two filters to one channel', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  const html = [{ name: 'content-type', value: 'text/html' }];
  webRequest.onBeforeRequest.fire({ requestId: 'r', tabId: 7, type: 'xmlhttprequest',
                                    method: 'GET', url: 'http://a.test/' });
  webRequest.onHeadersReceived.fire({ requestId: 'r', tabId: 7, statusCode: 200,
                                      responseHeaders: html, statusLine: 'ok' });
  webRequest.onHeadersReceived.fire({ requestId: 'r', tabId: 7, statusCode: 200,
                                      responseHeaders: html, statusLine: 'ok' });

  assert.equal(filters.length, 1,
    'two filters on one request would race over the captured body');
});

// --------------------------------------------------------------------------
// Eviction

test('an evicted in-flight request is logged rather than silently dropped', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  // Fill the pending map past its cap with requests that never finish.
  for (let i = 0; i < 302; i++) {
    webRequest.onBeforeRequest.fire({ requestId: `p${i}`, tabId: 7,
                                      type: 'xmlhttprequest', method: 'GET',
                                      url: `http://stub.test/${i}` });
  }

  const result = await command('getNetworkLogs', { limit: 500 }, 7);
  const dropped = result.logs.filter(l => /dropped/.test(l.error || ''));
  assert.ok(dropped.length > 0, 'eviction must leave a marker in the log');
  assert.equal(dropped[0].url, 'http://stub.test/0', 'the oldest is the one evicted');
});

// --------------------------------------------------------------------------

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    failed++;
    console.log(`FAIL ${name}`);
    console.log(`     ${err.message.split('\n').join('\n     ')}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
