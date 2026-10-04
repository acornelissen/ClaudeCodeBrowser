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
    /** innerText is the visible text; textContent includes display:none
     *  subtrees. They are the same on an element with nothing hidden in it,
     *  so that is the default, and a fixture with hidden content sets
     *  innerText itself (often to '') to make the difference show. */
    innerText: props.innerText !== undefined
      ? props.innerText
      : (props.textContent || ''),
    style: { cssText: '' },
    isContentEditable: false,
    disabled: false,
    checked: props.checked === true,
    parentElement: null,
    children: [],
    options: [],
    selectedIndex: -1,
    /** Computed CSS, keyed by real CSS property name ('background-color'). */
    _styles: props.styles || {},
    _attributes: props.attributes || {},
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attributes, name)
        ? this._attributes[name]
        : null;
    },
    setAttribute(name, value) { this._attributes[name] = value; },
    /** Records the init it was given: a prompt in an open root is one the
     *  page can read and whose buttons it can find, so the mode is part of
     *  what the tests have to check. */
    attachShadow(init) {
      this._shadowInit = init;
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
    /** Real pages are full of zero-size elements - hidden inputs, offscreen
     *  [tabindex] holders - and they report an all-zero rect, so a fixture
     *  can ask for one. */
    getBoundingClientRect: () => (props.rect || {
      left: 0, top: 0, width: 10, height: 10,
      x: 0, y: 0, right: 10, bottom: 10
    }),
    /** Element-scoped queries resolve against the same registry the document
     *  uses, so a test does not have to hand-stub this - a hand-stubbed
     *  lookup only ever proves what the stub was told to return. The registry
     *  is injected by loadContentScript. */
    querySelector(sel) { return (this._registry?.one(sel)) || null; },
    querySelectorAll(sel) { return (this._registry?.all(sel)) || []; },
    scrollIntoView() {},
    focus() {},
    click() {},
    dispatchEvent() { return true; },
    closest() { return null; }
  };

  if (el.tagName === 'SELECT') {
    el.options = (props.options || []).map(o => (
      typeof o === 'string' ? { value: o, text: o }
                            : { value: o.value, text: o.text ?? o.value }
    ));
    // A real <select> keeps no value of its own: .value is the selected
    // option's value, assigning a value no option carries deselects
    // everything and .value reads back as ''. Code that assigns and then
    // reports .value is only testable against that behaviour.
    let selectedIndex = props.selectedIndex !== undefined
      ? props.selectedIndex
      : (el.options.length ? 0 : -1);
    Object.defineProperty(el, 'selectedIndex', {
      get: () => selectedIndex,
      set: (i) => {
        const n = Number(i);
        selectedIndex = (Number.isInteger(n) && n >= 0 && n < el.options.length) ? n : -1;
      }
    });
    Object.defineProperty(el, 'value', {
      get: () => (el.options[selectedIndex] ? el.options[selectedIndex].value : ''),
      set: (v) => {
        selectedIndex = el.options.findIndex(o => o.value === String(v));
      }
    });
  }

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

  // A comma-separated selector is a union of its parts, as in a real
  // document. Without this the registry keyed on the exact string, so adding
  // one selector to a production query silently returned nothing and the test
  // reported a leak that was only a harness mismatch - or, worse, would have
  // reported a pass for a query that matched nothing.
  const resolveAll = (sel) => {
    if (all.has(sel)) return all.get(sel);
    const parts = sel.split(',').map(s => s.trim()).filter(Boolean);
    if (parts.length < 2) return [];
    const seen = new Set();
    const out = [];
    for (const part of parts) {
      for (const el of (all.get(part) || [])) {
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
    }
    return out;
  };
  const lookups = { one: resolveOne, all: resolveAll };
  const body = makeElement('body');
  body._registry = lookups;
  const consoleStub = {
    log() {}, warn() {}, error() {}, info() {}, debug() {}
  };
  const originalConsoleLog = consoleStub.log;

  const createdElements = [];
  // Every element the test registered can answer a scoped query too.
  for (const entry of all.values()) {
    for (const el of entry) {
      if (el && typeof el === 'object') el._registry = lookups;
    }
  }

  const document = {
    title: 'stub page',
    body,
    forms: registry.__forms__ || [],
    documentElement: { scrollHeight: 1000, scrollWidth: 800, lang: 'en' },
    // A real document always has something focused: <body> when nothing
    // else is. null made focus_first: false look like "no element".
    activeElement: body,
    querySelector: resolveOne,
    querySelectorAll: (sel) => {
      const union = resolveAll(sel);
      if (union.length) return union;
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
    /** getPropertyValue answers for CSS property names ('background-color')
     *  and returns '' for any name it does not know. A stub that returned ''
     *  for everything could not show that content.js was asking for the
     *  camelCase alias, so this one only answers for names it was given. */
    getComputedStyle: (el) => {
      const computed = {
        display: 'block',
        visibility: 'visible',
        opacity: '1',
        ...((el && el._styles) || {})
      };
      return {
        ...computed,
        getPropertyValue: (name) => (
          Object.prototype.hasOwnProperty.call(computed, name) ? computed[name] : ''
        )
      };
    },
    scrollTo() {}, scrollBy() {},
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    /** Deliver a page error the way Firefox delivers it to a content script. */
    emit(type, event) {
      (this._listeners[type] || []).forEach(fn => fn(event));
    },
    listenerCount(type) { return (this._listeners[type] || []).length; }
  };

  let messageListener = null;
  const browser = {
    runtime: {
      onMessage: { addListener: (fn) => { messageListener = fn; } },
      getURL: (p) => `moz-extension://stub/${p}`
    }
  };

  const sandbox = {
    __observers: [],
    window,
    document,
    browser,
    console: consoleStub,
    XMLHttpRequest: XMLHttpRequestStub,
    MutationObserver: class {
      constructor(cb) { this.cb = cb; this.disconnected = false;
                        sandbox.__observers.push(this); }
      observe() { this.observing = true; }
      disconnect() { this.disconnected = true; this.observing = false; }
    },
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

  return { send, window, document, consoleStub, XMLHttpRequestStub,
           createdElements,
           observers: sandbox.__observers,
           /** Evaluate source inside the sandbox. An Error built out here is
            *  not `instanceof Error` in there, so a cross-realm object would
            *  quietly test the wrong branch of content.js. */
           evalInSandbox: (source) => runInContext(source, context),
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
  assert.equal(host._shadowInit?.mode, 'closed',
    'an open root lets page script read the prompt and click its buttons');
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
// What console capture can honestly claim. Verified live: a page's own
// console.log produced zero entries while execute_script's produced one, and
// the result said interceptionAvailable: true throughout - inviting an agent
// to read an empty array as "the page logged nothing".

test('page errors and unhandled rejections are captured', async () => {
  const ctx = loadContentScript({});
  await ctx.send({ action: 'startLogging' });

  ctx.window.emit('error', {
    message: 'Uncaught TypeError: x is not a function',
    filename: 'http://stub.test/app.js', lineno: 12, colno: 3
  });
  // The Error has to be built inside the sandbox: one made out here is not
  // `instanceof Error` across the vm realm, so content.js would fall through
  // to its String(reason) path and the name/message branch would never run.
  // toString is overridden so the two branches produce different text and
  // the test can tell which one ran.
  ctx.window.emit('unhandledrejection', {
    reason: ctx.evalInSandbox(
      '(() => { const e = new TypeError("boom");'
      + ' e.toString = () => "via String()"; return e; })()')
  });

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  const messages = Array.from(logs.logs, l => l.message);
  assert.equal(logs.logs.length, 2);
  assert.ok(messages[0].includes('x is not a function'));
  assert.ok(messages[0].includes('app.js:12:3'), 'location should be included');
  assert.equal(messages[1], 'Unhandled promise rejection: TypeError: boom',
    'an Error reason is reported by name and message, not via String()');
  assert.ok(logs.logs.every(l => l.source === 'page'));
});

test('the result does not claim to capture the page console', async () => {
  const ctx = loadContentScript({});
  await ctx.send({ action: 'startLogging' });

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  assert.equal(logs.capturesPageConsole, false,
    'a content script cannot see the page console; saying otherwise invites '
    + 'an empty array to be read as "no errors"');
  assert.equal(logs.capturesPageErrors, true);
  assert.equal(logs.capturesExtensionConsole, true);
  assert.match(logs.note, /does not mean the page logged nothing/);
});

test('extension-side console output is tagged as such', async () => {
  const ctx = loadContentScript({});
  await ctx.send({ action: 'startLogging' });
  ctx.consoleStub.log('from an extension script');

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  assert.equal(logs.logs.length, 1);
  assert.equal(logs.logs[0].source, 'extension',
    'the two sources must be distinguishable in the result');
});

test('page error listeners are removed when logging stops', async () => {
  const ctx = loadContentScript({});
  assert.equal(ctx.window.listenerCount('error'), 0, 'nothing before logging');

  await ctx.send({ action: 'startLogging' });
  assert.equal(ctx.window.listenerCount('error'), 1);
  assert.equal(ctx.window.listenerCount('unhandledrejection'), 1);

  await ctx.send({ action: 'stopLogging' });
  assert.equal(ctx.window.listenerCount('error'), 0,
    'a page must not keep paying for a finished logging session');
  assert.equal(ctx.window.listenerCount('unhandledrejection'), 0);
});

test('page errors are not recorded while logging is off', async () => {
  const ctx = loadContentScript({});
  await ctx.send({ action: 'startLogging' });
  await ctx.send({ action: 'stopLogging' });
  ctx.window.emit('error', { message: 'after stop' });

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  assert.equal(logs.logs.length, 0);
});

test('an unprintable rejection reason does not break capture', async () => {
  const ctx = loadContentScript({});
  await ctx.send({ action: 'startLogging' });
  const hostile = { get reason() { throw new Error('nope'); } };
  ctx.window.emit('unhandledrejection', hostile);

  const logs = await ctx.send({ action: 'getConsoleLogs' });
  assert.equal(logs.logs.length, 1);
  assert.match(logs.logs[0].message, /unprintable/);
});

// --------------------------------------------------------------------------
// An observer the agent forgets about must not run for the page's lifetime

test('observeElement expires on its own and says when', async () => {
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  const started = await ctx.send({
    action: 'observeElement', selector: '#watch', maxLifetimeMs: 40
  });
  assert.equal(started.observing, true);
  assert.equal(started.expiresInMs, 40,
    'the caller should know it will not run forever');

  const observer = ctx.observers[ctx.observers.length - 1];
  assert.equal(observer.observing, true);

  await new Promise(resolve => setTimeout(resolve, 70));
  assert.equal(observer.disconnected, true,
    'a full-subtree observer is a real cost on an animation-heavy page');
});

test('an expired observer still returns the changes it collected', async () => {
  // This used to call stopObserving with observerId: undefined and assert
  // found === false, which has always been true for an unknown id - so it
  // proved nothing about its own title and passed with the whole expiry
  // feature reverted. Drive a real change through the observer, let it
  // expire, then ask for the changes by their real id.
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  const started = await ctx.send({ action: 'observeElement',
                                   selector: '#watch', maxLifetimeMs: 40 });
  const observer = ctx.observers.at(-1);
  observer.cb([{ type: 'childList', target,
                 addedNodes: [makeElement('span', {})], removedNodes: [] }]);

  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(observer.disconnected, true,
               'the lifetime elapsed, so the observer must be disconnected');

  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: started.observerId });
  assert.equal(stopped.found !== false, true,
               'the record must survive expiry so its changes can be read');
  assert.equal(stopped.changes.length, 1,
               'a change collected before expiry must still be returned');
});

test('an unknown observer id reports not-found rather than throwing', async () => {
  const ctx = loadContentScript({});
  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: 'never-existed' });
  assert.equal(stopped.found, false);
});

