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

// A destructuring default cannot express "the content script resolved with
// undefined", which is the real Firefox behaviour for a receiver that exists
// but returns neither true nor a Promise. SILENT is that case.
const SILENT = Symbol('content script answered nothing');

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
  const webNavigationOnCommitted = makeEvent();
  const privateTabs = new Set();
  let tabGetRejects = false;
  let tabUrl = 'http://stub.test/';
  const messageListeners = [];
  const externalListeners = [];
  const createdWindows = [];
  const removedWindows = [];
  let windowCreateFails = false;
  const nativeMessages = [];
  const menuListeners = [];

  const browserStub = {
    runtime: {
      connectNative: () => ({
        onMessage: { addListener() {} },
        onDisconnect: { addListener() {} },
        // Recorded, not swallowed: what the extension says to the native
        // host is the whole of what a context-menu item does.
        postMessage(message) { nativeMessages.push(message); }
      }),
      onMessage: { addListener: (fn) => messageListeners.push(fn) },
      onMessageExternal: { addListener: (fn) => externalListeners.push(fn) },
      id: 'ccb@stub',
      getURL: (p) => `moz-extension://stub/${p}`
    },
    tabs: {
      // Honours currentWindow, and the tabs carry the fields Firefox sets.
      // The stub used to discard the query object and return one bare tab, so
      // getAllTabs' privacy default - the current window, not the user's
      // whole browsing surface - was only ever asserted through the `scope`
      // string it reports. Swapping {currentWindow: true} for {} kept the
      // suite green.
      query: async (q = {}) => {
        const all = [
          { id: 7, windowId: 1, url: 'http://stub.test/', title: 'stub',
            active: true, pinned: false, status: 'complete', audible: false,
            discarded: false, index: 0, favIconUrl: 'http://stub.test/f.ico' },
          { id: 9, windowId: 2, url: 'http://other-window.test/',
            title: 'other', active: true, pinned: false, status: 'complete',
            audible: false, discarded: false, index: 0 }
        ];
        let out = all;
        if (q.currentWindow) out = out.filter(t => t.windowId === 1);
        if (q.active !== undefined) out = out.filter(t => t.active === q.active);
        return out;
      },
      get: async (id) => {
        if (tabGetRejects) throw new Error('No tab with id ' + id);
        return { id, windowId: 1, url: tabUrl, title: 'stub',
                 incognito: privateTabs.has(id) };
      },
      sendMessage: async (tabId, message) => {
        contentMessages.push({ tabId, message });
        return contentScriptReply === SILENT ? undefined : contentScriptReply;
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
      update: async () => ({}),
      create: async (options) => {
        if (windowCreateFails) throw new Error('no window manager');
        const win = { id: 900 + createdWindows.length, ...options };
        createdWindows.push(win);
        return win;
      },
      remove: async (id) => { removedWindows.push(id); }
    },
    webRequest,
    webNavigation: { onCommitted: webNavigationOnCommitted },
    notifications: { create: async () => 'id' },
    contextMenus: {
      create() {},
      // The listener was discarded, so neither menu item was reachable from
      // a test - which is how "Take Screenshot for Claude" could post an
      // action the native host has no handler for, and do nothing at all,
      // for its entire life.
      onClicked: { addListener: (fn) => menuListeners.push(fn) }
    }
  };

  const sandbox = {
    browser: browserStub,
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    TextDecoder,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, Map, Set, RegExp, JSON, Math, Promise, Error,
    URL, URLSearchParams
  };
  sandbox.globalThis = sandbox;

  const context = createContext(sandbox);
  runInContext(SOURCE, context, { filename: 'background.js' });

  // handleCommand is the native host's entry point into the extension.
  const command = (action, data, tabId) =>
    context.handleCommand({ action, data, tabId });

  // Deliver a message as the extension's own page would (no sender.tab), or
  // as a content script would (sender.tab set).
  const deliver = (message, sender) => new Promise((resolve) => {
    for (const listener of messageListeners) {
      const returned = listener(message, sender, resolve);
      if (returned === true) return;
    }
    resolve(undefined);
  });

  return {
    context, command, webRequest, filters, contentMessages, tabsOnRemoved,
    createdWindows, removedWindows, deliver, externalListeners,
    webNavigationOnCommitted,
    failWindowCreate: () => { windowCreateFails = true; },
    markPrivate: (id) => privateTabs.add(id),
    failTabGet: () => { tabGetRejects = true; },
    setTabUrl: (url) => { tabUrl = url; },
    nativeMessages,
    clickMenuItem: async (menuItemId, info = {}) => {
      for (const fn of menuListeners) {
        await fn({ menuItemId, ...info }, { id: 7, url: 'http://stub.test/',
                                            title: 'stub' });
      }
      // The handlers are fire-and-forget, so let their promises settle.
      await new Promise(resolve => setTimeout(resolve, 0));
    },
    extensionSender: { id: 'ccb@stub' },
    contentScriptSender: { id: 'ccb@stub', tab: { id: 7 } },
  };
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

// The real matcher, lifted from the source: the pattern alone is not the
// decision - looksLikeCredentialName normalises camelCase first, and testing
// the pattern directly is what let a regression through.
function loadNameMatcher() {
  const parts = [
    SOURCE.match(/const SECRET_KEY_RE =\n  \/.*\/i;/),
    SOURCE.match(/function normaliseNameForMatching[\s\S]*?\n\}/),
    SOURCE.match(/function looksLikeCredentialName[\s\S]*?\n\}/)
  ];
  for (const [i, m] of parts.entries()) {
    assert.ok(m, `could not lift credential-name part ${i} from background.js`);
  }
  return eval(parts.map(m => m[0]).join('\n') + '\nlooksLikeCredentialName');
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

test('invalid utf-8 bytes are passed through and not logged as text', async () => {
  // The old fixture was `{ byteLength: 4 }` - not a buffer, so decode() threw
  // and the test exercised a catch that real input never reaches:
  // TextDecoder is non-fatal by default and yields U+FFFD rather than
  // throwing. These are bytes Firefox really can deliver.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { complete: false });
  const filter = filters[0];
  const invalid = new Uint8Array([0x7b, 0xff, 0xfe, 0x80, 0x81, 0x82, 0x7d]);

  filter.ondata({ data: invalid });
  assert.deepEqual(Array.from(filter.written[0]), Array.from(invalid),
    'the page must receive the chunk even when logging cannot read it');

  filter.onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });
  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.match(entry.responseBody, /not captured/,
    'replacement characters are binary noise, not the page content');
});

test('a chunk that genuinely throws on decode is still passed through', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, { complete: false });
  const filter = filters[0];
  const bad = { byteLength: 4 };  // not a buffer: decode() will throw

  filter.ondata({ data: bad });
  assert.deepEqual(filter.written, [bad],
    'logging must never be able to stop the page receiving its data');
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

