/**
 * ClaudeCodeBrowser - Content Script
 * Runs in web pages to handle DOM interactions, clicks, typing, and element inspection
 *
 * MIT License
 * Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
 * Author: dre@ligandal.com
 */

(function() {
  'use strict';

  // Prevent multiple injections
  if (window.__claudeCodeBrowserInjected) return;
  window.__claudeCodeBrowserInjected = true;

  let highlightOverlay = null;
  let inspectorMode = false;

  // Feature flags reach this script verbatim: camelize_args() in server.py
  // renames keys without coercing their values and background.js forwards
  // content commands as they arrive, so a client that has not loaded the
  // current schema sends the JSON string "false". Read with bare truthiness
  // that is true, and read with !== false it is also true - so
  // browser_type {clear: "false", press_enter: "false", submit_form: "false"}
  // cleared the field and submitted the form, which is exactly what the
  // caller had declined.
  //
  // This is a copy of parseFlag() in background.js; the two must behave the
  // same, because the same option can be handled on either side.
  //
  // FEATURE flags only. The credential override stays a strict === true (see
  // passwordAllowed): a fail-closed security switch must not be opened by
  // anything that merely looks truthy.
  function parseFlag(value, fallback) {
    if (value === undefined || value === null || value === '') return fallback;
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    if (typeof value === 'string') {
      const text = value.trim().toLowerCase();
      if (['true', '1', 'yes', 'on'].includes(text)) return true;
      if (['false', '0', 'no', 'off'].includes(text)) return false;
    }
    return fallback;
  }

  // ============================================
  // Console Logging Infrastructure
  //
  // Network traffic is captured by background.js via webRequest.
  // ============================================

  // Logging state
  let loggingEnabled = false;
  let consoleLogs = [];
  const MAX_LOG_ENTRIES = 500;

  // Original console methods, saved unbound so restoreConsole() puts the
  // page's own functions back by identity. Calls go through .apply(console).
  const originalConsole = {
    log: console.log,
    warn: console.warn,
    error: console.error,
    info: console.info,
    debug: console.debug
  };

  // Console interceptor
  function interceptConsole() {
    ['log', 'warn', 'error', 'info', 'debug'].forEach(method => {
      console[method] = function(...args) {
        if (loggingEnabled) {
          const entry = {
            level: method,
            // Output from the extension's own scripts, not the page's.
            source: 'extension',
            timestamp: new Date().toISOString(),
            message: args.map(arg => {
              try {
                if (typeof arg === 'object') {
                  return JSON.stringify(arg, null, 2);
                }
                return String(arg);
              } catch (e) {
                return String(arg);
              }
            }).join(' '),
            url: window.location.href
          };
          consoleLogs.push(entry);
          if (consoleLogs.length > MAX_LOG_ENTRIES) {
            consoleLogs.shift();
          }
        }
        originalConsole[method].apply(console, args);
      };
    });
  }

  // Restore original console
  function restoreConsole() {
    Object.keys(originalConsole).forEach(method => {
      console[method] = originalConsole[method];
    });
  }

  // What console capture can and cannot see.
  //
  // A content script has its own `console`, separate from the page's. Wrapping
  // it captures output from the extension's own scripts - including anything
  // browser_execute_script prints - and never a single console.log the page
  // itself makes. Verified live: a page's console.log produced zero entries
  // while execute_script's produced one.
  //
  // Capturing the page's console for real would mean injecting a script into
  // the page's own world: back to mutating page globals, breakable by CSP,
  // and forgeable by the page. That is the thing the webRequest move was for
  // getting away from, so it is not done here.
  //
  // What IS reachable safely: page errors and unhandled rejections arrive as
  // DOM events on window, so a content-script listener sees them without
  // touching anything the page owns. That is the subset worth having for
  // debugging, and it is captured.
  //
  // Headless mode has no such boundary - the Playwright backend hooks
  // page.on('console') and captures everything.
  const interception = { console: false, pageErrors: false };

  function recordPageError(level, message) {
    if (!loggingEnabled) return;
    consoleLogs.push({
      level: level,
      source: 'page',
      timestamp: new Date().toISOString(),
      message: message,
      url: window.location.href
    });
    if (consoleLogs.length > MAX_LOG_ENTRIES) consoleLogs.shift();
  }

  const onPageError = (event) => {
    const where = event.filename
      ? ` (${event.filename}:${event.lineno || 0}:${event.colno || 0})`
      : '';
    recordPageError('error', `${event.message || 'Uncaught error'}${where}`);
  };

  const onPageRejection = (event) => {
    let reason;
    try {
      reason = event.reason instanceof Error
        ? `${event.reason.name}: ${event.reason.message}`
        : String(event.reason);
    } catch (e) {
      reason = '[unprintable rejection reason]';
    }
    recordPageError('error', `Unhandled promise rejection: ${reason}`);
  };

  function installInterception() {
    if (!interception.console) {
      try {
        interceptConsole();
        interception.console = true;
      } catch (e) {
        // console not writable in this sandbox — skip it
      }
    }
    if (!interception.pageErrors) {
      try {
        window.addEventListener('error', onPageError);
        window.addEventListener('unhandledrejection', onPageRejection);
        interception.pageErrors = true;
      } catch (e) {
        // no window error events available here
      }
    }
  }

  function removeInterception() {
    if (interception.console) {
      try {
        restoreConsole();
        interception.console = false;
      } catch (e) {
        // the wrapper stays, forwarding to originalConsole; logging stopped
      }
    }
    if (interception.pageErrors) {
      try {
        window.removeEventListener('error', onPageError);
        window.removeEventListener('unhandledrejection', onPageRejection);
      } catch (e) {
        // nothing more to do
      }
      interception.pageErrors = false;
    }
  }

  // Logging control functions
  function startLogging(options = {}) {
    installInterception();
    loggingEnabled = true;
    if (parseFlag(options.clearExisting, false)) {
      consoleLogs = [];
    }
    return {
      success: true,
      message: 'Console logging started',
      consoleLogsCount: consoleLogs.length
    };
  }

  function stopLogging() {
    loggingEnabled = false;
    removeInterception();
    return {
      success: true,
      message: 'Console logging stopped',
      consoleLogsCount: consoleLogs.length
    };
  }

  function getConsoleLogs(options = {}) {
    let logs = [...consoleLogs];

    // Filter by level if specified
    if (options.level) {
      logs = logs.filter(log => log.level === options.level);
    }

    // Filter by search term
    if (options.search) {
      const searchLower = options.search.toLowerCase();
      logs = logs.filter(log => log.message.toLowerCase().includes(searchLower));
    }

    // Limit results
    const limit = options.limit || 100;
    if (logs.length > limit) {
      logs = logs.slice(-limit);
    }

    return {
      success: true,
      logs: logs,
      totalCount: consoleLogs.length,
      returnedCount: logs.length,
      loggingEnabled: loggingEnabled,
      // Be precise about what an empty array means. Reporting a single
      // "interceptionAvailable: true" invited the conclusion that the page
      // had logged nothing, when the page's console was never visible.
      capturesPageConsole: false,
      capturesPageErrors: interception.pageErrors,
      capturesExtensionConsole: interception.console,
      note: 'A content script cannot see the page\'s own console. Captured ' +
            'here: uncaught page errors and unhandled rejections ' +
            '(source: "page"), and output from the extension\'s own scripts ' +
            'including browser_execute_script (source: "extension"). An empty ' +
            'result does not mean the page logged nothing. Headless mode ' +
            'captures the page console in full.'
    };
  }

  function clearLogs(options = {}) {
    if (parseFlag(options.console, true)) {
      consoleLogs = [];
    }
    return {
      success: true,
      message: 'Console logs cleared'
    };
  }

  // Message listener
  browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
    handleMessage(message)
      .then(sendResponse)
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true; // Keep channel open for async
  });

  async function handleMessage(message) {
    switch (message.action) {
      case "click":
        return performClick(message);
      case "type":
        return performType(message);
      case "scroll":
        return performScroll(message);
      case "getPageInfo":
        return getPageInfo(message);
      case "getElements":
        return getElements(message);
      case "highlight":
        return highlightElement(message);
      case "waitForElement":
        return waitForElement(message);
      case "waitForChange":
        return waitForChange(message);
      case "observeElement":
        return observeElement(message);
      case "stopObserving":
        return stopObserving(message);
      case "scrollAndCapture":
        return scrollAndCapture(message);
      case "clickAndWait":
        return clickAndWait(message);
      case "captureFullPage":
        return captureFullPage(message);
      case "inspectElement":
        return inspectElement(message);
      case "getValue":
        return getValue(message);
      case "setValue":
        return setValue(message);
      case "getAttribute":
        return getAttribute(message);
      case "focus":
        return focusElement(message);
      case "hover":
        return hoverElement(message);
      case "selectOption":
        return selectOption(message);
      case "getComputedStyles":
        return getComputedStyles(message);
      case "pressKey":
        return pressKey(message);
      case "getText":
        return getText(message);
      case "requestApproval":
        return requestApproval(message);
      case "solveCaptcha":
        return solveCaptcha(message);
      case "getBoundingRect":
        return getBoundingRect(message);
      // Console and network logging actions
      case "startLogging":
        return startLogging(message);
      case "stopLogging":
        return stopLogging();
      case "getConsoleLogs":
        return getConsoleLogs(message);
      case "clearLogs":
        return clearLogs(message);
      default:
        throw new Error(`Unknown action: ${message.action}`);
    }
  }

  // Credential guard: credentials never pass through the AI — neither written
  // into a password field nor read back out of one — unless the safety config
  // explicitly allows it. They belong in the browser's own password manager.
  // autocomplete is a space-separated token list and is case-insensitive, so
  // "Current-Password", "current-password " and the spec-legal
  // "section-login current-password" all have to match. Beyond passwords,
  // one-time codes and card fields are credentials too: they are not
  // type=password, so nothing else in here would have protected them.
  const CREDENTIAL_AUTOCOMPLETE_TOKENS = new Set([
    'current-password', 'new-password', 'one-time-code',
    'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year'
  ]);

  // A field's name or id is the third signal, and on real pages often the
  // only one: <input type="text" name="passwd"> is a password field that
  // says type="text", and a contenteditable <div id="otp-code"> is a
  // credential with no type at all.
  //
  // This list must stay at least as strict as SECRET_KEY_RE in
  // background.js, which scrubs the same names out of captured HTML and
  // request bodies and whose comment calls them "exactly the thing the
  // DOM-level guard masks". Change one, change the other. The extras here
  // (pwd, ssn) are names background.js misses; a name the DOM guard misses
  // hands a credential to the agent in clear, which is worse than masking a
  // field that happens to be called "author".
  const CREDENTIAL_NAME_RE =
    /(pass(word|wd)?|pwd|secret|token|otp|one[-_]?time[-_]?code|auth|credential|api[-_]?key|private[-_]?key|session|cvv|card[-_]?number|ssn)/i;

  function attributeOf(element, name) {
    if (!element || typeof element.getAttribute !== 'function') return null;
    return element.getAttribute(name);
  }

  function isPasswordField(element) {
    if (!element) return false;
    // Not restricted to <input>: Shoelace, Ionic and Vaadin wrap a real
    // input in a shadow root, so <sl-input type="password"> is the only
    // element an agent can target, and requiring tagName === 'INPUT' let
    // those through in clear.
    if (element.type === 'password') return true;
    if ((attributeOf(element, 'type') || '').toLowerCase() === 'password') return true;

    const autocomplete = attributeOf(element, 'autocomplete');
    if (autocomplete && autocomplete
          .toLowerCase()
          .split(/\s+/)
          .some(token => CREDENTIAL_AUTOCOMPLETE_TOKENS.has(token))) {
      return true;
    }

    // The name/id rule is only for elements that hold a value somebody
    // entered. Applied to everything, it would mask the text of any
    // <div id="user-session-banner"> on the page.
    if (!holdsEnteredValue(element)) return false;

    // name can be a form path like user[password], which still names a
    // credential, so this is a substring match rather than an equality test.
    const name = element.name || attributeOf(element, 'name') || '';
    const id = element.id || attributeOf(element, 'id') || '';
    return CREDENTIAL_NAME_RE.test(name) || CREDENTIAL_NAME_RE.test(id);
  }

  function holdsEnteredValue(element) {
    const tag = element.tagName || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (element.isContentEditable === true) return true;
    // A custom element - its tag name must contain a hyphen - is how a
    // component library ships a field: <sl-input>, <ion-input>,
    // <vaadin-password-field>.
    return tag.includes('-');
  }

  // Hidden inputs routinely carry CSRF tokens, session ids and order ids.
  // They are never something the agent needs the value of.
  function isConcealedValueField(element) {
    return isPasswordField(element) ||
      (element && element.tagName === 'INPUT' && element.type === 'hidden');
  }

  // The single place that decides what an element's value looks like to the
  // agent. Every reader goes through this so a new reader cannot reintroduce
  // the getPageInfo leak.
  function safeElementValue(element, limit, options) {
    if (element && isPasswordField(element) && !passwordAllowed(options)) {
      return element.value ? '***' : null;
    }
    if (element && isConcealedValueField(element) && !passwordAllowed(options)) {
      return element.value ? '***' : null;
    }
    // .value is not always a string: it is an IDL number on <li>, <progress>
    // and <meter> (0 for an <li> outside an <ol>) and on custom elements
    // like <md-slider>. ?. only short-circuits null and undefined, so
    // (0).substring threw a TypeError here - and performClick builds this
    // info *after* dispatching the click, so the caller got an error for an
    // action that had already happened and retried it.
    const raw = element?.value;
    if (raw === undefined || raw === null || raw === '') return null;
    return String(raw).substring(0, limit);
  }

  // textContent is the value of a contenteditable field, so a credential
  // held in one leaks through every result that carries element text.
  function safeElementText(element, limit, options) {
    if (element && isConcealedValueField(element) && !passwordAllowed(options)) {
      return element.textContent ? '***' : null;
    }
    return element?.textContent?.trim().substring(0, limit) || null;
  }

  // The server sets allow_password, but camelize_args() in server.py rewrites
  // it to allowPassword before dispatch, so that is what actually arrives here.
  // Both are accepted so a caller that reaches the extension without passing
  // through that conversion still gets the guard honoured.
  function passwordAllowed(options) {
    return options?.allow_password === true || options?.allowPassword === true;
  }

  function assertNotPasswordField(element, options) {
    if (isPasswordField(element) && !passwordAllowed(options)) {
      throw new Error(
        'Refused: target is a password field. Use the browser’s own ' +
        'password manager (autofill) for credentials, or set ' +
        '"allow_password_typing": true in ~/.claudecodebrowser/safety.json ' +
        'if you really want automated password entry.'
      );
    }
  }

  // Both prompts are rendered inside a closed shadow root on a host element
  // whose own styles are set with !important. The page can still cover the
  // viewport, so this is not a trustworthy channel in the strong sense -- the
  // only trustworthy place for a decision is browser chrome -- but it stops
  // the page reading the prompt, restyling it away, or finding its buttons.
  function createPromptRoot(hostId) {
    const existing = document.getElementById(hostId);
    if (existing) existing.remove();

    const host = document.createElement('div');
    host.id = hostId;
    host.setAttribute('style', [
      'all: initial !important',
      'position: fixed !important',
      'top: 0 !important',
      'left: 0 !important',
      'right: 0 !important',
      'z-index: 2147483647 !important',
      'display: block !important',
      'visibility: visible !important',
      'opacity: 1 !important',
      'pointer-events: auto !important',
      'transform: none !important'
    ].join(';'));

    // A closed root: page script cannot reach into it via host.shadowRoot.
    const root = host.attachShadow ? host.attachShadow({ mode: 'closed' }) : host;
    (document.body || document.documentElement).appendChild(host);
    return { host, root };
  }

  // A decision is only a decision if a person made it. Synthetic clicks from
  // page script carry isTrusted === false, which is the difference between a
  // human approving and the site approving on its own behalf.
  function onHumanClick(element, handler) {
    element.addEventListener('click', (event) => {
      if (!event.isTrusted) {
        console.warn('[ClaudeCodeBrowser] ignoring untrusted click on prompt');
        return;
      }
      handler();
    });
  }

  // Human approval banner: Approve/Deny prompt rendered on the page,
  // resolved by a real click from the person at the browser
  function requestApproval(options) {
    return new Promise((resolve) => {
      const { host, root } = createPromptRoot('__ccb_approval_host');

      const banner = document.createElement('div');
      banner.style.cssText = [
        'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
        'background:#1a1a2e', 'color:#fff', 'padding:14px 20px',
        'font:14px/1.5 system-ui,sans-serif', 'display:flex',
        'align-items:center', 'gap:16px', 'box-shadow:0 2px 12px rgba(0,0,0,.4)',
        'border-bottom:3px solid #e94560'
      ].join(';');

      const textWrap = document.createElement('div');
      textWrap.style.cssText = 'flex:1;min-width:0';
      const title = document.createElement('div');
      title.style.cssText = 'font-weight:600';
      title.textContent = '⚠️ Claude requests approval: ' + (options.message || 'Perform an action');
      textWrap.appendChild(title);
      if (options.detail) {
        const detail = document.createElement('div');
        detail.style.cssText = 'font-size:12px;opacity:.75;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        detail.textContent = options.detail;
        textWrap.appendChild(detail);
      }

      function makeButton(label, bg) {
        const b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'padding:8px 18px;border:none;border-radius:6px;cursor:pointer;' +
          'font:600 13px system-ui,sans-serif;color:#fff;background:' + bg;
        return b;
      }
      const approveBtn = makeButton('Approve', '#16a34a');
      const denyBtn = makeButton('Deny', '#dc2626');

      banner.appendChild(textWrap);
      banner.appendChild(approveBtn);
      banner.appendChild(denyBtn);
      root.appendChild(banner);

      const timeoutMs = options.timeout || 60000;
      let settled = false;
      function finish(approved, timedOut) {
        if (settled) return;
        settled = true;
        host.remove();
        resolve({
          success: true,
          approved: approved,
          timedOut: !!timedOut,
          decidedAt: new Date().toISOString()
        });
      }

      onHumanClick(approveBtn, () => finish(true, false));
      onHumanClick(denyBtn, () => finish(false, false));
      setTimeout(() => finish(false, true), timeoutMs);
    });
  }

  // Captcha detection. Returns the widgets found and, where the challenge
  // exposes a response token, whether it has already been solved.
  function detectCaptcha() {
    const widgets = [];

    function hasResponseToken(name) {
      const el = document.querySelector(`textarea[name="${name}"], input[name="${name}"]`);
      return !!(el && el.value && el.value.length > 0);
    }

    // Google reCAPTCHA (v2 checkbox / invisible / v3)
    if (document.querySelector('.g-recaptcha, iframe[src*="recaptcha"], #g-recaptcha-response')) {
      widgets.push({
        type: 'recaptcha',
        solved: hasResponseToken('g-recaptcha-response') ||
                !!document.querySelector('.recaptcha-checkbox-checked')
      });
    }
    // hCaptcha
    if (document.querySelector('.h-captcha, iframe[src*="hcaptcha"], textarea[name="h-captcha-response"]')) {
      widgets.push({ type: 'hcaptcha', solved: hasResponseToken('h-captcha-response') });
    }
    // Cloudflare Turnstile
    if (document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]')) {
      widgets.push({ type: 'turnstile', solved: hasResponseToken('cf-turnstile-response') });
    }
    // Generic image/text captcha (no reliable machine-readable completion)
    if (widgets.length === 0 &&
        document.querySelector('img[src*="captcha" i], input[name*="captcha" i], [id*="captcha" i], [class*="captcha" i]')) {
      widgets.push({ type: 'generic', solved: null });
    }

    return { present: widgets.length > 0, widgets };
  }

  // Detect a captcha and hand it to the human to solve, then continue.
  // NOTE: this does not auto-solve or use any solver service — the person at
  // the browser completes the challenge; we detect completion or a Done click.
  function solveCaptcha(options = {}) {
    const detection = detectCaptcha();
    if (parseFlag(options.detectOnly, false)) {
      return { success: true, ...detection };
    }
    if (!detection.present) {
      return { success: true, present: false, message: 'No captcha detected on the page.' };
    }
    if (detection.widgets.every(w => w.solved === true)) {
      return { success: true, present: true, solved: true, humanVerified: false,
               message: 'Captcha reports itself already solved. The response ' +
                        'token is page-writable, so this is not proof a human ' +
                        'solved it.',
               widgets: detection.widgets };
    }

    return new Promise((resolve) => {
      const { host, root } = createPromptRoot('__ccb_captcha_host');

      const types = detection.widgets.map(w => w.type).join(', ');
      const banner = document.createElement('div');
      banner.style.cssText = [
        'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
        'background:#0f3460', 'color:#fff', 'padding:14px 20px',
        'font:14px/1.5 system-ui,sans-serif', 'display:flex',
        'align-items:center', 'gap:16px', 'box-shadow:0 2px 12px rgba(0,0,0,.4)',
        'border-bottom:3px solid #ffd166'
      ].join(';');

      const textWrap = document.createElement('div');
      textWrap.style.cssText = 'flex:1;min-width:0';
      const title = document.createElement('div');
      title.style.cssText = 'font-weight:600';
      title.textContent = '🧩 Please solve the captcha (' + types + '), then it continues automatically.';
      const detail = document.createElement('div');
      detail.style.cssText = 'font-size:12px;opacity:.8';
      detail.textContent = 'Claude paused and is waiting for you. Click Done if it does not continue on its own.';
      textWrap.appendChild(title);
      textWrap.appendChild(detail);

      const doneBtn = document.createElement('button');
      doneBtn.textContent = 'Done';
      doneBtn.style.cssText = 'padding:8px 18px;border:none;border-radius:6px;cursor:pointer;' +
        'font:600 13px system-ui,sans-serif;color:#0f3460;background:#ffd166';
      const cancelBtn = document.createElement('button');
      cancelBtn.textContent = 'Cancel';
      cancelBtn.style.cssText = 'padding:8px 14px;border:1px solid #ffffff55;border-radius:6px;' +
        'cursor:pointer;font:600 13px system-ui,sans-serif;color:#fff;background:transparent';

      banner.appendChild(textWrap);
      banner.appendChild(doneBtn);
      banner.appendChild(cancelBtn);
      root.appendChild(banner);

      const timeoutMs = options.timeout || 180000;
      const start = Date.now();
      let settled = false;

      function finish(payload) {
        if (settled) return;
        settled = true;
        clearInterval(poll);
        host.remove();
        resolve({ success: true, present: true, elapsedMs: Date.now() - start, ...payload });
      }

      // Auto-detect completion for token-based widgets
      const poll = setInterval(() => {
        const now = detectCaptcha();
        const tokened = now.widgets.filter(w => w.solved !== null);
        if (tokened.length && tokened.every(w => w.solved === true)) {
          // The response token is a page-writable DOM value, so this is
          // evidence the challenge completed, not proof a human acted.
          finish({ solved: true, resolvedBy: 'token', humanVerified: false,
                   widgets: now.widgets });
        } else if (Date.now() - start > timeoutMs) {
          finish({ solved: false, timedOut: true, widgets: now.widgets });
        }
      }, 1000);

      onHumanClick(doneBtn, () =>
        finish({ solved: true, resolvedBy: 'human', widgets: detectCaptcha().widgets }));
      onHumanClick(cancelBtn, () =>
        finish({ solved: false, cancelled: true }));
    });
  }

  // Every tool hands its selector back as the handle for the next call, so a
  // selector that cannot be parsed again breaks the call after the one that
  // worked. Real ids are full of characters CSS reads as syntax: Headless UI
  // emits "headlessui-menu-item-:r1:", Rails and MUI emit "user.email", and
  // querySelector('#user.email') means "#user with class email", not that id.
  // Same for Tailwind's "md:flex" class names.
  //
  // CSS.escape does this in a browser. It is written out here so the tests
  // run the same code the extension does, and so this works in any context
  // the script is injected into.
  function cssIdentifier(value) {
    const escaped = String(value)
      // What CSS.escape does: a NUL becomes a replacement character, every
      // ASCII character that is not an identifier character is backslashed,
      // and non-ASCII characters are already legal in an identifier.
      .replace(/\0/g, '\uFFFD')
      .replace(/[^a-zA-Z0-9_\u0080-\uFFFF-]/g, (char) => `\\${char}`)
      // A leading digit, or a hyphen followed by one, cannot start an
      // identifier, so it goes in as a hex escape.
      .replace(/^(-?)([0-9])/, (whole, hyphen, digit) => `${hyphen}\\3${digit} `);
    return escaped;
  }

  // A caller-supplied needle goes into an attribute selector as a quoted
  // string: a value containing a double quote otherwise closes the string and
  // the rest of it is read as selector syntax, so the page can decide which
  // element the agent "found by name" actually is.
  function cssString(value) {
    const escaped = String(value)
      .replace(/[\\"]/g, '\\$&')
      // A raw newline is not allowed inside a CSS string.
      .replace(/[\n\r\f]/g, (char) => `\\${char.charCodeAt(0).toString(16)} `);
    return `"${escaped}"`;
  }

  // XPath 1.0 has no escape syntax, so a string containing both quote
  // characters has to be built with concat().
  function xpathLiteral(value) {
    const text = String(value);
    if (!text.includes('"')) return `"${text}"`;
    if (!text.includes("'")) return `'${text}'`;
    return 'concat(' + text.split('"')
      .map(part => `"${part}"`)
      .join(', \'"\', ') + ')';
  }

  // Find element by various selectors
  function findElement(options) {
    let element = null;

    if (options.selector) {
      element = document.querySelector(options.selector);
    } else if (options.xpath) {
      const result = document.evaluate(
        options.xpath,
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null
      );
      element = result.singleNodeValue;
    } else if (options.text) {
      // Find by text content. The needle is quoted with concat() so a label
      // containing a quote cannot close the literal and graft on a predicate
      // of its own -- a page could otherwise choose which element the agent
      // "clicked by text" actually hits.
      const xpath = `//*[contains(text(), ${xpathLiteral(options.text)})]`;
      const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      element = result.singleNodeValue;
    } else if (options.x !== undefined && options.y !== undefined) {
      element = document.elementFromPoint(options.x, options.y);
    } else if (options.id) {
      element = document.getElementById(options.id);
    } else if (options.name) {
      element = document.querySelector(`[name=${cssString(options.name)}]`);
    } else if (options.ariaLabel) {
      element = document.querySelector(`[aria-label=${cssString(options.ariaLabel)}]`);
    } else if (options.placeholder) {
      element = document.querySelector(`[placeholder=${cssString(options.placeholder)}]`);
    } else if (options.role) {
      element = document.querySelector(`[role=${cssString(options.role)}]`);
    }

    return element;
  }

  // Click functionality
  async function performClick(options) {
    const element = findElement(options);

    if (!element) {
      throw new Error(`Element not found with options: ${JSON.stringify(options)}`);
    }

    let defaultPrevented = null;

    // Scroll element into view
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    await sleep(100);

    // Get element position
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;

    // Create and dispatch events
    if (parseFlag(options.rightClick, false)) {
      const contextEvent = new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      });
      element.dispatchEvent(contextEvent);
    } else if (parseFlag(options.doubleClick, false)) {
      const dblClickEvent = new MouseEvent('dblclick', {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      });
      element.dispatchEvent(dblClickEvent);
    } else {
      // Regular click
      const mouseDown = new MouseEvent('mousedown', {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      });
      const mouseUp = new MouseEvent('mouseup', {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      });
      const click = new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      });

      element.dispatchEvent(mouseDown);
      await sleep(50);
      element.dispatchEvent(mouseUp);
      // One click only. Dispatching the synthetic event and then calling
      // element.click() delivered two click events per browser_click, so any
      // non-idempotent handler ran twice -- two items added, two submits.
      defaultPrevented = !element.dispatchEvent(click);
    }

    return {
      clicked: true,
      // "clicked" means the events were dispatched to a matching element, not
      // that the UI responded. These let the caller tell the difference.
      defaultPrevented: defaultPrevented,
      disabled: element.disabled === true,
      visible: isVisible(element),
      element: getElementInfo(element),
      position: { x, y }
    };
  }

  // Type functionality
  async function performType(options) {
    let element = findElement(options);

    if (!element && !parseFlag(options.focusFirst, true)) {
      // Type into currently focused element
      element = document.activeElement;
    }

    if (!element) {
      throw new Error(`Element not found with options: ${JSON.stringify(options)}`);
    }

    assertNotPasswordField(element, options);

    // Only a text field or a contenteditable element takes inserted text.
    // Anything else swallows every character: the key events dispatch, the
    // value assignment below is skipped, and the old code still returned
    // typed: true. document.activeElement is <body> whenever nothing is
    // focused, so focus_first: false hit this on any page the agent had not
    // clicked into first - the text went nowhere and the result said it had
    // been typed.
    const editable = element.tagName === 'INPUT' || element.tagName === 'TEXTAREA'
      || element.isContentEditable === true;
    if (!editable) {
      throw new Error(
        `Refused: <${element.tagName.toLowerCase()}> cannot receive typed ` +
        'text (not an input, textarea or contenteditable element). ' +
        'Nothing was typed. Name the field with "selector", or click into it ' +
        'first if you are relying on focus_first: false.'
      );
    }

    // Focus the element
    element.focus();
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    await sleep(100);

    const text = options.text || '';

    const clear = parseFlag(options.clear, false);
    if (clear) {
      // Clear existing content
      if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
        element.value = '';
      } else if (element.isContentEditable) {
        element.textContent = '';
      }
      element.dispatchEvent(new Event('input', { bubbles: true }));
    }

    if (parseFlag(options.instant, false)) {
      // Instant input (no typing simulation)
      if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
        element.value = clear ? text : element.value + text;
      } else if (element.isContentEditable) {
        element.textContent = clear ? text : element.textContent + text;
      }
      element.dispatchEvent(new Event('input', { bubbles: true }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // Simulate typing character by character
      for (const char of text) {
        const keyDown = new KeyboardEvent('keydown', {
          key: char,
          code: `Key${char.toUpperCase()}`,
          bubbles: true
        });
        const keyPress = new KeyboardEvent('keypress', {
          key: char,
          code: `Key${char.toUpperCase()}`,
          bubbles: true
        });
        const keyUp = new KeyboardEvent('keyup', {
          key: char,
          code: `Key${char.toUpperCase()}`,
          bubbles: true
        });

        element.dispatchEvent(keyDown);
        element.dispatchEvent(keyPress);

        // Actually insert the character
        if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
          element.value += char;
        } else if (element.isContentEditable) {
          document.execCommand('insertText', false, char);
        }

        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(keyUp);

        await sleep(options.delay || 50);
      }
      element.dispatchEvent(new Event('change', { bubbles: true }));
    }

    // Handle Enter key if specified
    if (parseFlag(options.pressEnter, false)) {
      const enterDown = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true });
      const enterUp = new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true });
      element.dispatchEvent(enterDown);
      element.dispatchEvent(enterUp);

      // Submit form if applicable
      const form = element.closest('form');
      if (form && parseFlag(options.submitForm, true)) {
        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
    }

    return {
      typed: true,
      element: getElementInfo(element),
      text: text
    };
  }

  // Scroll functionality
  async function performScroll(options) {
    let target = window;
    let element = null;

    if (options.selector || options.xpath || options.id) {
      element = findElement(options);
      if (!element) {
        // Silently scrolling the window instead of the element the caller
        // named, and reporting success, is worse than failing.
        throw new Error(
          `Scroll container not found: ${options.selector || options.xpath || options.id}`);
      }
      target = element;
    }

    if (options.toElement) {
      const targetElement = findElement({ selector: options.toElement });
      if (!targetElement) {
        throw new Error(`Scroll target not found: ${options.toElement}`);
      }
      if (targetElement) {
        targetElement.scrollIntoView({
          behavior: parseFlag(options.smooth, true) ? 'smooth' : 'auto',
          block: options.block || 'center'
        });
        await sleep(500);
        return { scrolled: true, element: getElementInfo(targetElement) };
      }
    }

    if (options.direction) {
      const amount = options.amount || 300;
      let scrollX = 0, scrollY = 0;

      switch (options.direction) {
        case 'up': scrollY = -amount; break;
        case 'down': scrollY = amount; break;
        case 'left': scrollX = -amount; break;
        case 'right': scrollX = amount; break;
        case 'top':
          if (element) element.scrollTop = 0;
          else window.scrollTo({ top: 0, behavior: 'smooth' });
          return { scrolled: true, position: { x: 0, y: 0 } };
        case 'bottom':
          if (element) element.scrollTop = element.scrollHeight;
          else window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
          return { scrolled: true, position: 'bottom' };
      }

      if (element) {
        element.scrollBy({ left: scrollX, top: scrollY, behavior: 'smooth' });
      } else {
        window.scrollBy({ left: scrollX, top: scrollY, behavior: 'smooth' });
      }
    } else if (options.x !== undefined || options.y !== undefined) {
      const scrollOptions = {
        left: options.x || 0,
        top: options.y || 0,
        behavior: parseFlag(options.smooth, true) ? 'smooth' : 'auto'
      };

      if (element) {
        element.scrollTo(scrollOptions);
      } else {
        window.scrollTo(scrollOptions);
      }
    }

    await sleep(300);

    return {
      scrolled: true,
      position: {
        x: element ? element.scrollLeft : window.scrollX,
        y: element ? element.scrollTop : window.scrollY
      }
    };
  }

  // Get page information
  function getPageInfo(options = {}) {
    const interactiveElements = [];

    // Find all interactive elements
    const selectors = [
      'a[href]', 'button', 'input', 'textarea', 'select',
      '[onclick]', '[role="button"]', '[role="link"]',
      '[tabindex]:not([tabindex="-1"])'
    ];

    // The cap is on how many elements are COLLECTED, not how many are
    // examined. Counting matches instead meant 100 zero-size matches (hidden
    // inputs, offscreen [tabindex] - normal on a real page) used up the whole
    // budget and the visible checkout button that followed them was reported
    // in the count but never returned.
    const MAX_INTERACTIVE_ELEMENTS = 100;
    const matches = Array.from(document.querySelectorAll(selectors.join(', ')));
    let stoppedAtCap = false;

    for (const el of matches) {
      if (interactiveElements.length >= MAX_INTERACTIVE_ELEMENTS) {
        stoppedAtCap = true;
        break;
      }
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      interactiveElements.push({
        tag: el.tagName.toLowerCase(),
        type: el.type || null,
        id: el.id || null,
        name: el.name || null,
        text: safeElementText(el, 100, options),
        href: el.href || null,
        value: safeElementValue(el, 100, options),
        placeholder: el.placeholder || null,
        ariaLabel: el.getAttribute('aria-label'),
        position: {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
          width: rect.width,
          height: rect.height
        },
        visible: isVisible(el),
        selector: generateSelector(el)
      });
    }

    const allInteractive = matches.length;
    return {
      url: window.location.href,
      title: document.title,
      interactiveElementCount: allInteractive,
      // How many of the matches are actually in the list below. The count
      // above includes zero-size elements, which are never returned, so the
      // two differ on most pages.
      interactiveElementsReturned: interactiveElements.length,
      interactiveElementsTruncated: stoppedAtCap,
      documentHeight: document.documentElement.scrollHeight,
      documentWidth: document.documentElement.scrollWidth,
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      scrollPosition: { x: window.scrollX, y: window.scrollY },
      interactiveElements: interactiveElements,
      forms: Array.from(document.forms).map(form => ({
        id: form.id,
        name: form.name,
        action: form.action,
        method: form.method,
        fields: Array.from(form.elements).slice(0, 20).map(el => ({
          tag: el.tagName.toLowerCase(),
          type: el.type,
          name: el.name,
          id: el.id,
          placeholder: el.placeholder,
          required: el.required,
          value: safeElementValue(el, 50, options)
        }))
      })),
      headings: Array.from(document.querySelectorAll('h1, h2, h3')).slice(0, 20).map(h => ({
        level: parseInt(h.tagName[1]),
        text: h.textContent?.trim().substring(0, 100)
      }))
    };
  }

  // Get elements by selector
  function getElements(options) {
    const elements = [];
    const selector = options.selector || '*';
    const limit = options.limit || 50;

    document.querySelectorAll(selector).forEach((el, index) => {
      if (index < limit) {
        elements.push(getElementInfo(el));
      }
    });

    return { elements, count: document.querySelectorAll(selector).length };
  }

  // Highlight element
  function highlightElement(options) {
    removeHighlight();

    const element = findElement(options);
    if (!element) {
      throw new Error('Element not found');
    }

    const rect = element.getBoundingClientRect();

    highlightOverlay = document.createElement('div');
    highlightOverlay.className = 'claude-highlight-overlay';
    highlightOverlay.style.cssText = `
      position: fixed;
      left: ${rect.left}px;
      top: ${rect.top}px;
      width: ${rect.width}px;
      height: ${rect.height}px;
      border: 3px solid #7c3aed;
      background: rgba(124, 58, 237, 0.1);
      pointer-events: none;
      z-index: 999999;
      box-shadow: 0 0 10px rgba(124, 58, 237, 0.5);
      transition: all 0.3s ease;
    `;

    // Add label
    const label = document.createElement('div');
    label.style.cssText = `
      position: absolute;
      top: -25px;
      left: 0;
      background: #7c3aed;
      color: white;
      padding: 2px 8px;
      font-size: 12px;
      font-family: monospace;
      border-radius: 3px;
      white-space: nowrap;
    `;
    label.textContent = options.label || generateSelector(element);
    highlightOverlay.appendChild(label);

    document.body.appendChild(highlightOverlay);

    // Auto-remove after duration
    if (options.duration !== 0) {
      setTimeout(removeHighlight, options.duration || 3000);
    }

    return { highlighted: true, element: getElementInfo(element) };
  }

  function removeHighlight() {
    if (highlightOverlay) {
      highlightOverlay.remove();
      highlightOverlay = null;
    }
  }

  // Wait for element
  async function waitForElement(options) {
    const timeout = options.timeout || 10000;
    const interval = options.interval || 100;
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      const element = findElement(options);
      if (element) {
        if (!options.visible || isVisible(element)) {
          return { found: true, element: getElementInfo(element) };
        }
      }
      await sleep(interval);
    }

    throw new Error(`Element not found within ${timeout}ms`);
  }

  // Active mutation observers
  const activeObservers = new Map();

  // Wait for DOM changes (useful after clicking dynamic elements)
  async function waitForChange(options) {
    const timeout = options.timeout || 10000;
    const targetSelector = options.selector || 'body';
    const target = document.querySelector(targetSelector) || document.body;

    return new Promise((resolve, reject) => {
      let resolved = false;
      const changes = [];

      const observer = new MutationObserver((mutations) => {
        if (resolved) return;

        for (const mutation of mutations) {
          const change = {
            type: mutation.type,
            target: mutation.target.tagName?.toLowerCase(),
            addedNodes: mutation.addedNodes.length,
            removedNodes: mutation.removedNodes.length
          };

          // Filter by change type if specified
          if (options.changeType) {
            if (options.changeType === 'childList' && mutation.type !== 'childList') continue;
            if (options.changeType === 'attributes' && mutation.type !== 'attributes') continue;
            if (options.changeType === 'text' && mutation.type !== 'characterData') continue;
          }

          changes.push(change);

          // Check if we should resolve now
          if (!parseFlag(options.waitForAll, false)) {
            resolved = true;
            observer.disconnect();
            resolve({
              changed: true,
              changes: changes,
              waitedMs: Date.now() - startTime
            });
            return;
          }
        }
      });

      const startTime = Date.now();

      observer.observe(target, {
        childList: true,
        subtree: parseFlag(options.subtree, true),
        attributes: parseFlag(options.attributes, true),
        characterData: parseFlag(options.characterData, false),
        attributeOldValue: parseFlag(options.attributeOldValue, false)
      });

      // Timeout
      setTimeout(() => {
        if (!resolved) {
          resolved = true;
          observer.disconnect();
          if (changes.length > 0) {
            resolve({ changed: true, changes, waitedMs: timeout });
          } else {
            resolve({ changed: false, changes: [], waitedMs: timeout, timedOut: true });
          }
        }
      }, timeout);
    });
  }

  // Set up continuous observation of an element for changes
  function observeElement(options) {
    const targetSelector = options.selector;
    const target = document.querySelector(targetSelector);

    if (!target) {
      throw new Error(`Element not found: ${targetSelector}`);
    }

    // Date.now() alone collides for two calls in the same millisecond, which
    // silently disconnected the first observer and lost its changes.
    const observerId = options.observerId ||
      `obs_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

    // Stop existing observer with same ID. observer_id is a documented
    // parameter, so reusing one has to replace the observer; the map holds a
    // record, not the observer, and calling .disconnect() on the record threw
    // instead. The old expiry timer has to go with it: it looks the id up
    // again when it fires, so it would disconnect the replacement and mark it
    // expired.
    const previous = activeObservers.get(observerId);
    if (previous) {
      previous.observer.disconnect();
      if (previous.expiry) clearTimeout(previous.expiry);
      activeObservers.delete(observerId);
    }

    const changes = [];

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        changes.push({
          type: mutation.type,
          timestamp: Date.now(),
          target: generateSelector(mutation.target),
          addedNodes: Array.from(mutation.addedNodes).map(n => n.tagName?.toLowerCase() || 'text').filter(Boolean),
          removedNodes: Array.from(mutation.removedNodes).map(n => n.tagName?.toLowerCase() || 'text').filter(Boolean),
          attributeName: mutation.attributeName,
          oldValue: mutation.oldValue
        });

        // Keep only last 100 changes
        if (changes.length > 100) changes.shift();
      }
    });

    observer.observe(target, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
      attributeOldValue: true
    });

    // An observer watching childList + subtree + attributes + characterData
    // is a measurable drag on an animation-heavy or virtualised page, and the
    // handle was dropped only by an explicit stopObserving - so an agent that
    // forgot left it running for the document's lifetime. Auto-expire it, and
    // say when it will go.
    const maxLifetimeMs = options.maxLifetimeMs || 300000;
    const expiry = setTimeout(() => {
      const record = activeObservers.get(observerId);
      if (!record) return;
      record.observer.disconnect();
      record.expired = true;
      // Keep the accumulated changes retrievable; only stop watching.
      activeObservers.set(observerId, record);
    }, maxLifetimeMs);

    activeObservers.set(observerId, {
      observer, changes, target: targetSelector, expiry
    });

    return {
      observing: true,
      observerId,
      target: targetSelector,
      expiresInMs: maxLifetimeMs
    };
  }

  // Stop observing and get accumulated changes
  function stopObserving(options) {
    const observerId = options.observerId;

    if (!activeObservers.has(observerId)) {
      return { found: false, observerId };
    }

    const { observer, changes, target, expiry, expired } =
      activeObservers.get(observerId);
    observer.disconnect();
    if (expiry) clearTimeout(expiry);
    activeObservers.delete(observerId);

    return {
      stopped: true,
      observerId,
      target,
      changes,
      totalChanges: changes.length,
      // If it expired, the change list stops where the observer stopped.
      expired: expired === true
    };
  }

  // Scroll through page and collect viewport snapshots info
  async function scrollAndCapture(options) {
    const scrollStep = options.scrollStep || window.innerHeight * 0.8;
    const delay = options.delay || 500;
    const maxScrolls = options.maxScrolls || 20;

    const snapshots = [];
    const originalScroll = window.scrollY;
    let scrollCount = 0;

    // Start from top
    window.scrollTo({ top: 0, behavior: 'instant' });
    await sleep(delay);

    while (scrollCount < maxScrolls) {
      const snapshot = {
        scrollY: window.scrollY,
        viewportHeight: window.innerHeight,
        documentHeight: document.documentElement.scrollHeight,
        visibleElements: getVisibleInteractiveElements(),
        timestamp: Date.now()
      };
      snapshots.push(snapshot);

      // Check if we've reached the bottom
      if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 10) {
        break;
      }

      // Scroll down
      window.scrollBy({ top: scrollStep, behavior: 'smooth' });
      await sleep(delay);
      scrollCount++;
    }

    // Restore original scroll position if requested
    if (parseFlag(options.restore, true)) {
      window.scrollTo({ top: originalScroll, behavior: 'instant' });
    }

    return {
      completed: true,
      snapshots,
      totalScrolls: scrollCount,
      documentHeight: document.documentElement.scrollHeight,
      message: 'Use browser_screenshot after each scroll position for images'
    };
  }

  // Get interactive elements currently visible in viewport
  function getVisibleInteractiveElements() {
    const elements = [];
    const selectors = [
      'a[href]', 'button', 'input', 'textarea', 'select',
      '[onclick]', '[role="button"]', '[role="link"]',
      '[tabindex]:not([tabindex="-1"])'
    ];

    document.querySelectorAll(selectors.join(', ')).forEach((el) => {
      const rect = el.getBoundingClientRect();

      // Check if element is in viewport
      if (rect.top < window.innerHeight && rect.bottom > 0 &&
          rect.left < window.innerWidth && rect.right > 0 &&
          rect.width > 0 && rect.height > 0 && isVisible(el)) {

        elements.push({
          tag: el.tagName.toLowerCase(),
          text: el.textContent?.trim().substring(0, 50) || null,
          selector: generateSelector(el),
          position: {
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2)
          }
        });
      }
    });

    return elements.slice(0, 50); // Limit to 50 elements
  }

  // Click an element and wait for dynamic changes
  async function clickAndWait(options) {
    const waitTimeout = options.waitTimeout || 5000;
    const waitForSelector = options.waitForSelector;
    const waitForChange = options.waitForChange !== false;

    // Set up mutation observer before clicking
    let changes = [];
    let observer = null;

    if (waitForChange) {
      observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
          changes.push({
            type: mutation.type,
            target: mutation.target.tagName?.toLowerCase(),
            addedNodes: mutation.addedNodes.length,
            removedNodes: mutation.removedNodes.length
          });
        }
      });

      observer.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true
      });
    }

    // Perform the click
    const clickResult = await performClick(options);

    // Wait for changes or specific element
    const startTime = Date.now();

    if (waitForSelector) {
      // Wait for specific element to appear
      while (Date.now() - startTime < waitTimeout) {
        const el = document.querySelector(waitForSelector);
        if (el && isVisible(el)) {
          if (observer) observer.disconnect();
          return {
            ...clickResult,
            waited: true,
            waitedMs: Date.now() - startTime,
            foundElement: getElementInfo(el),
            changes: changes.slice(0, 20)
          };
        }
        await sleep(100);
      }
    } else if (waitForChange) {
      // Wait for any DOM changes to settle
      let lastChangeCount = 0;
      let stableTime = 0;

      while (Date.now() - startTime < waitTimeout) {
        if (changes.length > lastChangeCount) {
          lastChangeCount = changes.length;
          stableTime = 0;
        } else {
          stableTime += 100;
          if (stableTime >= 500) {
            // DOM has been stable for 500ms
            break;
          }
        }
        await sleep(100);
      }
    }

    if (observer) observer.disconnect();

    return {
      ...clickResult,
      waited: true,
      waitedMs: Date.now() - startTime,
      changes: changes.slice(0, 20),
      totalChanges: changes.length
    };
  }

  // Capture full page
  async function captureFullPage(options) {
    // This needs to be handled by background script
    // Content script can only prepare the page
    return {
      scrollHeight: document.documentElement.scrollHeight,
      scrollWidth: document.documentElement.scrollWidth,
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth
    };
  }

  // Inspect element at position
  function inspectElement(options) {
    // The context menu reports page (document) coordinates; elementFromPoint
    // takes viewport coordinates, so on any scrolled page the wrong element
    // was inspected.
    const viewportCoords = parseFlag(options.viewport, false);
    const viewportX = viewportCoords
      ? options.x : options.x - window.scrollX;
    const viewportY = viewportCoords
      ? options.y : options.y - window.scrollY;
    const element = document.elementFromPoint(viewportX, viewportY);
    if (!element) {
      return { found: false };
    }

    highlightElement({ selector: generateSelector(element), duration: 5000 });

    return {
      found: true,
      element: getElementInfo(element),
      selector: generateSelector(element),
      xpath: generateXPath(element)
    };
  }

  // Get value
  function getValue(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    // Reading a password field would hand the credential to the AI just as
    // surely as typing one would, so the same guard applies. Masking rather
    // than refusing keeps the tool useful: you can still see whether the
    // field is filled. This covers the textContent branch below as well: a
    // contenteditable field is just as much a credential holder as an input.
    if (isConcealedValueField(element) && !passwordAllowed(options)) {
      return {
        value: '***',
        masked: true,
        note: 'Credential field value withheld (password, one-time code, card ' +
              'or hidden field). Set "allow_password_typing": true in ' +
              '~/.claudecodebrowser/safety.json to read credentials through the agent.',
        element: getElementInfo(element)
      };
    }

    // A checkbox or radio keeps its state in `checked`; `.value` is only the
    // string it would submit, and that string is the same ticked or not
    // (it defaults to "on"). Returning it told an agent reading a consent
    // box or a radio group nothing at all, so report the state as the value
    // and keep the submit string beside it.
    if (isCheckableInput(element)) {
      return {
        value: !!element.checked,
        checked: !!element.checked,
        submitValue: element.value ?? null,
        element: getElementInfo(element)
      };
    }

    let value;
    if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
      value = element.value;
    } else if (element.tagName === 'SELECT') {
      value = element.options[element.selectedIndex]?.value;
    } else {
      value = element.textContent;
    }

    return { value, element: getElementInfo(element) };
  }

  // <input type="checkbox"> and <input type="radio"> are the two inputs whose
  // state lives in `checked` rather than `value`.
  function isCheckableInput(element) {
    if (!element || element.tagName !== 'INPUT') return false;
    const type = String(element.type || '').toLowerCase();
    return type === 'checkbox' || type === 'radio';
  }

  // Set value
  function setValue(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    assertNotPasswordField(element, options);

    // Assigning to `.value` on a checkbox or radio changes the string it
    // submits and leaves the tick alone, so the old code reported
    // {set: true} after changing nothing the caller cared about. Take the
    // value as the state to put the box in, and refuse anything that is not
    // clearly on or off rather than guessing.
    if (isCheckableInput(element)) {
      const wanted = parseFlag(options.value, null);
      if (wanted === null) {
        throw new Error(
          `Cannot set a ${element.type} from ${JSON.stringify(options.value)}: ` +
          'its state is checked/unchecked, not text. Pass true or false ' +
          '(or "on"/"off"), or use browser_click to toggle it.'
        );
      }
      element.checked = wanted;
      element.dispatchEvent(new Event('input', { bubbles: true }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
      return { set: true, checked: wanted, element: getElementInfo(element) };
    }

    if (element.tagName === 'SELECT') {
      // A real <select> silently resets to '' when the assigned value matches
      // no option, so {set: true} was a lie for any value the dropdown does
      // not offer. Go through the same resolver browser_select_option uses.
      const chosen = chooseSelectOption(element, { value: options.value });
      element.dispatchEvent(new Event('input', { bubbles: true }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
      return { set: true, ...chosen, element: getElementInfo(element) };
    }

    if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
      element.value = options.value;
    } else if (element.isContentEditable) {
      element.textContent = options.value;
    }

    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { set: true, element: getElementInfo(element) };
  }

  // Get attribute
  function getAttribute(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    // The value attribute of a credential field is the credential whenever it
    // is server-rendered or set with setAttribute, so this path needs the same
    // guard as getValue.
    const attribute = String(options.attribute || '');
    if (attribute.toLowerCase() === 'value' &&
        isConcealedValueField(element) && !passwordAllowed(options)) {
      return {
        value: element.getAttribute(attribute) ? '***' : null,
        masked: true,
        note: 'Credential field value withheld. See allow_password_typing in ' +
              '~/.claudecodebrowser/safety.json.',
        element: getElementInfo(element)
      };
    }

    const value = element.getAttribute(attribute);
    return { value, element: getElementInfo(element) };
  }

  // Focus element
  function focusElement(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    element.focus();
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });

    return { focused: true, element: getElementInfo(element) };
  }

  // Hover element
  async function hoverElement(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    await sleep(100);

    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;

    const mouseEnter = new MouseEvent('mouseenter', { bubbles: true, clientX: x, clientY: y });
    const mouseOver = new MouseEvent('mouseover', { bubbles: true, clientX: x, clientY: y });

    element.dispatchEvent(mouseEnter);
    element.dispatchEvent(mouseOver);

    return { hovered: true, element: getElementInfo(element) };
  }

  // Press a keyboard key with optional modifiers
  async function pressKey(options) {
    let target = document.activeElement || document.body;
    if (options.selector) {
      const element = findElement(options);
      if (!element) throw new Error('Element not found');
      element.focus();
      target = element;
    }

    const eventInit = {
      key: options.key,
      bubbles: true,
      cancelable: true,
      ctrlKey: !!options.ctrl,
      shiftKey: !!options.shift,
      altKey: !!options.alt,
      metaKey: !!options.meta
    };

    target.dispatchEvent(new KeyboardEvent('keydown', eventInit));
    target.dispatchEvent(new KeyboardEvent('keypress', eventInit));
    target.dispatchEvent(new KeyboardEvent('keyup', eventInit));

    // Synthetic key events don't trigger default actions, so emulate the
    // common one: Enter inside a form submits it
    if (options.key === 'Enter' && !options.ctrl && !options.shift && target.form) {
      if (target.form.requestSubmit) {
        target.form.requestSubmit();
      } else {
        target.form.submit();
      }
    }

    return { pressed: options.key, element: getElementInfo(target) };
  }

  // Extract visible text from the page or an element
  function getText(options) {
    let element = document.body;
    if (options.selector) {
      element = findElement(options);
      if (!element) throw new Error('Element not found');
    }

    // Asking for the text of a credential field is asking for its value.
    // Only the named element is checked: scrubbing the whole page's innerText
    // is not something this can do honestly, so document-wide text stays the
    // caller's own risk.
    if (isConcealedValueField(element) && !passwordAllowed(options)) {
      return {
        text: '***',
        masked: true,
        note: 'Credential field text withheld. Use browser_get_value with ' +
              '"allow_password_typing": true in ~/.claudecodebrowser/safety.json ' +
              'to read credentials through the agent.',
        url: window.location.href,
        title: document.title
      };
    }

    const maxLength = options.maxLength || 20000;
    // innerText is the visible text this tool promises: it leaves out
    // display:none subtrees, textContent does not. `innerText || textContent`
    // fell through whenever the visible text was empty - a hidden template,
    // a collapsed menu - and handed back the hidden markup as "visible text".
    // The fallback is still needed where innerText does not exist (an SVG
    // element, a detached node), so keep it and say which one was read.
    const visible = typeof element.innerText === 'string';
    const text = visible ? element.innerText : (element.textContent || '');
    const source = visible ? 'innerText' : 'textContent';

    return {
      text: text.slice(0, maxLength),
      truncated: text.length > maxLength,
      totalLength: text.length,
      source,
      url: window.location.href,
      title: document.title
    };
  }

  // Select option
  function selectOption(options) {
    const element = findElement(options);
    if (!element || element.tagName !== 'SELECT') {
      throw new Error('Select element not found');
    }

    const chosen = chooseSelectOption(element, options);

    element.dispatchEvent(new Event('change', { bubbles: true }));

    return { selected: true, ...chosen };
  }

  // Pick the option the caller asked for and report what the <select> is
  // actually on afterwards. The old code assigned element.value and trusted
  // it: a value no option carries resets a real <select> to '', and a `text`
  // that matched nothing was ignored without a word, so the caller was told
  // {selected: true} about a dropdown it had not moved. Selecting by index
  // is the one authoritative assignment - it updates .value for us, and it
  // still works when two options share a value.
  function chooseSelectOption(element, request) {
    const list = Array.from(element.options || []);
    let index = -1;

    if (request.value !== undefined) {
      index = list.findIndex(o => o.value === String(request.value));
    } else if (request.index !== undefined) {
      index = Number(request.index);
      if (!Number.isInteger(index) || index < 0 || index >= list.length) index = -1;
    } else if (request.text !== undefined) {
      index = list.findIndex(o => o.text === request.text);
    } else {
      throw new Error('Nothing to select: pass value, text or index.');
    }

    if (index < 0) {
      const offered = list.map(o => o.value).join(', ') || '(none)';
      throw new Error(
        `No option matching ${JSON.stringify(request.value ?? request.text ?? request.index)}. ` +
        `Available values: ${offered}`
      );
    }

    element.selectedIndex = index;

    return {
      value: element.value,
      text: list[element.selectedIndex]?.text ?? null,
      index: element.selectedIndex
    };
  }

  // Get computed styles
  function getComputedStyles(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    const styles = window.getComputedStyle(element);
    // getPropertyValue takes a CSS property name, not the camelCase alias:
    // 'backgroundColor', 'fontSize' and 'fontFamily' used to come back ''
    // on every real page, so three of these twelve defaults never reported
    // anything.
    const properties = options.properties || [
      'display', 'visibility', 'opacity', 'position',
      'width', 'height', 'color', 'background-color',
      'font-size', 'font-family', 'margin', 'padding'
    ];

    const result = {};
    properties.forEach(prop => {
      // Callers (and older workflows) still pass camelCase, so translate
      // before asking, and key the answer by whatever name they used.
      result[prop] = styles.getPropertyValue(cssPropertyName(prop));
    });

    return { styles: result, element: getElementInfo(element) };
  }

  // backgroundColor -> background-color. A name that is already hyphenated
  // or is a --custom-property passes through untouched.
  function cssPropertyName(prop) {
    const name = String(prop);
    if (name.startsWith('--')) return name;
    return name.replace(/[A-Z]/g, (ch) => `-${ch.toLowerCase()}`);
  }

  // Get bounding rect
  function getBoundingRect(options) {
    const element = findElement(options);
    if (!element) throw new Error('Element not found');

    const rect = element.getBoundingClientRect();
    return {
      rect: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        left: rect.left
      },
      element: getElementInfo(element)
    };
  }

  // Helper functions
  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function isVisible(element) {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();

    return (
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0' &&
      rect.width > 0 &&
      rect.height > 0
    );
  }

  function getElementInfo(element) {
    const rect = element.getBoundingClientRect();
    // Element metadata is incidental to every tool that returns it, so a
    // credential value is always masked here, with no override.
    // browser_get_value is the one deliberate way to read one.
    const value = safeElementValue(element, 200, {});
    return {
      tag: element.tagName.toLowerCase(),
      id: element.id || null,
      classes: Array.from(element.classList),
      name: element.name || null,
      type: element.type || null,
      text: safeElementText(element, 200, {}),
      value: value,
      href: element.href || null,
      src: element.src || null,
      placeholder: element.placeholder || null,
      ariaLabel: element.getAttribute('aria-label'),
      role: element.getAttribute('role'),
      disabled: element.disabled,
      checked: element.checked,
      visible: isVisible(element),
      position: {
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        centerX: rect.left + rect.width / 2,
        centerY: rect.top + rect.height / 2
      },
      selector: generateSelector(element)
    };
  }

  function generateSelector(element) {
    if (element.id) return `#${cssIdentifier(element.id)}`;

    const path = [];
    let current = element;

    while (current && current !== document.body) {
      let selector = current.tagName.toLowerCase();

      if (current.id) {
        selector = `#${cssIdentifier(current.id)}`;
        path.unshift(selector);
        break;
      }

      if (current.className && typeof current.className === 'string') {
        const classes = current.className.trim().split(/\s+/).filter(c => c && !c.match(/^[0-9]/));
        if (classes.length > 0) {
          selector += '.' + classes.slice(0, 2).map(cssIdentifier).join('.');
        }
      }

      const siblings = current.parentElement?.children || [];
      const sameTagSiblings = Array.from(siblings).filter(s => s.tagName === current.tagName);
      if (sameTagSiblings.length > 1) {
        const index = sameTagSiblings.indexOf(current) + 1;
        selector += `:nth-of-type(${index})`;
      }

      path.unshift(selector);
      current = current.parentElement;
    }

    return path.join(' > ');
  }

  function generateXPath(element) {
    if (element.id) return `//*[@id=${xpathLiteral(element.id)}]`;

    const parts = [];
    let current = element;

    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = current.previousSibling;

      while (sibling) {
        if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === current.tagName) {
          index++;
        }
        sibling = sibling.previousSibling;
      }

      const tagName = current.tagName.toLowerCase();
      parts.unshift(`${tagName}[${index}]`);
      current = current.parentNode;
    }

    return '/' + parts.join('/');
  }

  console.log('[ClaudeCodeBrowser] Content script loaded');
})();
