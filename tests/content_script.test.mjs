/**
 * Content-script tests. No dependencies: a minimal DOM/browser stub is built
 * here and extension/content.js is evaluated against it, then driven through
 * the same runtime.onMessage entry point the extension uses.
 *
 * Run: node tests/content_script.test.mjs
 */

import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(here, '..', 'extension', 'content.js'), 'utf8');

function makeElement(tag, props = {}) {
  const el = {
    tagName: tag.toUpperCase(),
    id: props.id || '',
    name: props.name || '',
    type: props.type,
    value: props.value,
    className: props.className || '',
    classList: props.className ? props.className.split(/\s+/) : [],
    textContent: props.textContent || '',
    isContentEditable: false,
    disabled: false,
    checked: false,
    parentElement: null,
    children: [],
    options: [],
    selectedIndex: -1,
    _attributes: props.attributes || {},
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attributes, name)
        ? this._attributes[name]
        : null;
    },
    getBoundingClientRect: () => ({
      left: 0, top: 0, width: 10, height: 10,
      x: 0, y: 0, right: 10, bottom: 10
    }),
    scrollIntoView() {},
    focus() {},
    click() {},
    dispatchEvent() { return true; },
    closest() { return null; }
  };
  return el;
}

/** Build a fresh sandbox with the given selector -> element(s) registry. */
function loadContentScript(registry) {
  const single = new Map(Object.entries(registry));
  const all = new Map(
    Object.entries(registry).map(([sel, el]) => [sel, Array.isArray(el) ? el : [el]])
  );
  const resolveOne = (sel) => {
    const hit = single.get(sel);
    if (!hit) return null;
    return Array.isArray(hit) ? hit[0] : hit;
  };

  const body = makeElement('body');
  const consoleStub = {
    log() {}, warn() {}, error() {}, info() {}, debug() {}
  };
  const originalConsoleLog = consoleStub.log;

  const document = {
    title: 'stub page',
    body,
    documentElement: { scrollHeight: 1000, scrollWidth: 800, lang: 'en' },
    forms: [],
    activeElement: null,
    querySelector: resolveOne,
    querySelectorAll: (sel) => all.get(sel) || [],
    getElementById: (id) => resolveOne(`#${id}`),
    evaluate: () => ({ singleNodeValue: null }),
    createElement: (tag) => makeElement(tag),
    elementFromPoint: () => null,
    execCommand: () => true,
    addEventListener() {},
    getElementsByTagName: () => []
  };

  const makeResponse = () => ({
    status: 200,
    statusText: 'OK',
    headers: {
      get: (name) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
      entries: () => [['content-type', 'application/json']]
    },
    clone: () => ({ json: async () => ({ ok: true }), text: async () => '{"ok":true}' })
  });
  const fetchStub = async () => makeResponse();
  function XMLHttpRequestStub() {}
  XMLHttpRequestStub.prototype.open = function open() {};
  XMLHttpRequestStub.prototype.send = function send() {};
  const originalXhrOpen = XMLHttpRequestStub.prototype.open;

  const window = {
    fetch: fetchStub,
    location: { href: 'http://stub.test/page' },
    innerWidth: 1280,
    innerHeight: 800,
    scrollX: 0,
    scrollY: 0,
    getComputedStyle: () => ({
      display: 'block',
      visibility: 'visible',
      opacity: '1',
      getPropertyValue: () => ''
    }),
    scrollTo() {}, scrollBy() {}
  };

  let messageListener = null;
  const browser = {
    runtime: {
      onMessage: { addListener: (fn) => { messageListener = fn; } },
      getURL: (p) => `moz-extension://stub/${p}`
    }
  };

  const sandbox = {
    window,
    document,
    browser,
    console: consoleStub,
    XMLHttpRequest: XMLHttpRequestStub,
    MutationObserver: class { observe() {} disconnect() {} },
    KeyboardEvent: class { constructor(type, init) { Object.assign(this, init, { type }); } },
    MouseEvent: class { constructor(type, init) { Object.assign(this, init, { type }); } },
    Event: class { constructor(type, init) { Object.assign(this, init, { type }); } },
    Node: { ELEMENT_NODE: 1 },
    XPathResult: { FIRST_ORDERED_NODE_TYPE: 9 },
    setTimeout, clearTimeout, setInterval, clearInterval
  };
  sandbox.globalThis = sandbox;

  const context = createContext(sandbox);
  runInContext(SOURCE, context, { filename: 'content.js' });

  assert.ok(messageListener, 'content.js must register a runtime.onMessage listener');

  const send = (message) => new Promise((resolve, reject) => {
    const ret = messageListener(message, null, resolve);
    assert.equal(ret, true, 'listener must keep the channel open');
    setTimeout(() => reject(new Error(`no response for ${message.action}`)), 2000);
  });

  return { send, window, consoleStub, XMLHttpRequestStub,
           pristine: {
             fetch: fetchStub,
             consoleLog: originalConsoleLog,
             xhrOpen: originalXhrOpen
           } };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --------------------------------------------------------------------------
// Credential guard: reading a password field must not return the plaintext.

test('browser_get_value masks a password field', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SuperSecret123!' });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'getValue', selector: '#pw' });

  assert.equal(result.value, '***');
  assert.equal(result.masked, true);
  assert.ok(!JSON.stringify(result).includes('SuperSecret123!'),
    'the plaintext password must not appear anywhere in the response');
});

test('browser_get_value masks autocomplete="current-password" fields', async () => {
  const pw = makeElement('input', {
    id: 'pw', type: 'text', value: 'hunter2',
    attributes: { autocomplete: 'current-password' }
  });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'getValue', selector: '#pw' });

  assert.equal(result.value, '***');
  assert.equal(result.masked, true);
});