test('the credential-name list matches credentials and not ordinary words', () => {
  // These lists are derived from names real systems use - WebAuthn, OAuth,
  // React, the DOM - NOT from reading the pattern. An earlier version of this
  // test was fitted to the implementation: it listed the camelCase names that
  // happened to pass and omitted every one that failed, so it certified a
  // regex that had silently stopped matching passkey, otpCode, sessionValue,
  // authData, authz, authn and oauth_verifier.
  const matches = loadNameMatcher();

  const credentials = [
    // Passwords, in the spellings forms actually use.
    'password', 'passwd', 'passphrase', 'passcode', 'pass', 'pwd',
    'user[password]', 'userpass',
    // WebAuthn.
    'passkey', 'passKey',
    // One-time codes.
    'one-time-code', 'otp', 'otp_code', 'otpCode', 'otpValue',
    // OAuth and HTTP auth.
    'authorization', 'Authorization', 'authentication', 'auth', 'x-auth',
    'auth_token', 'authToken', 'authData', 'authz', 'authn',
    'oauth', 'oauth_verifier',
    // Keys and tokens.
    'secret', 'client_secret', 'token', 'access_token', 'refresh_token',
    'credential', 'api_key', 'apiKey', 'private_key', 'jwt', 'bearer',
    'signature',
    // Sessions, including the servlet and PHP cookie names.
    'session', 'session_id', 'sessionToken', 'sessionValue',
    'JSESSIONID', 'PHPSESSID',
    // Card and identity.
    'cvv', 'cvc', 'card_number', 'cardNumber', 'ssn', 'pin', 'PIN', 'pinCode'
  ];
  for (const name of credentials) {
    assert.ok(matches(name), `${name} must be treated as a credential`);
  }

  const ordinary = [
    // The ones anchoring was introduced to stop masking.
    'author', 'authors', 'authored', 'passed', 'passenger', 'bypass',
    'bypassCache', 'compass', 'notPublished', 'shipping', 'mapping',
    'spinner', 'pinned',
    // cla-SSN-ame: the single most common key in a React-shaped payload.
    'className', 'classNames', 'businessName', 'addressName', 'witnessName',
    'accessName', 'guessNumber',
    // Ordinary payload fields.
    'email', 'username', 'title', 'views', 'published', 'tags', 'id', 'name'
  ];
  for (const name of ordinary) {
    assert.ok(!matches(name),
              `${name} is not a credential and must stay readable`);
  }

  // Accepted over-redaction, recorded so it is a decision rather than a bug.
  // A bare `session` followed by a separator is what catches `session=abcdef`
  // in a cookie-shaped body, and once camelCase is normalised to a separator
  // that rule cannot tell `sessionCount` from `sessionValue`. Masking a count
  // in a log costs little; missing a session token costs the session. Neither
  // appears in captured JavaScript, because the text passes need a `:` or `=`
  // straight after the name, so `sessionStorage.setItem(...)` is untouched.
  for (const name of ['session_duration', 'sessionCount', 'sessionStorage']) {
    assert.ok(matches(name), `${name} is expected to be over-redacted`);
  }
});

test('anchoring a name rule never loses a name the loose version caught', () => {
  // The regression guard for what actually went wrong: anchoring `auth` so it
  // would stop matching `author` also stopped `otpCode` and friends matching
  // at all, because the anchors only recognise a non-letter as a boundary.
  // Nothing the original unanchored pattern treated as a credential may be
  // dropped by a later tightening.
  const matches = loadNameMatcher();
  const ORIGINAL = /(pass(word|wd)?|pwd|secret|token|otp|one[-_]?time[-_]?code|auth|credential|api[-_]?key|private[-_]?key|session|cvv|card[-_]?number|ssn)/i;
  const names = [
    'passkey', 'passKey', 'userpass', 'otpCode', 'otpValue', 'oauth_verifier',
    'authz', 'authn', 'sessionValue', 'authData', 'pinCode', 'cardNumber',
    'password', 'api_key', 'private_key', 'JSESSIONID'
  ];
  const lost = names.filter(n => ORIGINAL.test(n) && !matches(n));
  assert.deepEqual(lost, [],
    `tightening dropped credential names the loose pattern caught: ${lost}`);
});

test('the DOM guard and the body scrubber use the same name list', () => {
  // The two have to agree, or a field is *** in the network log and
  // plaintext from browser_get_value - which is exactly what happened.
  const contentSource = readFileSync(
    join(here, '..', 'extension', 'content.js'), 'utf8');
  const fromBackground = SOURCE.match(/const SECRET_KEY_RE =\n  \/(.*)\/i;/)[1];
  const fromContent = contentSource.match(
    /const CREDENTIAL_NAME_RE =\n    \/(.*)\/i;/)[1];
  assert.equal(fromContent, fromBackground,
    'content.js CREDENTIAL_NAME_RE has drifted from background.js SECRET_KEY_RE');

  // The pattern alone is not the decision: both sides must also normalise
  // camelCase before testing, or the anchors silently stop matching otpCode,
  // sessionValue and authData - which is exactly what happened once.
  for (const [where, src] of [['background.js', SOURCE],
                              ['content.js', contentSource]]) {
    assert.match(src, /replace\(\/\(\[a-z0-9\]\)\(\[A-Z\]\)\/g, '\$1_\$2'\)/,
      `${where} must normalise camelCase before matching a credential name`);
  }
});

test('a credential nested deeper than the walk limit is not logged', async () => {
  // The structural walk returned the raw subtree past MAX_REDACT_DEPTH, so a
  // credential nested deeper than 12 was logged verbatim - and the flat regex
  // this replaced scrubbed one at any depth. Depth 13 is ordinary in GraphQL
  // and paginated responses.
  const deep = (n) => {
    let o = { password: 'hunter2' };
    for (let i = 0; i < n; i++) o = { a: o };
    return JSON.stringify(o);
  };
  for (const depth of [5, 12, 13, 31]) {
    const { command, webRequest } = loadBackground();
    await command('startLogging', {}, 7);
    fireRequest(webRequest, {
      method: 'POST',
      requestBody: { raw: [{ bytes: new TextEncoder().encode(deep(depth)) }] }
    });
    const body = (await command('getNetworkLogs', {}, 7)).logs[0].requestBody;
    assert.ok(!body.includes('hunter2'),
              `a credential at depth ${depth} was logged: ${body.slice(0, 120)}`);
  }
});