test('stopObserving reports whether the observer had already expired', async () => {
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  const started = await ctx.send({ action: 'observeElement',
                                   selector: '#watch', maxLifetimeMs: 30 });
  await new Promise(resolve => setTimeout(resolve, 60));

  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: started.observerId });
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.expired, true,
    'the change list stops where the observer stopped; say so');
});

test('an observer stopped in time is not marked expired', async () => {
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  const started = await ctx.send({ action: 'observeElement',
                                   selector: '#watch', maxLifetimeMs: 5000 });
  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: started.observerId });
  assert.equal(stopped.expired, false);
});

test('whole-page get_text does not return a contenteditable credential', async () => {
  // <body> is the default target, so browser_get_text with no selector
  // returned a contenteditable PIN in the middle of the page dump. Only
  // contenteditable credentials can reach innerText - an <input> contributes
  // nothing to it whatever its value - so enumerating them covers it.
  const pin = makeElement('div', { id: 'otp-code', textContent: '483920' });
  pin.isContentEditable = true;
  const body = makeElement('body', {
    textContent: 'Enter your code 483920 then continue'
  });
  const ctx = loadContentScript({ '[contenteditable]': [pin] });
  ctx.document.body.innerText = 'Enter your code 483920 then continue';

  const result = await ctx.send({ action: 'getText' });

  assert.ok(!result.text.includes('483920'),
            `the PIN was returned in the page text: ${result.text}`);
  assert.equal(result.maskedFields, 1);
  assert.ok(result.text.includes('Enter your code'),
            'the rest of the page stays readable');
});