test('browser_get_value still returns ordinary input values', async () => {
  const email = makeElement('input', { id: 'email', type: 'email', value: 'a@b.test' });
  const { send } = loadContentScript({ '#email': email });

  const result = await send({ action: 'getValue', selector: '#email' });

  assert.equal(result.value, 'a@b.test');
  assert.notEqual(result.masked, true);
});

test('browser_get_elements masks password values but not other values', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SuperSecret123!' });
  const user = makeElement('input', { id: 'user', type: 'text', value: 'albert' });
  const { send } = loadContentScript({ input: [pw, user], '#pw': pw, '#user': user });

  const result = await send({ action: 'getElements', selector: 'input' });

  const [pwInfo, userInfo] = result.elements;
  assert.equal(pwInfo.value, '***');
  assert.equal(userInfo.value, 'albert');
  assert.ok(!JSON.stringify(result).includes('SuperSecret123!'),
    'the plaintext password must not appear anywhere in the response');
});

test('an explicit allow_password override returns the real value', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SuperSecret123!' });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'getValue', selector: '#pw', allow_password: true });

  assert.equal(result.value, 'SuperSecret123!');
});

test('typing into a password field is refused by default', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: '' });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'type', selector: '#pw', text: 'nope' });

  assert.equal(result.success, false);
  assert.match(result.error, /password field/i);
});

// The server's camelize_args() turns allow_password into allowPassword before
// dispatch, so allowPassword is the key that really arrives. Both are accepted.
test('either spelling of the allow_password override permits typing', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: '' });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({
    action: 'type', selector: '#pw', text: 'ok', instant: true, allow_password: true
  });

  assert.notEqual(result.success, false);
  assert.equal(pw.value, 'ok');

  const camel = makeElement('input', { id: 'pw2', type: 'password', value: '' });
  const ctx = loadContentScript({ '#pw2': camel });
  const camelResult = await ctx.send({
    action: 'type', selector: '#pw2', text: 'ok', instant: true, allowPassword: true
  });

  assert.notEqual(camelResult.success, false);
  assert.equal(camel.value, 'ok');
});

// --------------------------------------------------------------------------
// Interception is opt-in: no hooks on pages until logging is started.

test('the page keeps its own fetch, XHR and console at load', async () => {
  const { window, consoleStub, XMLHttpRequestStub, pristine } = loadContentScript({});

  assert.equal(window.fetch, pristine.fetch, 'window.fetch must not be replaced');
  assert.equal(consoleStub.log, pristine.consoleLog, 'console.log must not be replaced');
  assert.equal(XMLHttpRequestStub.prototype.open, pristine.xhrOpen,
    'XHR.prototype.open must not be replaced');
});

test('network globals are never touched, even while logging', async () => {
  // Network capture moved to background.js/webRequest. The content script has
  // no business reaching into the page's networking any more.
  const ctx = loadContentScript({});

  await ctx.send({ action: 'startLogging' });

  assert.equal(ctx.window.fetch, ctx.pristine.fetch,
    'startLogging must not hook fetch');
  assert.equal(ctx.XMLHttpRequestStub.prototype.open, ctx.pristine.xhrOpen,
    'startLogging must not hook XHR');

  assert.ok(!SOURCE.includes('window.fetch ='),
    'content.js must not assign to window.fetch');
  assert.ok(!SOURCE.includes('XMLHttpRequest.prototype.open ='),
    'content.js must not assign to XMLHttpRequest.prototype.open');
});

test('startLogging hooks console and stopLogging restores it', async () => {
  const ctx = loadContentScript({});

  const started = await ctx.send({ action: 'startLogging' });
  assert.equal(started.success, true);
  assert.notEqual(ctx.consoleStub.log, ctx.pristine.consoleLog,
    'startLogging must hook console');

  const stopped = await ctx.send({ action: 'stopLogging' });
  assert.equal(stopped.success, true);
  assert.equal(ctx.consoleStub.log, ctx.pristine.consoleLog,
    'stopLogging must restore console');
});

test('repeated start/stop cycles leave console as it was', async () => {
  const ctx = loadContentScript({});

  for (let i = 0; i < 3; i++) {
    await ctx.send({ action: 'startLogging' });
    await ctx.send({ action: 'stopLogging' });
  }

  assert.equal(ctx.consoleStub.log, ctx.pristine.consoleLog);
});

test('console output is captured only while logging is on', async () => {
  const ctx = loadContentScript({});

  ctx.consoleStub.log('before');
  await ctx.send({ action: 'startLogging' });
  ctx.consoleStub.log('during');
  await ctx.send({ action: 'stopLogging' });
  ctx.consoleStub.log('after');

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  // Array.from re-homes the cross-realm array so deepEqual can compare it.
  const messages = Array.from(logs.logs, l => l.message);
  assert.deepEqual(messages, ['during']);
});

test('the content script no longer answers waitForNetworkIdle', async () => {
  // It used to wrap the page's fetch and XHR to count in-flight requests;
  // background.js counts them at the network layer instead.
  const ctx = loadContentScript({});

  const result = await ctx.send({ action: 'waitForNetworkIdle' });

  assert.equal(result.success, false);
  assert.match(result.error, /Unknown action/);
});

test('the content script no longer answers getNetworkLogs', async () => {
  // background.js owns network logs now; a stale route here would shadow it.
  const ctx = loadContentScript({});

  const result = await ctx.send({ action: 'getNetworkLogs' });

  assert.equal(result.success, false);
  assert.match(result.error, /Unknown action/);
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