test('a boolean under a credential name is kept, a number is not', async () => {
  // `authenticated: true` was replaced with ***, which destroys the one field
  // that tells you whether the login you are debugging actually worked. A
  // boolean is never a credential whatever the key is called. A NUMBER is not
  // exempt: an OTP or a PIN is a number and is exactly what must be hidden.
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  const body = JSON.stringify({
    authenticated: true, isAuthenticated: false, passwordSet: true,
    sessionValid: null, otp: 483920, password: 'hunter2'
  });
  fireRequest(webRequest, {
    method: 'POST',
    requestBody: { raw: [{ bytes: new TextEncoder().encode(body) }] }
  });

  const logged = (await command('getNetworkLogs', {}, 7)).logs[0].requestBody;
  const parsed = JSON.parse(logged);
  assert.equal(parsed.authenticated, true, 'a boolean must survive');
  assert.equal(parsed.isAuthenticated, false);
  assert.equal(parsed.passwordSet, true);
  assert.equal(parsed.sessionValid, null);
  assert.equal(parsed.otp, '***', 'a numeric OTP is a credential');
  assert.equal(parsed.password, '***');
});

test('a captured body keeps its numbers exactly when nothing is redacted', async () => {
  // Redaction re-serialised every JSON body, and JSON.stringify(JSON.parse(x))
  // turns 12345678901234567890 into 12345678901234567000, 1e400 into null and
  // 1.0 into 1 - so a Snowflake- or Twitter-style id in a captured body came
  // back silently wrong, which is the kind of thing you capture a body to look
  // at in the first place.
  const cases = ['{"orderId": 12345678901234567890}', '{"big": 1e400}',
                 '{"amount": 1.0}'];
  for (const raw of cases) {
    const { command, webRequest, filters } = loadBackground();
    await command('startLogging', {}, 7);
    fireRequest(webRequest, { complete: false });
    filters[0].ondata({ data: new TextEncoder().encode(raw) });
    filters[0].onstop();
    webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

    const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
    assert.equal(entry.responseBody, raw,
      `the body was rewritten although nothing needed redacting: ${raw}`);
  }
});

test('an HTML form login does not log the password', async () => {
  // webRequest hands an ordinary <form method=POST> over as requestBody
  // .formData, whose values are ARRAYS. Every fixture here used raw bytes, so
  // the one request that matters most - a login - was never exercised, and
  // the scrubber's string passes cannot see "password":["hunter2"].
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, {
    method: 'POST',
    requestBody: { formData: {
      username: ['albert'], password: ['hunter2'], csrf_token: ['tok-123'] } }
  });

  const result = await command('getNetworkLogs', {}, 7);
  const body = result.logs[0].requestBody;
  assert.ok(!body.includes('hunter2'), `password leaked: ${body}`);
  assert.ok(!body.includes('tok-123'), `csrf token leaked: ${body}`);
  assert.ok(body.includes('albert'), 'the username stays readable');
});

test('credential values that are not quoted strings are scrubbed too', async () => {
  // The old pass matched only "key":"string", so a numeric OTP, an array of
  // tokens and a nested credential object all went through verbatim.
  const cases = [
    ['{"password":1234,"otp":654321}', ['1234', '654321']],
    ['{"access_tokens":["tok-aaa","tok-bbb"]}', ['tok-aaa', 'tok-bbb']],
    ['{"auth":{"value":"tok-zzz"}}', ['tok-zzz']],
    ['{"user":{"profile":{"api_key":"deep-secret"}}}', ['deep-secret']],
    ['pwd=hunter2&next=/home', ['hunter2']],
    ['<login password="hunter2"/>', ['hunter2']],
    ['pin=4321', ['4321']]
  ];
  for (const [raw, secrets] of cases) {
    const { command, webRequest } = loadBackground();
    await command('startLogging', {}, 7);
    fireRequest(webRequest, {
      method: 'POST',
      requestBody: { raw: [{ bytes: new TextEncoder().encode(raw) }] }
    });
    const result = await command('getNetworkLogs', {}, 7);
    const body = result.logs[0].requestBody;
    for (const secret of secrets) {
      assert.ok(!body.includes(secret),
                `${secret} leaked from ${raw}: got ${body}`);
    }
  }
});

test('a prefilled password inside a JSON string is scrubbed', async () => {
  // The structural path short-circuits the markup pass, so a JSON-wrapped
  // server-rendered form had to keep going through it.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });
  filters[0].ondata({ data: new TextEncoder().encode(
    '{"html":"<input type=\\"password\\" value=\\"hunter2\\">","ok":true}') });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(!entry.responseBody.includes('hunter2'),
            `password leaked inside a JSON string: ${entry.responseBody}`);
  assert.ok(entry.responseBody.includes('input'),
            'the rest of the markup stays readable');
});

test('a multipart login frame does not log the password', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  const raw = [
    '--X', 'Content-Disposition: form-data; name="username"', '', 'albert',
    '--X', 'Content-Disposition: form-data; name="password"', '', 'hunter2',
    '--X--', ''
  ].join('\r\n');
  fireRequest(webRequest, {
    method: 'POST',
    requestBody: { raw: [{ bytes: new TextEncoder().encode(raw) }] }
  });
  const result = await command('getNetworkLogs', {}, 7);
  const body = result.logs[0].requestBody;
  assert.ok(!body.includes('hunter2'), `password leaked: ${body}`);
  assert.ok(body.includes('albert'), 'the username stays readable');
});

test('scrubbing a captured script does not destroy the source around it', async () => {
  // The form-encoded pass was unanchored with a loose value class, so it ate
  // whatever followed an assignment: this body came back as
  // `const apiKey=*** sessionId=***`, eleven characters of source gone, and
  // `a.password==="x"` became `a.password=***"x"` - mangled and still leaking.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'application/javascript' }],
    complete: false
  });
  filters[0].ondata({ data: new TextEncoder().encode(
    'const apiKey=process.env.KEY;let count=1;if(a.password==="x"){}') });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(entry.responseBody.includes('let count=1;'),
            `the following statement was destroyed: ${entry.responseBody}`);
  assert.ok(!entry.responseBody.includes('a.password=***"x"'),
            `a comparison was mangled: ${entry.responseBody}`);
});

test('one oversized chunk cannot stall the background script', async () => {
  // The length check happens before appending, so a single chunk - and
  // Firefox can deliver a whole response in one - landed in full and the
  // scrubber ran over all of it. Its passes are quadratic on adversarial
  // input, and this is the single-threaded background script: a page serving
  // a few hundred KB of quote marks could stall every tool call for minutes.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });

  const hostile = new TextEncoder().encode('"'.repeat(400000));
  const started = Date.now();
  filters[0].ondata({ data: hostile });
  filters[0].onstop();
  const elapsed = Date.now() - started;
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  assert.ok(elapsed < 2000,
    `a page-controlled body took ${elapsed}ms of the background script`);

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(entry.responseBody.length <= 5000,
            'the kept body must still respect the cap');
  assert.equal(entry.responseBodyTruncated, true);
  assert.equal(entry.responseBodyBytes, 400000,
               'the real size is still reported honestly');
});

