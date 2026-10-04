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
    style: { cssText: '' },
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
    setAttribute(name, value) { this._attributes[name] = value; },
    attachShadow() {
      this._shadow = { children: [], appendChild(c) { this.children.push(c); } };
      return this._shadow;
    },
    appendChild(child) { this.children.push(child); return child; },
    remove() { this._removed = true; },
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    /** Deliver an event the way page script would (isTrusted false) or the
     *  way a real user click arrives (isTrusted true). */
    emit(type, isTrusted) {
      (this._listeners[type] || []).forEach(fn => fn({ type, isTrusted }));
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

  const createdElements = [];
  const document = {
    title: 'stub page',
    body,
    forms: registry.__forms__ || [],
    documentElement: { scrollHeight: 1000, scrollWidth: 800, lang: 'en' },
    activeElement: null,
    querySelector: resolveOne,
    querySelectorAll: (sel) => {
      if (all.has(sel)) return all.get(sel);
      // getPageInfo builds one long interactive-element selector; route it to
      // an explicit registry key rather than trying to parse CSS here.
      if (sel.includes('a[href]')) return all.get('__interactive__') || [];
      if (sel.startsWith('h1')) return all.get('__headings__') || [];
      return [];
    },
    getElementById: (id) => resolveOne(`#${id}`),
    evaluate: () => ({ singleNodeValue: null }),
    createElement: (tag) => {
      const el = makeElement(tag);
      createdElements.push(el);
      return el;
    },
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

  return { send, window, consoleStub, XMLHttpRequestStub, createdElements,
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
// The leak that survived the first fix: get_page_info masked passwords in its
// forms[] branch but not in interactiveElements[], and the test that
// "covered" it was a grep for the forms[] string.

test('browser_get_page_info masks password values in interactiveElements', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SuperSecret123!' });
  const user = makeElement('input', { id: 'user', type: 'text', value: 'albert' });
  const { send } = loadContentScript({ __interactive__: [pw, user] });

  const result = await send({ action: 'getPageInfo' });

  const values = result.interactiveElements.map(e => e.value);
  assert.ok(values.includes('albert'), 'ordinary values still come through');
  assert.ok(!values.includes('SuperSecret123!'), 'the password must not be returned');
  assert.ok(!JSON.stringify(result).includes('SuperSecret123!'),
    'the plaintext password must not appear anywhere in the response');
});

test('browser_get_page_info masks credential values in forms[] too', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SuperSecret123!' });
  const csrf = makeElement('input', { id: 'csrf', type: 'hidden', value: 'csrf-token-abc' });
  const form = { id: 'f', name: 'f', action: '/login', method: 'post',
                 elements: [pw, csrf] };
  const { send } = loadContentScript({ __forms__: [form] });

  const result = await send({ action: 'getPageInfo' });

  const serialized = JSON.stringify(result);
  assert.ok(!serialized.includes('SuperSecret123!'));
  assert.ok(!serialized.includes('csrf-token-abc'),
    'hidden inputs carry CSRF and session tokens; they are not needed in clear');
});

test('hidden input values are masked by get_value as well', async () => {
  const csrf = makeElement('input', { id: 'csrf', type: 'hidden', value: 'csrf-token-abc' });
  const { send } = loadContentScript({ '#csrf': csrf });

  const result = await send({ action: 'getValue', selector: '#csrf' });

  assert.equal(result.value, '***');
  assert.equal(result.masked, true);
});

// --------------------------------------------------------------------------
// The credential predicate itself

test('isPasswordField accepts the spec-legal autocomplete forms', async () => {
  const cases = [
    ['Current-Password', 'mixed case'],
    ['current-password ', 'trailing whitespace'],
    ['section-login current-password', 'token list'],
    ['new-password', 'signup form'],
    ['one-time-code', '2FA code'],
    ['cc-number', 'card number'],
    ['cc-csc', 'card security code']
  ];
  for (const [value, why] of cases) {
    const el = makeElement('input', {
      id: 'f', type: 'text', value: 'SECRET', attributes: { autocomplete: value }
    });
    const { send } = loadContentScript({ '#f': el });
    const result = await send({ action: 'getValue', selector: '#f' });
    assert.equal(result.value, '***', `autocomplete="${value}" (${why}) must be guarded`);
  }
});

test('the allow_password override is strictly boolean true', async () => {
  // A fail-closed guard must not be unlocked by any truthy value.
  for (const sneaky of ['false', 'true', 1, 0.1, {}, [], 'yes']) {
    const pw = makeElement('input', { id: 'pw', type: 'password', value: 'SECRET' });
    const { send } = loadContentScript({ '#pw': pw });
    const result = await send({
      action: 'getValue', selector: '#pw', allow_password: sneaky
    });
    assert.equal(result.value, '***',
      `allow_password: ${JSON.stringify(sneaky)} must not unlock the guard`);
  }
});

test('browser_get_attribute cannot read a password out of the value attribute', async () => {
  const pw = makeElement('input', {
    id: 'pw', type: 'password', value: 'SECRET', attributes: { value: 'SECRET' }
  });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'getAttribute', selector: '#pw', attribute: 'value' });

  assert.equal(result.value, '***');
  assert.equal(result.masked, true);
});