test('a credential field past the 50th editable element is still masked', async () => {
  // The cap was applied to ALL contenteditable elements before the credential
  // filter ran, so a Notion- or CMS-style page with 50 ordinary editable
  // cells followed by one credential field never reached the credential -
  // the exact leak this function exists to stop, back on any busy page.
  const cells = Array.from({ length: 60 }, (_, i) =>
    Object.assign(makeElement('div', { id: 'cell-' + i,
                                       textContent: 'note ' + i }),
                  { isContentEditable: true }));
  const otp = Object.assign(
    makeElement('div', { id: 'otp-code', textContent: '483920' }),
    { isContentEditable: true });
  const ctx = loadContentScript({ '[contenteditable]': [...cells, otp] });
  ctx.document.body.innerText = 'note 0 note 1 483920 done';

  const result = await ctx.send({ action: 'getText' });

  assert.ok(!result.text.includes('483920'),
            `the OTP leaked from behind 60 ordinary cells: ${result.text}`);
  assert.equal(result.maskedFields, 1);
});

test('a not-found error names the locator, never the text being typed', async () => {
  // These errors embedded JSON.stringify(options), and `text` is the value
  // being TYPED - so a failed browser_type put the password into the error,
  // which the agent printed and kept in its action history. The client-side
  // scrub catches it now too, but not building the string that way is the
  // better fix: the value is never part of the locator.
  const ctx = loadContentScript({});

  const result = await ctx.send({ action: 'type', name: 'password',
                                  text: 'hunter2-correct-horse' });

  assert.equal(result.success, false);
  assert.ok(!result.error.includes('hunter2-correct-horse'),
            `the typed value is in the error: ${result.error}`);
  assert.match(result.error, /name="password"/,
               'the locator must still be named, or the error is useless');
});

test('a textarea holding a credential is masked in whole-page text', async () => {
  // The comment claimed "an <input> contributes nothing to innerText", which
  // is true, and then relied on that for every form control. A <textarea>'s
  // text IS rendered, so its value can appear in document.body.innerText -
  // the reasoning did not generalise, and relying on it was an overclaim.
  const ta = makeElement('textarea', { id: 'otp-field', value: '483920' });
  const ctx = loadContentScript({ 'textarea': [ta] });
  ctx.document.body.innerText = 'code 483920 ok';

  const result = await ctx.send({ action: 'getText' });

  assert.ok(!result.text.includes('483920'),
            `a textarea credential leaked: ${result.text}`);
  assert.equal(result.maskedFields, 1);
});

test('a credential whose text starts with another is fully masked', async () => {
  // In document order, masking a shorter secret that is a PREFIX of a longer
  // one destroys the longer one's text and leaves its tail behind: two OTP
  // fields holding "4839" and "48391" came out as "*** ***1", so a digit of
  // the second credential survived and it was not counted as masked.
  const a = makeElement('div', { id: 'otp-a', textContent: '4839' });
  const b = makeElement('div', { id: 'otp-b', textContent: '48391' });
  a.isContentEditable = true;
  b.isContentEditable = true;
  const ctx = loadContentScript({ '[contenteditable]': [a, b] });
  ctx.document.body.innerText = 'codes 4839 and 48391 ok';

  const result = await ctx.send({ action: 'getText' });

  assert.ok(!/4839/.test(result.text),
            `a fragment of a credential survived: ${result.text}`);
  assert.equal(result.maskedFields, 2);
  assert.ok(result.text.includes('codes') && result.text.includes('ok'),
            'the surrounding page stays readable');
});

test('whole-page get_text leaves ordinary contenteditable text alone', async () => {
  const notes = makeElement('div', { id: 'notes', textContent: 'Buy milk' });
  notes.isContentEditable = true;
  const ctx = loadContentScript({ '[contenteditable]': [notes] });
  ctx.document.body.innerText = 'Reminders Buy milk';

  const result = await ctx.send({ action: 'getText' });

  assert.ok(result.text.includes('Buy milk'),
            'an ordinary editable field is not a credential');
  assert.equal(result.maskedFields, undefined);
});