test('a credential straddling the body cap is still scrubbed', async () => {
  // The response path truncated and then scrubbed, so a secret cut in half
  // survived as a fragment: the unterminated string matched nothing.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });
  const padding = 'x'.repeat(4960);
  filters[0].ondata({ data: new TextEncoder().encode(
    `{"pad":"${padding}","access_token":"SECRET-JWT-VALUE-abcdefgh"}`) });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(!entry.responseBody.includes('SECRET-JWT-'),
            `a fragment of the token survived: ${entry.responseBody.slice(-80)}`);
});

test('a body truncated across several chunks says so, with its real size', async () => {
  // Collection stops at the cap, so collected.length never exceeded it by
  // more than one chunk - and landed exactly on it when the chunks divided
  // evenly, which left truncation entirely unflagged.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { complete: false });
  const chunk = new TextEncoder().encode('y'.repeat(2500));
  filters[0].ondata({ data: chunk });
  filters[0].ondata({ data: chunk });
  filters[0].ondata({ data: chunk });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.equal(entry.responseBodyTruncated, true,
               'a 7500-character body cut to 5000 must be flagged');
  assert.equal(entry.responseBodyBytes, 7500,
               'the reported size must be the real one, not the cap');
});

test('a JSON body is decoded as utf-8 whatever charset it claims', async () => {
  // RFC 8259 requires UTF-8 for application/json and says the charset
  // parameter must be ignored. Older stacks send charset=ISO-8859-1 while
  // emitting UTF-8, and trusting the label turned café into cafÃ© with no
  // note, because iso-8859-1 is a charset TextDecoder knows.
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, {
    responseHeaders: [{ name: 'content-type',
                        value: 'application/json;charset=ISO-8859-1' }],
    complete: false
  });
  filters[0].ondata({ data: new TextEncoder().encode('{"city":"café"}') });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(entry.responseBody.includes('café'),
            `mojibake in the log: ${entry.responseBody}`);
  assert.equal(entry.responseCharset, 'utf-8');
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
// Human approval happens outside the page being automated

test('approval opens an extension window, not an in-page banner', async () => {
  const ctx = loadBackground();

  const pending = ctx.command('requestApproval',
    { message: 'run a thing', detail: '{"script":"x"}', timeout: 5000 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));

  assert.equal(ctx.createdWindows.length, 1, 'a window should have been opened');
  const win = ctx.createdWindows[0];
  assert.match(win.url, /^moz-extension:\/\/stub\/approve\/approve\.html\?id=/,
    'the prompt must be an extension page, not page DOM');
  assert.equal(win.type, 'popup');
  assert.ok(!ctx.contentMessages.some(m => m.message.action === 'requestApproval'),
    'the in-page banner must not be used when a window is available');

  // The page reports the decision.
  const id = decodeURIComponent(new URL(win.url).searchParams.get('id'));
  await ctx.deliver({ target: 'approval', action: 'decide', requestId: id,
                      approved: true }, ctx.extensionSender);

  const result = await pending;
  assert.equal(result.approved, true);
  assert.equal(result.promptSurface, 'window');
  assert.deepEqual(Array.from(ctx.removedWindows), [win.id],
    'the window should be closed once decided');
});

test('the approval page can read its own request details', async () => {
  const ctx = loadBackground();
  ctx.command('requestApproval',
    { message: 'delete everything', detail: 'the detail', protectedUrl: 'https://bank.test/',
      timeout: 4000 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));

  const id = decodeURIComponent(
    new URL(ctx.createdWindows[0].url).searchParams.get('id'));
  const details = await ctx.deliver(
    { target: 'approval', action: 'details', requestId: id }, ctx.extensionSender);

  assert.equal(details.found, true);
  assert.equal(details.message, 'delete everything');
  assert.equal(details.detail, 'the detail',
    'the person must see what will run, not a redaction');
  assert.equal(details.protectedUrl, 'https://bank.test/');
});

test('a content script cannot decide an approval', async () => {
  const ctx = loadBackground();
  const pending = ctx.command('requestApproval', { message: 'x', timeout: 300 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  const id = decodeURIComponent(
    new URL(ctx.createdWindows[0].url).searchParams.get('id'));

  // sender.tab set: this is a page's content script, not our prompt.
  const reply = await ctx.deliver(
    { target: 'approval', action: 'decide', requestId: id, approved: true },
    ctx.contentScriptSender);
  assert.equal(reply.found, false, 'a tab must not be able to approve');

  const result = await pending;
  assert.equal(result.approved, false, 'it must fall through to the timeout');
  assert.equal(result.timedOut, true);
});

test('another extension cannot decide an approval', async () => {
  const ctx = loadBackground();
  const pending = ctx.command('requestApproval', { message: 'x', timeout: 300 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  const id = decodeURIComponent(
    new URL(ctx.createdWindows[0].url).searchParams.get('id'));

  await ctx.deliver({ target: 'approval', action: 'decide', requestId: id,
                      approved: true }, { id: 'someone-else@evil' });

  const result = await pending;
  assert.equal(result.approved, false);
});

test('closing the window without answering is a denial', async () => {
  const ctx = loadBackground();
  const pending = ctx.command('requestApproval', { message: 'x', timeout: 5000 }, 7);
  await new Promise(resolve => setTimeout(resolve, 20));
  const id = decodeURIComponent(
    new URL(ctx.createdWindows[0].url).searchParams.get('id'));

  await ctx.deliver({ target: 'approval', action: 'decide', requestId: id,
                      approved: false, closed: true }, ctx.extensionSender);

  const result = await pending;
  assert.equal(result.approved, false);
  assert.equal(result.closedWithoutAnswering, true);
});

test('with no window manager it falls back and says the prompt is degraded', async () => {
  const ctx = loadBackground();
  ctx.failWindowCreate();

  const result = await ctx.command('requestApproval', { message: 'x', timeout: 500 }, 7);

  assert.ok(ctx.contentMessages.some(m => m.message.action === 'requestApproval'),
    'it should fall back to the in-page banner');
  assert.equal(result.promptSurface, 'page');
  assert.equal(result.degraded, true,
    'a prompt sharing the DOM with the page is not equivalent; say so');
});

test('external messages are still refused outright', async () => {
  const ctx = loadBackground();
  assert.equal(ctx.externalListeners.length, 1,
    'onMessageExternal must have a handler that refuses');

  let replied = null;
  ctx.externalListeners[0](
    { action: 'executeScript', data: { script: 'evil' } },
    { id: 'other@extension' },
    (response) => { replied = response; });

  assert.equal(replied.success, false,
    'a co-installed extension must not be able to issue commands');
});

// --------------------------------------------------------------------------
// Flags arriving as strings. Observed live: the MCP client sent
// include_all_types: "true", and === true rejected it, so a documented option
// silently did nothing. capture_bodies: "false" was worse - it would have
// captured bodies for a caller who asked for none.

test('include_all_types works when it arrives as the string "true"', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', { includeAllTypes: 'true' }, 7);

  fireRequest(webRequest, { requestId: 'img', type: 'image',
                            url: 'http://stub.test/logo.png' });

  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1,
    'a string "true" must enable asset logging, as the boolean does');
  assert.equal(result.logs[0].type, 'image');
});

test('capture_bodies fails CLOSED when it arrives as the string "false"', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', { captureBodies: 'false' }, 7);

  const body = new TextEncoder().encode('{"password":"hunter2"}');
  fireRequest(webRequest, { method: 'POST', requestBody: { raw: [{ bytes: body }] } });

  assert.equal(filters.length, 0, 'no response filter should be attached');
  const result = await command('getNetworkLogs', {}, 7);
  assert.equal(result.logs[0].requestBody, undefined,
    'a privacy option must not fail open because it arrived as a string');
  assert.ok(!JSON.stringify(result).includes('hunter2'));
});

test('flag parsing accepts the usual spellings and ignores nonsense', async () => {
  for (const [value, expected] of [
    [true, 1], ['true', 1], ['TRUE', 1], [' true ', 1], ['1', 1], [1, 1],
    ['yes', 1], ['on', 1],
    [false, 0], ['false', 0], ['0', 0], [0, 0], ['no', 0], ['off', 0],
    // Unparseable values fall back to the default (false here), rather than
    // being treated as true because they are truthy strings.
    ['maybe', 0], ['', 0], [null, 0], [undefined, 0],
  ]) {
    const { command, webRequest } = loadBackground();
    await command('startLogging', { includeAllTypes: value }, 7);
    fireRequest(webRequest, { requestId: 'i', type: 'image',
                              url: 'http://stub.test/x.png' });
    const result = await command('getNetworkLogs', {}, 7);
    assert.equal(result.logs.length, expected,
      `includeAllTypes: ${JSON.stringify(value)} should give ${expected} log(s)`);
  }
});

test('clear_existing works as a string too', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest);
  assert.equal((await command('getNetworkLogs', {}, 7)).logs.length, 1);

  await command('startLogging', { clearExisting: 'true' }, 7);
  assert.equal((await command('getNetworkLogs', {}, 7)).logs.length, 0);
});

