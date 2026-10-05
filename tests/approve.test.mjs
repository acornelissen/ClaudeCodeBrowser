/**
 * Approval-page tests. No dependencies: the few elements, events, timers and
 * the clock that extension/approve/approve.js touches are stubbed here, the
 * script is evaluated against them, and the messages it sends to the
 * background are recorded.
 *
 * This page is the only trusted surface where a person decides, so the tests
 * pin what may and may not count as that decision.
 *
 * Run: node tests/approve.test.mjs
 */

import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(here, '..', 'extension', 'approve', 'approve.js'), 'utf8');

const REQUEST_ID = 'approval_1_1700000000000';

/** Anything that can be listened to: an element, the document, the window. */
function makeTarget(props = {}) {
  const listeners = {};
  return {
    ...props,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    /** isTrusted false is what page script or dispatchEvent produces; true is
     *  what a real click or keypress produces. */
    emit(type, init = {}) {
      (listeners[type] || []).forEach(fn => fn({ type, isTrusted: false, ...init }));
    },
  };
}

/** An element that keeps text and markup apart. Every way of writing markup
 *  is recorded, so a test can tell "rendered as text" from "parsed as HTML"
 *  without a real DOM. */
function makeElement(id) {
  const el = makeTarget({ id, textContent: '', hidden: false, disabled: false });
  el.htmlWrites = [];
  for (const prop of ['innerHTML', 'outerHTML']) {
    Object.defineProperty(el, prop, {
      get: () => el.textContent,
      set: (value) => { el.htmlWrites.push([prop, value]); },
    });
  }
  el.insertAdjacentHTML = (where, value) => {
    el.htmlWrites.push(['insertAdjacentHTML', value]);
  };
  return el;
}

/**
 * Load approve.js. `details` is what the background answers to the page's
 * details request; `detailsFail` makes that request reject instead.
 */
async function loadApprovePage({ details = { found: true, message: 'do a thing' },
                                 detailsFail = false } = {}) {
  const elements = {};
  for (const id of ['heading', 'message', 'detail', 'detail-heading', 'site',
                    'countdown', 'approve', 'deny']) {
    elements[id] = makeElement(id);
  }

  const sent = [];
  let closed = false;

  let now = 1_000_000;
  const intervals = new Map();
  let nextTimer = 1;

  const document = makeTarget({
    title: '',
    getElementById: (id) => elements[id] || null,
  });
  const window = makeTarget({
    location: { search: `?id=${encodeURIComponent(REQUEST_ID)}` },
    close() { closed = true; },
  });

  const context = createContext({
    window,
    document,
    URLSearchParams,
    console,
    Date: { now: () => now },
    setInterval: (fn, ms) => { intervals.set(nextTimer, { fn, ms }); return nextTimer++; },
    clearInterval: (id) => { intervals.delete(id); },
    browser: {
      runtime: {
        sendMessage: async (message) => {
          sent.push(message);
          if (message.action === 'details') {
            if (detailsFail) throw new Error('background unavailable');
            return details;
          }
          return { ok: true };
        },
      },
    },
  });

  runInContext(SOURCE, context);
  await settle();

  return {
    elements, document, window,
    // Copied out of the vm realm: its objects have a different Object
    // prototype, which deepStrictEqual counts as a difference.
    decisions: () => JSON.parse(JSON.stringify(sent.filter(m => m.action === 'decide'))),
    isClosed: () => closed,
    /** Advance the clock one second and fire the countdown, as setInterval would. */
    tick: async () => {
      now += 1000;
      for (const { fn } of [...intervals.values()]) fn();
      await settle();
    },
    settle,
  };
}