test('set_value refuses an element it cannot set, instead of claiming success', async () => {
  // Everything that was not INPUT/TEXTAREA/SELECT/contenteditable fell
  // through to an unconditional {set: true}, so browser_set_value on a <div>
  // reported success having changed nothing.
  const div = makeElement('div', { id: 'q', textContent: 'old' });
  const ctx = loadContentScript({ '#q': div });

  const result = await ctx.send({ action: 'setValue', selector: '#q',
                                  value: 'new' });

  assert.equal(result.success, false, 'nothing was changed, so not success');
  assert.match(result.error, /not an input|cannot set/i);
  assert.equal(div.textContent, 'old');
});

test('set_value on a custom element verifies the value took', async () => {
  // <sl-input> is the element an agent must target on Shoelace/Ionic/Vaadin
  // pages, because the real input is inside a shadow root - the credential
  // guard in this same file treats it that way. Assigning is the honest
  // attempt; reading it back is what stops a lie when a framework overwrites.
  const ok = makeElement('sl-input', { id: 'a', value: 'old' });
  let ctx = loadContentScript({ '#a': ok });
  let result = await ctx.send({ action: 'setValue', selector: '#a',
                                value: 'new' });
  assert.equal(result.set, true);
  assert.equal(ok.value, 'new');

  // One that ignores the assignment, as a controlled component does.
  const stubborn = makeElement('sl-input', { id: 'b', value: 'old' });
  Object.defineProperty(stubborn, 'value',
                        { get: () => 'old', set: () => {} });
  ctx = loadContentScript({ '#b': stubborn });
  result = await ctx.send({ action: 'setValue', selector: '#b',
                            value: 'new' });
  assert.equal(result.success, false,
    'an assignment the element dropped must not report set: true');
  assert.match(result.error, /did not take/i);
});

test('observe_element does not report a credential as a mutation oldValue', async () => {
  // The observer watches attributes with attributeOldValue and returned
  // mutation.oldValue verbatim, so any page that rewrites the `value`
  // ATTRIBUTE of a password or OTP input handed the old value to the agent.
  // Worse than the other readers: browser_observe_element counts as
  // observation, so it is allowed in read-only mode and on a protected site
  // with no confirmation, and the server never attaches allow_password to it.
  const pw = makeElement('input', { id: 'pw', type: 'password' });
  const ctx = loadContentScript({ '#pw': pw });

  const started = await ctx.send({ action: 'observeElement', selector: '#pw' });
  const observer = ctx.observers.at(-1);
  observer.cb([{ type: 'attributes', target: pw, attributeName: 'value',
                 oldValue: 'OLD-SECRET-PASSWORD',
                 addedNodes: [], removedNodes: [] }]);

  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: started.observerId });

  const dump = JSON.stringify(stopped);
  assert.ok(!dump.includes('OLD-SECRET-PASSWORD'),
            `the previous credential leaked through a mutation: ${dump}`);
  assert.equal(stopped.changes[0].oldValue, '***');
});

test('observe_element still reports an ordinary attribute change', async () => {
  const box = makeElement('div', { id: 'box' });
  const ctx = loadContentScript({ '#box': box });

  const started = await ctx.send({ action: 'observeElement', selector: '#box' });
  ctx.observers.at(-1).cb([{ type: 'attributes', target: box,
                             attributeName: 'class', oldValue: 'open',
                             addedNodes: [], removedNodes: [] }]);
  const stopped = await ctx.send({ action: 'stopObserving',
                                   observerId: started.observerId });

  assert.equal(stopped.changes[0].oldValue, 'open',
    'masking an ordinary class change would make the tool useless');
});

test('scroll_and_capture masks a credential field like get_page_info does', async () => {
  // Both use the same selector list; getPageInfo masked and this did not, so
  // the same field read *** from one tool and in clear from the other.
  const ta = makeElement('textarea', { id: 't', name: 'password',
                                       textContent: 'hunter2-from-textarea' });
  const otp = Object.assign(
    makeElement('div', { id: 'otp-code', textContent: '884213' }),
    { isContentEditable: true });
  const ctx = loadContentScript({ '__interactive__': [ta, otp] });

  // delay 0 and one scroll: the default is 500ms x 20 scrolls.
  const result = await ctx.send({ action: 'scrollAndCapture',
                                  delay: 0, maxScrolls: 1 });

  const dump = JSON.stringify(result);
  assert.ok(!dump.includes('hunter2-from-textarea'),
            `a textarea credential leaked: ${dump}`);
  assert.ok(!dump.includes('884213'),
            `a contenteditable OTP leaked: ${dump}`);
});

// --------------------------------------------------------------------------
// The credential guard, against the markup real pages ship.
//
// The guard used to look only at input[type=password] and the autocomplete
// token list, which three normal patterns walk straight past. background.js
// already scrubbed the same names out of captured HTML with SECRET_KEY_RE,
// so the extension was masking a field in one result and printing it in
// another.

test('a shadow-DOM credential host is guarded, not just a bare <input>', async () => {
  // Shoelace, Ionic and Vaadin put the real input in a shadow root, so the
  // host element is the only thing an agent can target.
  const pw = makeElement('sl-input', {
    id: 'pw', value: 'SuperSecret123!', attributes: { type: 'password' }
  });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'getValue', selector: '#pw' });

  assert.equal(result.value, '***');
  assert.equal(result.masked, true);
  assert.ok(!JSON.stringify(result).includes('SuperSecret123!'));
});

test('typing into a shadow-DOM credential host is refused too', async () => {
  const pw = makeElement('sl-input', {
    id: 'pw', value: '', attributes: { type: 'password' }
  });
  const { send } = loadContentScript({ '#pw': pw });

  const result = await send({ action: 'type', selector: '#pw', text: 'nope' });

  assert.equal(result.success, false);
  assert.match(result.error, /password field/i);
});