// --------------------------------------------------------------------------
// Markup bodies. Found by live testing: the scrubber handled JSON and
// form-encoded payloads, so a server-rendered form arrived with the password
// in a value="..." attribute while the DOM-level guard was correctly
// returning "***" for the very same field.

test('credential values in HTML attributes are scrubbed from captured bodies', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', { includeAllTypes: true }, 7);

  fireRequest(webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'text/html' }],
    complete: false
  });

  const html =
    '<form>' +
    '<input id="user" type="text" name="username" value="albert">' +
    '<input id="pw" type="password" name="password" value="SuperSecret123!">' +
    '<input id="csrf" type="hidden" name="csrf_token" value="csrf-abc-123">' +
    '<input type=text name=api_token value=bare-unquoted>' +
    "<input type='password' name='pw2' value='single-quoted'>" +
    '</form>';
  filters[0].ondata({ data: new TextEncoder().encode(html) });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const result = await command('getNetworkLogs', {}, 7);
  const body = result.logs[0].responseBody;

  for (const secret of ['SuperSecret123!', 'csrf-abc-123', 'bare-unquoted',
                        'single-quoted']) {
    assert.ok(!body.includes(secret), `${secret} must not survive in the body`);
  }
  assert.ok(body.includes('albert'),
    'a non-credential field must still be readable');
  assert.ok(body.includes('<form>'), 'the markup itself must stay intact');
});

test('ordinary markup is not mangled by the scrubber', async () => {
  const { command, webRequest, filters } = loadBackground();
  await command('startLogging', { includeAllTypes: true }, 7);

  fireRequest(webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'text/html' }],
    complete: false
  });

  const html = '<a href="/x?page=2">Next</a><input type="text" name="q" value="search terms">';
  filters[0].ondata({ data: new TextEncoder().encode(html) });
  filters[0].onstop();
  webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const body = (await command('getNetworkLogs', {}, 7)).logs[0].responseBody;
  assert.ok(body.includes('search terms'), 'a search box is not a credential');
  assert.ok(body.includes('href="/x?page=2"'), 'links must survive intact');
});

// --------------------------------------------------------------------------
// A logging session belongs to a page, not to a tab id

test('navigating the tab to another origin stops capture', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);
  fireRequest(ctx.webRequest);
  assert.equal((await ctx.command('getNetworkLogs', {}, 7)).logs.length, 1);

  // The user types their bank's URL into the same tab.
  ctx.webNavigationOnCommitted.fire({ tabId: 7, frameId: 0,
                                      url: 'https://bank.example/accounts' });
  await new Promise(resolve => setTimeout(resolve, 10));

  fireRequest(ctx.webRequest, { requestId: 'after',
                                url: 'https://bank.example/api/balance' });
  const logs = await ctx.command('getNetworkLogs', {}, 7);
  assert.ok(!JSON.stringify(logs).includes('bank.example/api/balance'),
    'a session started elsewhere must not capture the new origin');
  assert.equal(logs.loggingEnabled, false, 'the session should have ended');
});

test('navigating within the same origin keeps capture running', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  ctx.webNavigationOnCommitted.fire({ tabId: 7, frameId: 0,
                                      url: 'http://stub.test/other-page' });
  await new Promise(resolve => setTimeout(resolve, 10));

  fireRequest(ctx.webRequest, { requestId: 'same' });
  const logs = await ctx.command('getNetworkLogs', {}, 7);
  assert.equal(logs.logs.length, 1, 'same-origin navigation is not a new page');
  assert.equal(logs.loggingEnabled, true);
});

test('a subframe navigating does not end the session', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  // An ad iframe navigating must not stop the tab's logging.
  ctx.webNavigationOnCommitted.fire({ tabId: 7, frameId: 3,
                                      url: 'https://ads.example/frame' });
  await new Promise(resolve => setTimeout(resolve, 10));

  fireRequest(ctx.webRequest, { requestId: 'still' });
  assert.equal((await ctx.command('getNetworkLogs', {}, 7)).logs.length, 1);
});

// --------------------------------------------------------------------------
// Flags and fields the caller reasons about