test('browser_set_value refuses a password field', async () => {
  const pw = makeElement('input', { id: 'pw', type: 'password', value: '' });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'setValue', selector: '#pw', value: 'nope' });

  assert.equal(result.success, false);
  assert.match(result.error, /password field/i);
  assert.equal(pw.value, '');
});

// --------------------------------------------------------------------------
// The approval prompt must require a human

test('a page-dispatched click cannot approve a protected action', async () => {
  const ctx = loadContentScript({});
  const pending = ctx.send({ action: 'requestApproval', message: 'do a thing',
                             timeout: 400 });

  // The banner lives in a closed shadow root; find the Approve button among
  // the elements the content script created and click it as page script would.
  await new Promise(resolve => setTimeout(resolve, 20));
  const approve = ctx.createdElements.find(e => e.textContent === 'Approve');
  assert.ok(approve, 'the prompt should have rendered an Approve button');
  approve.emit('click', false);   // isTrusted: false, i.e. element.click()

  const result = await pending;
  assert.equal(result.approved, false,
    'an untrusted click must not count as human approval');
  assert.equal(result.timedOut, true, 'it should fall through to the timeout');
});

test('a real click does approve', async () => {
  const ctx = loadContentScript({});
  const pending = ctx.send({ action: 'requestApproval', message: 'do a thing',
                             timeout: 2000 });

  await new Promise(resolve => setTimeout(resolve, 20));
  const approve = ctx.createdElements.find(e => e.textContent === 'Approve');
  approve.emit('click', true);    // isTrusted: true, i.e. a person clicked

  const result = await pending;
  assert.equal(result.approved, true);
  assert.notEqual(result.timedOut, true);
});

test('the prompt is rendered in a closed shadow root', async () => {
  const ctx = loadContentScript({});
  ctx.send({ action: 'requestApproval', message: 'x', timeout: 200 });
  await new Promise(resolve => setTimeout(resolve, 20));

  const host = ctx.createdElements.find(e => e.id === '__ccb_approval_host');
  assert.ok(host, 'the prompt needs its own host element');
  assert.ok(host._shadow, 'the banner must live in a shadow root, not the page DOM');
  assert.match(host.getAttribute('style') || '', /!important/,
    'host styling must resist a page !important rule');
});

// --------------------------------------------------------------------------
// Click semantics

test('browser_click dispatches exactly one click', async () => {
  let clicks = 0;
  const button = makeElement('button', { id: 'go', textContent: 'Go' });
  button.dispatchEvent = (event) => {
    if (event.type === 'click') clicks++;
    return true;
  };
  button.click = () => { clicks++; };
  const { send } = loadContentScript({ '#go': button });

  await send({ action: 'click', selector: '#go' });

  assert.equal(clicks, 1, 'a second click would re-trigger non-idempotent handlers');
});

test('a text selector containing a quote cannot graft on an XPath predicate', async () => {
  const ctx = loadContentScript({});
  let seen = null;
  // Capture what findElement asks XPath for.
  await ctx.send({ action: 'getText', selector: 'body' }).catch(() => {});
  const evil = 'Accept")]|//a[@id="transfer-all"][contains(text(),"';
  await ctx.send({ action: 'click', text: evil }).catch(() => {});
  // The expression is built with concat() so the needle stays a single literal.
  assert.ok(!SOURCE.includes('contains(text(), "${options.text}")'),
    'the raw interpolation must be gone');
  assert.ok(SOURCE.includes('xpathLiteral('),
    'the text needle must be quoted through xpathLiteral');
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