test('a credential-shaped name or id is guarded whatever the type says', async () => {
  const names = ['passwd', 'pwd', 'cvv', 'otp', 'ssn', 'user[password]',
                 'privateKey', 'sessionToken', 'card-number', 'api_key'];
  for (const name of names) {
    const byName = makeElement('input', { id: 'f', name, type: 'text', value: 'SECRET' });
    const byName_ctx = loadContentScript({ '#f': byName });
    const byNameResult = await byName_ctx.send({ action: 'getValue', selector: '#f' });
    assert.equal(byNameResult.value, '***', `name="${name}" must be guarded`);

    const byId = makeElement('input', { id: name, type: 'text', value: 'SECRET' });
    const byId_ctx = loadContentScript({ [`#${name}`]: byId });
    const byIdResult = await byId_ctx.send({ action: 'getValue', selector: `#${name}` });
    assert.equal(byIdResult.value, '***', `id="${name}" must be guarded`);
  }
});

test('writing to a credential-shaped name is refused as well', async () => {
  const field = makeElement('input', { id: 'f', name: 'passwd', type: 'text', value: '' });
  const { send } = loadContentScript({ '#f': field });

  const typed = await send({ action: 'type', selector: '#f', text: 'nope' });
  assert.equal(typed.success, false);
  assert.match(typed.error, /password field/i);
  assert.equal(field.value, '');

  const set = await send({ action: 'setValue', selector: '#f', value: 'nope' });
  assert.equal(set.success, false);
  assert.equal(field.value, '');
});

test('ordinary fields are not swept up by the name guard', async () => {
  const cases = [
    ['email', 'a@b.test'],
    ['search', 'shoes'],
    ['first-name', 'Albert'],
    ['quantity', '2']
  ];
  for (const [name, value] of cases) {
    const el = makeElement('input', { id: 'f', name, type: 'text', value });
    const { send } = loadContentScript({ '#f': el });
    const result = await send({ action: 'getValue', selector: '#f' });
    assert.equal(result.value, value, `name="${name}" is not a credential`);
    assert.notEqual(result.masked, true);
  }
});

test('ordinary page text is not masked by the name guard', async () => {
  // The name/id rule is for fields, not for every element that happens to
  // have "session" or "auth" in its id.
  const banner = makeElement('div', {
    id: 'user-session-banner', textContent: 'Signed in as albert'
  });
  const { send } = loadContentScript({ '#user-session-banner': banner,
                                       div: [banner] });

  const text = await send({ action: 'getText', selector: '#user-session-banner' });
  assert.equal(text.text, 'Signed in as albert');
  assert.notEqual(text.masked, true);

  const elements = await send({ action: 'getElements', selector: 'div' });
  assert.equal(elements.elements[0].text, 'Signed in as albert');
});

test('a contenteditable credential field is masked, value and text', async () => {
  // No type, no value property: the code path that leaks is textContent.
  const pin = makeElement('div', { id: 'otp-code', textContent: '482913' });
  pin.isContentEditable = true;
  const { send } = loadContentScript({ '#otp-code': pin, div: [pin] });

  const value = await send({ action: 'getValue', selector: '#otp-code' });
  assert.equal(value.value, '***');
  assert.equal(value.masked, true);
  assert.ok(!JSON.stringify(value).includes('482913'));

  const text = await send({ action: 'getText', selector: '#otp-code' });
  assert.ok(!JSON.stringify(text).includes('482913'),
    'get_text on a credential field is just another way to read it');

  const elements = await send({ action: 'getElements', selector: 'div' });
  assert.ok(!JSON.stringify(elements).includes('482913'),
    'element text is incidental metadata; a credential in it is still a leak');
});

test('browser_get_page_info leaks none of the three', async () => {
  const host = makeElement('sl-input', {
    id: 'pw', value: 'HOST-SECRET', attributes: { type: 'password', tabindex: '0' }
  });
  const named = makeElement('input', { id: 'c', name: 'cvv', type: 'text', value: 'CVV-SECRET' });
  const pin = makeElement('div', { id: 'otp-code', textContent: 'PIN-SECRET' });
  pin.isContentEditable = true;
  const { send } = loadContentScript({ __interactive__: [host, named, pin] });

  const result = await send({ action: 'getPageInfo' });

  const serialized = JSON.stringify(result);
  for (const secret of ['HOST-SECRET', 'CVV-SECRET', 'PIN-SECRET']) {
    assert.ok(!serialized.includes(secret), `${secret} must not be returned`);
  }
});

// --------------------------------------------------------------------------
// A numeric .value must not turn a completed click into an error

test('browser_click on an element with a numeric value still reports success', async () => {
  // .value is an IDL number on <li>, <progress>, <meter> and on custom
  // elements like <md-slider>. The old reader called .substring on it and
  // threw - after the click had been dispatched - so an agent that retried
  // clicked twice.
  let clicks = 0;
  const item = makeElement('li', { id: 'opt', value: 0, textContent: 'Option' });
  item.dispatchEvent = (event) => {
    if (event.type === 'click') clicks++;
    return true;
  };
  const { send } = loadContentScript({ '#opt': item });

  const result = await send({ action: 'click', selector: '#opt' });

  assert.notEqual(result.success, false,
    `a delivered click must not be reported as a failure: ${result.error}`);
  assert.equal(result.clicked, true);
  assert.equal(clicks, 1);
  assert.equal(result.element.value, '0');
});

test('a numeric value is reported in element metadata, not thrown over', async () => {
  const meter = makeElement('meter', { id: 'm', value: 42 });
  const slider = makeElement('md-slider', { id: 's', value: 0 });
  const { send } = loadContentScript({ '[role="slider"]': [meter, slider] });

  const result = await send({ action: 'getElements', selector: '[role="slider"]' });

  assert.notEqual(result.success, false, `${result.error}`);
  assert.deepEqual(Array.from(result.elements, e => e.value), ['42', '0']);
});