test('reload_all bypasses the cache by default, as its schema says', async () => {
  const ctx = loadBackground();
  const reloads = [];
  ctx.context.browser.tabs.reload = async (id, options) => { reloads.push(options); };
  ctx.context.browser.tabs.query = async () => [{ id: 1, url: 'http://a.test/' }];

  await ctx.command('reloadAll', {}, undefined);
  assert.equal(reloads.length, 1);
  assert.equal(reloads[0].bypassCache, true,
    'the tool exists for picking up a restarted dev server');
});

test('full_page says plainly that it did not capture a full page', async () => {
  const ctx = loadBackground();
  const result = await ctx.command('screenshot', { fullPage: true }, 7);

  assert.equal(result.fullPageRequested, true);
  assert.equal(result.fullPageCaptured, false,
    'it has never worked; silently returning the viewport is worse');
  assert.match(result.note, /not supported/i);
  assert.equal(result.type, 'visible');
});

test('internal filter bookkeeping does not reach the caller', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);
  fireRequest(ctx.webRequest);

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.equal(entry.filterAttached, undefined,
    'filterAttached is bookkeeping, not something to reason about');
});

// --------------------------------------------------------------------------
// Scope and caps

test('logging a private-browsing tab is refused', async () => {
  const ctx = loadBackground();
  ctx.markPrivate(7);

  const result = await ctx.command('startLogging', {}, 7);

  assert.equal(result.success, false,
    'private windows exist so their contents are not retained');
  assert.match(result.error, /private/i);

  // And nothing is captured for it.
  fireRequest(ctx.webRequest);
  const logs = await ctx.command('getNetworkLogs', {}, 7);
  assert.equal(logs.logs.length, 0);
});

test('an ordinary tab is still loggable', async () => {
  const ctx = loadBackground();
  const result = await ctx.command('startLogging', {}, 7);
  assert.equal(result.success, true);
});

test('a private tab is refused when we cannot tell whether it is private', async () => {
  // isPrivateTab returned false on any exception, so a check that could not
  // answer was the reason a private window's traffic reached the buffer.
  const ctx = loadBackground();
  ctx.context.browser.tabs.get = async () => { throw new Error('no such tab'); };

  const result = await ctx.command('startLogging', {}, 7);

  assert.equal(result.success, false,
    'a privacy gate that cannot answer must refuse');
});

test('a background tab cannot be screenshotted without focusing it', async () => {
  // captureVisibleTab photographs the window's ACTIVE tab, so with
  // allow_focus false this returned the active tab's image labelled with the
  // requested tab's id, url and title, and wasFocused: true - a screenshot of
  // the user's open mail, filed as a screenshot of some other page.
  const ctx = loadBackground();
  const updates = [];
  ctx.context.browser.tabs.update = async (id, opts) => { updates.push(id); };

  const result = await ctx.command('screenshot',
                                   { allowFocus: false }, 9);

  assert.equal(result.success, false,
    'a wrong image presented as the right one is worse than an error');
  assert.match(result.error, /focus/i);
  assert.deepEqual(updates, [], 'allow_focus: false must not focus anything');
});

test('allow_focus as the string "false" is honoured, not ignored', async () => {
  const ctx = loadBackground();
  const result = await ctx.command('screenshot',
                                   { allowFocus: 'false' }, 9);
  assert.equal(result.success, false);
});

test('a bulk screenshot sweep skips private windows', async () => {
  const ctx = loadBackground();
  ctx.context.browser.tabs.query = async () => ([
    { id: 1, url: 'http://ordinary.test/', title: 'a', windowId: 1, active: true },
    { id: 2, url: 'http://secret.test/', title: 'b', windowId: 2, incognito: true }
  ]);

  const result = await ctx.command('screenshotAllTabs', {}, undefined);

  const urls = (result.screenshots || result.tabs || []).map(s => s.url);
  assert.ok(!urls.includes('http://secret.test/'),
            `a private window was captured: ${JSON.stringify(urls)}`);
});

test('a single screenshot says when it came from a private window', async () => {
  // The image still goes to the agent, but the server needs to know not to
  // write it to disk for a week.
  const ctx = loadBackground();
  ctx.markPrivate(7);   // the harness's active tab

  const result = await ctx.command('screenshot', {}, undefined);

  assert.equal(result.privateWindow, true);
});

test('a string tab id reaches the same logging state as the number', async () => {
  // The per-tab logging state is kept in Maps keyed by the tabId Firefox
  // reports, which is a number. Starting logging with "7" would have written
  // to a string key, so the capture listeners - which look up the number -
  // would never have found the session, and getNetworkLogs would have
  // reported nothing while claiming logging was on.
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, '7');

  fireRequest(ctx.webRequest, { tabId: 7 });

  const result = await ctx.command('getNetworkLogs', {}, 7);
  assert.equal(result.logs.length, 1,
    'the session started as "7" must be the session tab 7 logs into');
});

test('a tab argument of 0 is not mistaken for "no tab"', async () => {
  // `tabId ? await browser.tabs.get(tabId) : activeTab` was falsy for tab id
  // 0 at thirteen call sites, so a request naming that tab silently acted on
  // whichever tab happened to be in front.
  const ctx = loadBackground();
  const asked = [];
  ctx.context.browser.tabs.get = async (id) => {
    asked.push(id);
    return { id, windowId: 1, url: 'http://zero.test/', title: 'zero' };
  };

  await ctx.command('getText', {}, 0);

  assert.ok(asked.includes(0), `tab 0 was never asked for: ${asked}`);
});

test('a tab argument given as a string still names that tab', async () => {
  const ctx = loadBackground();
  const asked = [];
  ctx.context.browser.tabs.get = async (id) => {
    asked.push(id);
    return { id, windowId: 1, url: 'http://seven.test/', title: 's' };
  };

  await ctx.command('getText', {}, '7');

  assert.ok(asked.includes(7),
            `a JSON client's "7" was passed through unconverted: ${asked}`);
});

test('an invalid url_pattern says which argument was wrong', async () => {
  const ctx = loadBackground();
  const result = await ctx.command('findTabs', { urlPattern: 'what(' },
                                   undefined);
  assert.equal(result.success, false);
  assert.match(result.error, /url_pattern/,
    'the regex parser message was returned as the whole error');
});

test('a non-object runtime message does not throw inside the listener', () => {
  const ctx = loadBackground();
  assert.doesNotThrow(() => ctx.deliver(null));
  assert.doesNotThrow(() => ctx.deliver('hello'));
});

test('a request body Firefox could not read is not logged as no body', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { method: 'POST',
                            requestBody: { error: 'Request body too large' } });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.match(String(entry.requestBody), /not available/i,
    'an unreadable body read as a POST with no body at all');
});