/** Let the page's promise chains (details, then decide, then close) run. */
function settle() {
  return new Promise(resolve => setImmediate(resolve));
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --------------------------------------------------------------------------
// A decision has to come from a person

test('a real click on Approve approves, once, and closes the window', async () => {
  const page = await loadApprovePage();

  page.elements.approve.emit('click', { isTrusted: true });
  page.elements.approve.emit('click', { isTrusted: true });
  await page.settle();

  assert.deepEqual(page.decisions(), [
    { target: 'approval', action: 'decide', requestId: REQUEST_ID, approved: true },
  ], 'one decision, carrying the id from the page URL');
  assert.equal(page.isClosed(), true);
});

test('a real click on Deny denies', async () => {
  const page = await loadApprovePage();

  page.elements.deny.emit('click', { isTrusted: true });
  await page.settle();

  assert.equal(page.decisions().length, 1);
  assert.equal(page.decisions()[0].approved, false);
});

test('a synthetic click decides nothing, on either button', async () => {
  const page = await loadApprovePage();

  page.elements.approve.emit('click', { isTrusted: false });
  page.elements.deny.emit('click', { isTrusted: false });
  await page.settle();

  assert.deepEqual(page.decisions(), [],
    'only a person can decide; a dispatched click is not one');
  assert.equal(page.isClosed(), false);
});

test('a synthetic Escape decides nothing', async () => {
  const page = await loadApprovePage();

  page.document.emit('keydown', { key: 'Escape', isTrusted: false });
  await page.settle();

  assert.deepEqual(page.decisions(), []);
});

// --------------------------------------------------------------------------
// The keyboard can only deny

test('Enter does not approve', async () => {
  // Enter is what a person presses without reading; approving has to be a
  // deliberate click.
  const page = await loadApprovePage();

  page.document.emit('keydown', { key: 'Enter', isTrusted: true });
  await page.settle();

  assert.deepEqual(page.decisions(), [], 'Enter must not decide anything');
  assert.equal(page.isClosed(), false);
});

test('Escape denies', async () => {
  const page = await loadApprovePage();

  page.document.emit('keydown', { key: 'Escape', isTrusted: true });
  await page.settle();

  assert.equal(page.decisions().length, 1);
  assert.equal(page.decisions()[0].approved, false);
});

// --------------------------------------------------------------------------
// No answer is a denial

test('closing the window is a denial', async () => {
  const page = await loadApprovePage();

  page.window.emit('beforeunload');
  await page.settle();

  assert.deepEqual(page.decisions(), [
    { target: 'approval', action: 'decide', requestId: REQUEST_ID,
      approved: false, closed: true },
  ]);
});

test('closing after a decision does not send a second one', async () => {
  // window.close() after an approval fires beforeunload too; that must not
  // be reported as the person closing the prompt unanswered.
  const page = await loadApprovePage();

  page.elements.approve.emit('click', { isTrusted: true });
  page.window.emit('beforeunload');
  await page.settle();

  assert.equal(page.decisions().length, 1);
  assert.equal(page.decisions()[0].approved, true);
});

test('the countdown denies when it runs out, and not before', async () => {
  const page = await loadApprovePage({
    details: { found: true, message: 'x', timeout: 3000 },
  });
  assert.match(page.elements.countdown.textContent, /3s/);

  await page.tick();
  await page.tick();
  assert.deepEqual(page.decisions(), [], 'no decision while time remains');

  await page.tick();
  assert.equal(page.decisions().length, 1);
  assert.equal(page.decisions()[0].approved, false, 'running out of time is a denial');
  assert.match(page.elements.countdown.textContent, /denied/i);
  assert.equal(page.isClosed(), true);
});

// --------------------------------------------------------------------------
// The agent's words are shown, never parsed

test('agent-supplied text is rendered as text, not markup', async () => {
  const markup = '<img src=x onerror="alert(1)"><b>Approve me</b>';
  const page = await loadApprovePage({
    details: { found: true, heading: markup, message: markup, detail: markup,
               protectedUrl: `https://bank.test/${markup}` },
  });
  const { heading, message, detail, site } = page.elements;

  assert.equal(heading.textContent, markup);
  assert.equal(message.textContent, markup);
  assert.equal(detail.textContent, markup);
  assert.equal(site.textContent, `Site: https://bank.test/${markup}`);
  for (const el of [heading, message, detail, site]) {
    assert.deepEqual(el.htmlWrites, [],
      `#${el.id} must never be written as HTML; the agent controls this text`);
  }
});

test('a request that is no longer pending cannot be approved', async () => {
  const page = await loadApprovePage({ details: { found: false } });

  assert.equal(page.elements.approve.disabled, true);
  assert.equal(page.elements.deny.textContent, 'Close');
});

test('when the details cannot be loaded the page says to deny', async () => {
  const page = await loadApprovePage({ detailsFail: true });

  assert.match(page.elements.message.textContent, /Deny/);
  assert.deepEqual(page.decisions(), []);
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