// --------------------------------------------------------------------------
// Feature flags arrive as strings. "false" must mean false.

test('browser_type honours string "false" for clear, press_enter and submit_form', async () => {
  const field = makeElement('input', { id: 'q', type: 'text', value: 'keep' });
  const keys = [];
  const submits = [];
  field.dispatchEvent = (event) => {
    if (event.type.startsWith('key')) keys.push(event.key);
    return true;
  };
  field.closest = () => ({
    tagName: 'FORM',
    dispatchEvent: (event) => { submits.push(event.type); return true; }
  });
  const { send } = loadContentScript({ '#q': field });

  const result = await send({
    action: 'type', selector: '#q', text: 'x', instant: 'true',
    clear: 'false', pressEnter: 'false', submitForm: 'false'
  });

  assert.equal(result.typed, true);
  assert.equal(field.value, 'keepx', 'clear: "false" must not clear the field');
  assert.deepEqual(keys, [], 'press_enter: "false" must not press Enter');
  assert.deepEqual(submits, [], 'submit_form: "false" must not submit the form');
});

test('submit_form: "false" holds even when Enter is asked for', async () => {
  const field = makeElement('input', { id: 'q', type: 'text', value: '' });
  const submits = [];
  field.closest = () => ({
    tagName: 'FORM',
    dispatchEvent: (event) => { submits.push(event.type); return true; }
  });
  const { send } = loadContentScript({ '#q': field });

  await send({
    action: 'type', selector: '#q', text: 'x', instant: 'true',
    pressEnter: 'true', submitForm: 'false'
  });

  assert.deepEqual(submits, [],
    'submitting a form the caller declined is destructive and not undoable');
});

test('string flags are read elsewhere too: inspect_element viewport', async () => {
  const ctx = loadContentScript({});
  const asked = [];
  ctx.document.elementFromPoint = (x, y) => { asked.push([x, y]); return null; };
  ctx.window.scrollY = 100;

  await ctx.send({ action: 'inspectElement', x: 10, y: 10, viewport: 'true' });

  assert.deepEqual(asked, [[10, 10]],
    'viewport: "true" means the coordinates are already viewport-relative');
});

// --------------------------------------------------------------------------
// get_page_info must return the elements it counts

test('a visible element after 100 zero-size matches is still returned', async () => {
  // Hidden inputs and offscreen [tabindex] holders are normal on a real page
  // and report an all-zero rect. The cap used to count matches rather than
  // collected elements, so they ate the whole budget.
  const zeroRect = { left: 0, top: 0, width: 0, height: 0,
                     x: 0, y: 0, right: 0, bottom: 0 };
  const hidden = [];
  for (let i = 0; i < 100; i++) {
    hidden.push(makeElement('input', { id: `h${i}`, type: 'hidden', rect: zeroRect }));
  }
  const checkout = makeElement('button', { id: 'checkout', textContent: 'Place order' });
  const { send } = loadContentScript({ __interactive__: [...hidden, checkout] });

  const result = await send({ action: 'getPageInfo' });

  assert.equal(result.interactiveElementCount, 101);
  assert.equal(result.interactiveElementsReturned, 1);
  assert.equal(result.interactiveElementsTruncated, false,
    'nothing was dropped, so the result must not claim it was');
  assert.deepEqual(Array.from(result.interactiveElements, e => e.id), ['checkout']);
});

test('get_page_info still caps the list and says when it did', async () => {
  const many = [];
  for (let i = 0; i < 150; i++) {
    many.push(makeElement('button', { id: `b${i}`, textContent: `b${i}` }));
  }
  const { send } = loadContentScript({ __interactive__: many });

  const result = await send({ action: 'getPageInfo' });

  assert.equal(result.interactiveElementsReturned, 100);
  assert.equal(result.interactiveElementCount, 150);
  assert.equal(result.interactiveElementsTruncated, true);
});

// --------------------------------------------------------------------------
// Reusing an observer_id replaces the observer

test('observe_element with a reused observer_id replaces the old observer', async () => {
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  const first = await ctx.send({ action: 'observeElement', selector: '#watch',
                                 observerId: 'obs-1', maxLifetimeMs: 40 });
  assert.equal(first.observing, true);

  const second = await ctx.send({ action: 'observeElement', selector: '#watch',
                                  observerId: 'obs-1', maxLifetimeMs: 5000 });
  assert.notEqual(second.success, false,
    `observer_id is documented, so reusing one must work: ${second.error}`);
  assert.equal(second.observing, true);
  assert.equal(second.observerId, 'obs-1');

  const [old, current] = ctx.observers.slice(-2);
  assert.equal(old.disconnected, true, 'the replaced observer must be let go');
  assert.equal(current.observing, true);
});

test("the replaced observer's expiry timer does not stop the new one", async () => {
  const target = makeElement('div', { id: 'watch' });
  const ctx = loadContentScript({ '#watch': target });

  await ctx.send({ action: 'observeElement', selector: '#watch',
                   observerId: 'obs-1', maxLifetimeMs: 30 });
  await ctx.send({ action: 'observeElement', selector: '#watch',
                   observerId: 'obs-1', maxLifetimeMs: 5000 });

  // Past the first observer's lifetime. Its timer looks the id up again when
  // it fires, so it would find - and kill - the replacement.
  await new Promise(resolve => setTimeout(resolve, 60));

  const current = ctx.observers[ctx.observers.length - 1];
  assert.equal(current.observing, true, 'the new observer must still be watching');

  const stopped = await ctx.send({ action: 'stopObserving', observerId: 'obs-1' });
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.expired, false,
    'the replacement had 5s left; it must not be reported as expired');
});