test('a file upload names the file instead of logging an empty body', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, {
    method: 'POST',
    requestBody: { raw: [{ file: '/home/albert/tax-return.pdf' }] }
  });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  assert.match(String(entry.requestBody), /tax-return\.pdf/);
});

test('the screenshot menu item posts an action the native host handles', async () => {
  // It posted "screenshotTaken", for which the host has no handler, so the
  // message fell through to the /browser/command endpoint and the menu item
  // did nothing at all. The host's only screenshot handler is
  // "saveScreenshot" - see handle_local_command in
  // native-host/claudecodebrowser_host.py.
  const ctx = loadBackground();

  await ctx.clickMenuItem('claude-screenshot');

  const posted = ctx.nativeMessages.filter(m => m && m.action);
  assert.equal(posted.length, 1, 'the menu item must say something');
  assert.equal(posted[0].action, 'saveScreenshot',
    `the host has no handler for "${posted[0].action}"`);
  assert.ok(posted[0].data, 'it must carry the image');
});

test('the screenshot menu item declines a private window', async () => {
  // The native host writes this file itself, so the server's refusal to
  // persist a private-window screenshot does not cover this path.
  const ctx = loadBackground();
  ctx.markPrivate(7);

  await ctx.clickMenuItem('claude-screenshot');

  assert.deepEqual(ctx.nativeMessages.filter(m => m && m.action), [],
    'a private window must not be written to disk');
});

test('a click that navigates reports the page it landed on', async () => {
  // The server's safety guard tracks the current page from tool results, and
  // a click result carried no url - so after a click that navigated, the
  // blocklist, the allowlist and protected-domain confirmation were all
  // still judging the previous page.
  const ctx = loadBackground();
  let url = 'http://before.test/';
  ctx.context.browser.tabs.get = async (id) => ({ id, url, title: 't' });
  ctx.context.browser.tabs.sendMessage = async () => {
    url = 'https://chase.com/transfer';   // the click navigated
    return { clicked: true };
  };

  const result = await ctx.command('click', { selector: '#go' }, 7);

  assert.equal(result.url, 'https://chase.com/transfer');
});

test('a result that reports its own url keeps it', async () => {
  const ctx = loadBackground({
    contentScriptReply: { url: 'http://from-the-page.test/' }
  });
  ctx.context.browser.tabs.get = async (id) => ({ id, url: 'http://tab.test/' });

  const result = await ctx.command('getPageInfo', {}, 7);

  assert.equal(result.url, 'http://from-the-page.test/');
});

test('a content script that answers nothing is not reported as success', async () => {
  // A receiver that exists but returns neither true nor a Promise resolves
  // the sender's promise with undefined, and `{success: true, ...undefined}`
  // claimed the work was done: getText returned {success:true} with no text
  // and click reported a click nobody made.
  const ctx = loadBackground({ contentScriptReply: SILENT });

  for (const action of ['getText', 'click', 'type', 'getElements']) {
    const result = await ctx.command(action, { selector: '#x' }, 7);
    assert.equal(result.success, false,
      `${action} claimed success for work that never happened`);
    assert.match(result.error, /did not answer/i);
  }
});

test('wait_for_load: "false" returns at once instead of blocking', async () => {
  // `!== false` was true for the string, so declining to wait made
  // browser_refresh block for the full 30-second timeout.
  const ctx = loadBackground();
  const result = await ctx.command('refresh', { waitForLoad: 'false' }, 7);
  assert.equal(result.success, true);
  assert.equal(result.refreshed, true);
});

test('refresh reports the bypass_cache it actually used', async () => {
  // The reload took `|| false` while the result reported
  // parseFlag(..., true), so the reported field was a lie.
  const ctx = loadBackground();
  const used = [];
  ctx.context.browser.tabs.reload = async (id, opts) => { used.push(opts); };

  const result = await ctx.command('refresh', { waitForLoad: 'false' }, 7);

  assert.equal(used[0].bypassCache, false,
               'the schema default for browser_refresh is false');
  assert.equal(result.bypassCache, used[0].bypassCache,
               'the reported value must be the one that was used');
});

test('duplicate response headers are both represented', async () => {
  // out[header.name] = ... collapsed them, so two Set-Cookie headers became
  // one and the count was lost. Real responses almost always carry several.
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);

  fireRequest(webRequest, {
    responseHeaders: [
      { name: 'content-type', value: 'application/json' },
      { name: 'x-dup', value: 'one' },
      { name: 'x-dup', value: 'two' }
    ]
  });

  const entry = (await command('getNetworkLogs', {}, 7)).logs[0];
  const dup = String(entry.responseHeaders['x-dup']);
  assert.ok(dup.includes('one') && dup.includes('two'),
            `one of the duplicate headers was lost: ${dup}`);
});

test('get_network_logs finds a status given as a string', async () => {
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, { statusCode: 404 });

  const result = await command('getNetworkLogs', { status: '404' }, 7);

  assert.equal(result.logs.length, 1,
    'a strict comparison against the numeric status returned nothing');
});

test('get_tabs defaults to the current window, by which tabs come back', async () => {
  // This privacy default was only ever asserted through the `scope` string
  // the result reports, so swapping {currentWindow: true} for {} kept the
  // suite green while handing over every window.
  const ctx = loadBackground();

  const scoped = await ctx.command('getTabs', {}, undefined);
  assert.deepEqual(scoped.tabs.map(t => t.id), [7],
    'the default must not reach into another window');

  const wide = await ctx.command('getTabs', { currentWindowOnly: false },
                                 undefined);
  assert.deepEqual(wide.tabs.map(t => t.id).sort(), [7, 9],
    'opting in explicitly must widen the view');
});

test('a log entry hands the caller only the fields it should', async () => {
  // Every other assertion here is a scalar, because deepEqual cannot compare
  // an object built inside the vm realm. Re-homing it through JSON makes a
  // whole-shape assertion possible, so the next internal field added does not
  // reach the agent unnoticed - filterAttached already did once.
  const { command, webRequest } = loadBackground();
  await command('startLogging', {}, 7);
  fireRequest(webRequest, {
    method: 'POST',
    requestHeaders: [{ name: 'Authorization', value: 'Bearer x' }]
  });

  const entry = JSON.parse(JSON.stringify(
    (await command('getNetworkLogs', {}, 7)).logs[0]));

  const allowed = new Set([
    'id', 'requestId', 'tabId', 'url', 'method', 'type', 'timestamp',
    'status', 'statusLine', 'statusCode', 'statusText', 'startTime',
    'fromCache', 'requestHeaders',
    'responseHeaders', 'requestBody', 'responseBody', 'responseBodyTruncated',
    'responseBodyBytes', 'responseCharset', 'responseEncoding', 'charsetNote',
    'responseType', 'contentType', 'error', 'duration', 'completedAt',
    'startedAt', 'redirectedTo', 'redirectedFrom', 'ip', 'size'
  ]);
  const unexpected = Object.keys(entry).filter(k => !allowed.has(k));
  assert.deepEqual(unexpected, [],
    `internal bookkeeping reached the caller: ${unexpected.join(', ')}`);
  assert.equal(entry.requestHeaders.Authorization, '***',
    'the credential header must be redacted');
});