// --------------------------------------------------------------------------
// A selector is handed back as the handle for the next call, so it has to
// survive being parsed again

test('generateSelector escapes an id that CSS reads as syntax', async () => {
  const headless = makeElement('input', { id: 'headlessui-menu-item-:r1:', value: 'a' });
  const dotted = makeElement('input', { id: 'user.email', value: 'b' });
  const { send } = loadContentScript({ input: [headless, dotted] });

  const result = await send({ action: 'getElements', selector: 'input' });

  assert.equal(result.elements[0].selector, '#headlessui-menu-item-\\:r1\\:',
    'an unescaped colon makes querySelector throw');
  assert.equal(result.elements[1].selector, '#user\\.email',
    '#user.email means "#user with class email", not that id');
});

test('generateSelector escapes class names too', async () => {
  const el = makeElement('div', { className: 'md:flex w-1/2' });
  el.parentElement = null;
  const { send } = loadContentScript({ div: [el] });

  const result = await send({ action: 'getElements', selector: 'div' });

  assert.equal(result.elements[0].selector, 'div.md\\:flex.w-1\\/2');
});

test('a name needle containing a quote cannot re-target the lookup', async () => {
  const ctx = loadContentScript({});
  const asked = [];
  ctx.document.querySelector = (sel) => { asked.push(sel); return null; };

  await ctx.send({ action: 'click', name: 'a"],[name="transfer-all' });

  assert.deepEqual(asked, ['[name="a\\"],[name=\\"transfer-all"]'],
    'the needle must stay one quoted string, not become selector syntax');
});

test('generateXPath quotes an id containing a quote', async () => {
  const el = makeElement('div', { id: 'a"b' });
  const ctx = loadContentScript({ '#a\\"b': el });
  ctx.document.elementFromPoint = () => el;

  const result = await ctx.send({ action: 'inspectElement', x: 1, y: 1,
                                  viewport: true });

  assert.equal(result.xpath, '//*[@id=\'a"b\']',
    'an unescaped quote closes the literal and the rest is read as XPath');
});

// --------------------------------------------------------------------------
// browser_type must not claim to have typed into something that cannot type

test('typing with focus_first:false into <body> does not claim success', async () => {
  // document.activeElement is <body> whenever nothing is focused, and <body>
  // swallows every character. Reporting typed: true there is a lie the agent
  // builds its next step on.
  const ctx = loadContentScript({});

  const result = await ctx.send({
    action: 'type', text: 'hello', focusFirst: false, instant: true
  });

  assert.equal(result.success, false);
  assert.match(result.error, /cannot receive typed text/i);
  assert.match(result.error, /Nothing was typed/);
});

test('typing into a plain <div> does not claim success either', async () => {
  const div = makeElement('div', { id: 'box' });
  const { send } = loadContentScript({ '#box': div });

  const result = await send({ action: 'type', selector: '#box', text: 'hello' });

  assert.equal(result.success, false);
  assert.match(result.error, /cannot receive typed text/i);
});

test('typing into a contenteditable element still works', async () => {
  const editor = makeElement('div', { id: 'editor' });
  editor.isContentEditable = true;
  const { send } = loadContentScript({ '#editor': editor });

  const result = await send({
    action: 'type', selector: '#editor', text: 'hello', instant: true
  });

  assert.equal(result.typed, true);
  assert.equal(editor.textContent, 'hello');
});

// --------------------------------------------------------------------------
// Checkboxes, radios and <select>: reading state, not the submit string.

test('browser_get_value reports a checkbox state, not its submit string', async () => {
  // The default value attribute of a checkbox is "on" whether it is ticked
  // or not, so returning it told the caller nothing about the consent box.
  const off = makeElement('input', { id: 'tos', type: 'checkbox', value: 'on' });
  const on = makeElement('input', { id: 'ads', type: 'checkbox', value: 'on', checked: true });
  const { send } = loadContentScript({ '#tos': off, '#ads': on });

  const unchecked = await send({ action: 'getValue', selector: '#tos' });
  const checked = await send({ action: 'getValue', selector: '#ads' });

  assert.equal(unchecked.value, false);
  assert.equal(unchecked.checked, false);
  assert.equal(unchecked.submitValue, 'on');
  assert.equal(checked.value, true);
  assert.equal(checked.checked, true);
  assert.notEqual(unchecked.value, checked.value,
    'a ticked and an unticked box must not read back the same');
});

test('browser_get_value tells the selected radio from the unselected one', async () => {
  const yes = makeElement('input', { id: 'yes', type: 'radio', name: 'ship', value: 'yes' });
  const no = makeElement('input', {
    id: 'no', type: 'radio', name: 'ship', value: 'no', checked: true
  });
  const { send } = loadContentScript({ '#yes': yes, '#no': no });

  assert.equal((await send({ action: 'getValue', selector: '#yes' })).checked, false);
  assert.equal((await send({ action: 'getValue', selector: '#no' })).checked, true);
});

test('browser_set_value ticks a checkbox instead of rewriting its value', async () => {
  const box = makeElement('input', { id: 'tos', type: 'checkbox', value: 'on' });
  const { send } = loadContentScript({ '#tos': box });

  const result = await send({ action: 'setValue', selector: '#tos', value: true });

  assert.equal(result.set, true);
  assert.equal(result.checked, true);
  assert.equal(box.checked, true);
  assert.equal(box.value, 'on', 'the submit string must be left alone');

  const cleared = await send({ action: 'setValue', selector: '#tos', value: 'off' });
  assert.equal(cleared.checked, false);
  assert.equal(box.checked, false);
});

test('browser_set_value refuses a checkbox value that is not on or off', async () => {
  const box = makeElement('input', { id: 'tos', type: 'checkbox', value: 'on' });
  const { send } = loadContentScript({ '#tos': box });

  const result = await send({ action: 'setValue', selector: '#tos', value: 'hello' });

  assert.equal(result.success, false);
  assert.match(result.error, /checked/i);
  assert.equal(box.checked, false, 'a refused set must not change the box');
});