test('find_tabs requires a filter rather than dumping every tab', async () => {
  const ctx = loadBackground();

  const result = await ctx.command('findTabs', {}, undefined);

  assert.equal(result.success, false,
    'with no filter this returned the entire browsing surface');
  assert.match(result.error, /needs a filter/i);
});

test('a filter that narrows nothing does not satisfy find_tabs', async () => {
  // active: false and audible: false pass an `!== undefined` check and match
  // essentially every tab, so one boolean defeated the filter requirement
  // and handed over the whole browsing surface anyway.
  for (const filter of [{ active: false }, { audible: false },
                        { active: 'false' }, { url: '' }]) {
    const ctx = loadBackground();
    const result = await ctx.command('findTabs', filter, undefined);
    assert.equal(result.success, false,
      `${JSON.stringify(filter)} is not a filter that narrows anything`);
  }
});

test('find_tabs honours a string limit and clamps an outsized one', async () => {
  // Number.isInteger refused the string "1" a JSON client sends and fell
  // back to 50, so a caller asking for one tab got all of them; and nothing
  // stopped limit: 100000, which removed the cap altogether.
  const many = Array.from({ length: 120 }, (_, i) => ({
    id: i, url: `http://stub.test/${i}`, title: `t${i}`, active: false,
    windowId: 1, status: 'complete'
  }));

  let ctx = loadBackground();
  ctx.context.browser.tabs.query = async () => many;
  let result = await ctx.command('findTabs', { urlPattern: 'stub', limit: '1' },
                                 undefined);
  assert.equal(result.tabs.length, 1, 'a string limit must be honoured');

  ctx = loadBackground();
  ctx.context.browser.tabs.query = async () => many;
  result = await ctx.command('findTabs', { urlPattern: 'stub', limit: 100000 },
                             undefined);
  assert.equal(result.tabs.length, 50, 'the cap must survive a huge limit');
  assert.equal(result.truncated, true);
});

test('find_tabs caps its results and says when it did', async () => {
  const ctx = loadBackground();
  const many = Array.from({ length: 120 }, (_, i) => ({
    id: i, url: `http://stub.test/${i}`, title: `t${i}`, active: false,
    windowId: 1, status: 'complete'
  }));
  ctx.context.browser.tabs.query = async () => many;

  const result = await ctx.command('findTabs', { urlPattern: 'stub' }, undefined);

  assert.equal(result.tabs.length, 50, 'default cap, as browser_get_tabs has');
  assert.equal(result.totalMatched, 120);
  assert.equal(result.truncated, true);
});

// --------------------------------------------------------------------------
// Captured bodies are bytes, and their encoding matters

test('a declared charset is honoured rather than assumed to be utf-8', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, {
    responseHeaders: [{ name: 'content-type',
                        value: 'text/html; charset=windows-1252' }],
    complete: false
  });

  // 0x93/0x94 are curly quotes in windows-1252 and invalid in utf-8.
  ctx.filters[0].ondata({ data: new Uint8Array([0x93, 0x68, 0x69, 0x94]) });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.equal(entry.responseCharset, 'windows-1252');
  assert.ok(!entry.responseBody.includes('\uFFFD'),
    'decoding with the declared charset must not produce replacement chars');
  assert.ok(entry.responseBody.includes('hi'));
});

test('a gzip/br response is still captured, because Firefox decompresses it', async () => {
  // Refusing on the content-encoding header would drop bodies on essentially
  // every real site: the header is present even though Firefox hands the
  // filter decompressed bytes.
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, {
    responseHeaders: [
      { name: 'content-type', value: 'application/json' },
      { name: 'content-encoding', value: 'br' }
    ],
    complete: false
  });
  assert.equal(ctx.filters.length, 1, 'a filter must still be attached');

  ctx.filters[0].ondata({ data: new TextEncoder().encode('{"ok":true}') });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.equal(entry.responseBody, '{"ok":true}',
    'the body decoded fine; the header alone must not refuse it');
  assert.equal(entry.responseEncoding, 'br',
    'the encoding is still recorded for diagnostics');
});

test('bytes that do not decode as text are refused, not logged as noise', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'application/json' }],
    complete: false
  });

  // Actual compressed/binary bytes: mostly control and invalid sequences.
  const binary = new Uint8Array(400);
  for (let i = 0; i < binary.length; i++) binary[i] = (i * 7) % 32;
  ctx.filters[0].ondata({ data: binary });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.match(entry.responseBody, /did not decode as text/,
    'judge the decoded result, not the header');
});

test('ordinary text with a stray control byte is still captured', async () => {
  // The heuristic must not reject real content over one odd character.
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, {
    responseHeaders: [{ name: 'content-type', value: 'text/plain' }],
    complete: false
  });
  ctx.filters[0].ondata({
    data: new TextEncoder().encode('a'.repeat(300) + '\u0001' + 'b'.repeat(300))
  });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.ok(entry.responseBody.includes('aaa'),
    'one control byte in 600 is not binary');
});

test('an unsupported charset falls back and says so', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, {
    responseHeaders: [{ name: 'content-type',
                        value: 'text/plain; charset=x-made-up-encoding' }],
    complete: false
  });
  ctx.filters[0].ondata({ data: new TextEncoder().encode('plain text') });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.match(entry.charsetNote, /unsupported charset/);
  assert.ok(entry.responseBody.includes('plain text'),
    'the body is still delivered, just flagged');
});

test('a truncated body is flagged with its real length', async () => {
  const ctx = loadBackground();
  await ctx.command('startLogging', {}, 7);

  fireRequest(ctx.webRequest, { complete: false });
  const big = 'x'.repeat(6000);
  ctx.filters[0].ondata({ data: new TextEncoder().encode(big) });
  ctx.filters[0].onstop();
  ctx.webRequest.onCompleted.fire({ requestId: '1', tabId: 7, statusCode: 200 });

  const entry = (await ctx.command('getNetworkLogs', {}, 7)).logs[0];
  assert.equal(entry.responseBody.length, 5000);
  assert.equal(entry.responseBodyTruncated, true,
    'the cap applied silently, so a short body and a cut one looked alike');
  assert.equal(entry.responseBodyBytes, 6000);
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