test('browser_set_value on a <select> reports the option it landed on', async () => {
  const picker = makeElement('select', {
    id: 'country',
    options: [{ value: 'nl', text: 'Netherlands' }, { value: 'be', text: 'Belgium' }]
  });
  const { send } = loadContentScript({ '#country': picker });

  const result = await send({ action: 'setValue', selector: '#country', value: 'be' });

  assert.equal(result.set, true);
  assert.equal(result.value, 'be');
  assert.equal(result.text, 'Belgium');
  assert.equal(picker.selectedIndex, 1);
});

test('browser_set_value on a <select> refuses a value no option carries', async () => {
  // A real <select> resets to '' here, so {set: true} was a lie.
  const picker = makeElement('select', {
    id: 'country',
    options: [{ value: 'nl', text: 'Netherlands' }, { value: 'be', text: 'Belgium' }]
  });
  const { send } = loadContentScript({ '#country': picker });

  const result = await send({ action: 'setValue', selector: '#country', value: 'fr' });

  assert.equal(result.success, false);
  assert.match(result.error, /no option matching/i);
  assert.match(result.error, /nl, be/);
  assert.equal(picker.value, 'nl', 'the dropdown must stay where it was');
});

test('browser_select_option refuses text that matches no option', async () => {
  const picker = makeElement('select', {
    id: 'country',
    options: [{ value: 'nl', text: 'Netherlands' }, { value: 'be', text: 'Belgium' }]
  });
  const { send } = loadContentScript({ '#country': picker });

  const result = await send({ action: 'selectOption', selector: '#country', text: 'France' });

  assert.equal(result.success, false);
  assert.match(result.error, /no option matching/i);
  assert.equal(picker.selectedIndex, 0, 'the dropdown must stay where it was');
});

test('browser_select_option by text and by index report where they landed', async () => {
  const picker = makeElement('select', {
    id: 'country',
    options: [{ value: 'nl', text: 'Netherlands' },
              { value: 'be', text: 'Belgium' },
              { value: 'de', text: 'Germany' }]
  });
  const { send } = loadContentScript({ '#country': picker });

  const byText = await send({ action: 'selectOption', selector: '#country', text: 'Germany' });
  assert.equal(byText.selected, true);
  assert.equal(byText.value, 'de');
  assert.equal(byText.index, 2);

  const byIndex = await send({ action: 'selectOption', selector: '#country', index: 1 });
  assert.equal(byIndex.value, 'be');
  assert.equal(byIndex.text, 'Belgium');

  const outOfRange = await send({ action: 'selectOption', selector: '#country', index: 9 });
  assert.equal(outOfRange.success, false);
  assert.equal(picker.value, 'be', 'the dropdown must stay where it was');
});

// --------------------------------------------------------------------------
// browser_get_computed_styles asks the stylesheet, so it must use CSS
// property names.

test('browser_get_computed_styles returns the hyphenated CSS properties', async () => {
  const box = makeElement('div', {
    id: 'box',
    styles: {
      'background-color': 'rgb(255, 0, 0)',
      'font-size': '14px',
      'font-family': 'Inter'
    }
  });
  const { send } = loadContentScript({ '#box': box });

  const result = await send({ action: 'getComputedStyles', selector: '#box' });

  assert.equal(result.styles['background-color'], 'rgb(255, 0, 0)');
  assert.equal(result.styles['font-size'], '14px');
  assert.equal(result.styles['font-family'], 'Inter');
});

test('browser_get_computed_styles still answers a camelCase request', async () => {
  const box = makeElement('div', {
    id: 'box', styles: { 'background-color': 'rgb(0, 128, 0)' }
  });
  const { send } = loadContentScript({ '#box': box });

  const result = await send({
    action: 'getComputedStyles', selector: '#box', properties: ['backgroundColor']
  });

  assert.equal(result.styles.backgroundColor, 'rgb(0, 128, 0)',
    'the answer is keyed by the name the caller asked for');
});

// --------------------------------------------------------------------------
// browser_get_text promises visible text.

test('browser_get_text returns the visible text, not the hidden markup', async () => {
  // A collapsed admin template: innerText skips a display:none subtree,
  // textContent does not, and `innerText || textContent` fell through to
  // textContent whenever the visible text was empty.
  const panel = makeElement('div', {
    id: 'panel',
    innerText: 'Welcome back',
    textContent: 'Welcome back ADMIN PANEL token=abc123'
  });
  const { send } = loadContentScript({ '#panel': panel });

  const result = await send({ action: 'getText', selector: '#panel' });

  assert.equal(result.text, 'Welcome back');
  assert.equal(result.source, 'innerText');
  assert.ok(!result.text.includes('token=abc123'),
    'hidden text must not be returned as visible text');
});

test('browser_get_text reports empty visible text as empty', async () => {
  const hidden = makeElement('div', {
    id: 'tpl', innerText: '', textContent: 'ADMIN PANEL token=abc123'
  });
  const { send } = loadContentScript({ '#tpl': hidden });

  const result = await send({ action: 'getText', selector: '#tpl' });

  assert.equal(result.text, '');
  assert.equal(result.totalLength, 0);
  assert.equal(result.source, 'innerText');
});

test('browser_get_text falls back to textContent and says so', async () => {
  // Nodes without an innerText (SVG elements, detached nodes) still need
  // reading, so the fallback stays - it is just labelled now.
  const svg = makeElement('g', { id: 'label', textContent: 'Revenue' });
  delete svg.innerText;
  const { send } = loadContentScript({ '#label': svg });

  const result = await send({ action: 'getText', selector: '#label' });

  assert.equal(result.text, 'Revenue');
  assert.equal(result.source, 'textContent');
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
