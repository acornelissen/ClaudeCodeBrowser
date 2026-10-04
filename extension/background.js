/**
 * ClaudeCodeBrowser - Background Script
 * Handles native messaging, tab management, and coordination with content scripts
 *
 * MIT License
 * Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
 * Author: dre@ligandal.com
 */

const NATIVE_HOST_NAME = "claudecodebrowser";
let nativePort = null;
let isConnected = false;
let pendingRequests = new Map();
let requestCounter = 0;

// Reconnection settings
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 20;
const INITIAL_RECONNECT_DELAY = 1000;  // 1 second
const MAX_RECONNECT_DELAY = 30000;     // 30 seconds

// ============================================================
// Network logging (webRequest)
//
// Capture happens here, at the network layer, not by wrapping page globals.
// Firefox's content-script sandbox refuses a window.fetch override, so a
// content script can only ever see XHR — and fetch is what modern apps use.
// webRequest sees everything, needs no page mutation, and is unaffected by a
// page's CSP.
//
// Listeners are attached only while at least one tab is being logged, so a
// browser that nobody asked to log pays nothing.
// ============================================================

// Feature flags arrive over JSON-RPC, HTTP and native messaging, and a client
// that has not loaded the current schema can coerce a boolean to a string:
// observed live as include_all_types: "true", which === true rejected, so a
// documented option silently did nothing. Worse for capture_bodies, where
// "false" !== false would have captured bodies for a caller who asked for
// none - a privacy option failing open.
//
// This leniency is for FEATURE flags only. The credential override stays
// strictly === true (see passwordAllowed in content.js): a fail-closed
// security switch must not be unlocked by any truthy-looking value.
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

const MAX_LOG_ENTRIES = 500;
const MAX_BODY_CHARS = 5000;
// What the scrubber is allowed to see: the kept body plus a margin, so a
// credential straddling the cut is still redacted before the cut without
// handing the regex passes an unbounded string. See filter.ondata.
const SCRUB_LIMIT = MAX_BODY_CHARS + 1000;
const MAX_PENDING_REQUESTS = 300;

// fetch() and XHR both surface as "xmlhttprequest" here, which is the traffic
// worth logging when debugging an app. Assets are excluded unless asked for.
const DEFAULT_REQUEST_TYPES = [
  "xmlhttprequest", "websocket", "beacon", "ping", "csp_report", "other"
];

// Headers whose values carry credentials. The name stays visible, the value
// does not — these logs are read by the agent.
const REDACTED_HEADERS = new Set([
  "authorization", "proxy-authorization", "cookie", "set-cookie",
  "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
  "api-key", "auth-token", "x-session-token", "x-access-token"
]);

// Response bodies are only collected for types that are text to begin with.
const TEXTUAL_CONTENT_TYPE = /^(text\/|application\/(json|javascript|xml|x-www-form-urlencoded)|application\/[^;]*\+json)/i;

// A body is bytes; decoding it needs the charset the server declared. Always
// assuming UTF-8 turned a shift_jis or windows-1252 page into mojibake with
// no indication, so the agent reasoned over corrupted text believing it was
// the page's content.
function charsetFromContentType(value) {
  const match = /charset\s*=\s*"?([^";\s]+)/i.exec(value || '');
  return match ? match[1].toLowerCase() : null;
}

// JSON is UTF-8 by definition. RFC 8259 section 8.1 requires it and says the
// charset parameter must be ignored, and plenty of older stacks send
// `application/json;charset=ISO-8859-1` while emitting UTF-8 anyway. Trusting
// that label turned `café` into `cafÃ©` in the log with no note attached,
// because iso-8859-1 is a charset TextDecoder knows, so nothing looked wrong.
function charsetForBody(contentType) {
  const type = (contentType || "").split(";")[0].trim().toLowerCase();
  if (type === "application/json" || type.endsWith("+json")) return "utf-8";
  return charsetFromContentType(contentType) || "utf-8";
}

// TextDecoder throws on a label it does not know; fall back rather than
// losing the body entirely.
function decoderFor(charset) {
  if (!charset || charset === 'utf-8' || charset === 'utf8') {
    return { decoder: new TextDecoder('utf-8'), charset: 'utf-8', fallback: false };
  }
  try {
    return { decoder: new TextDecoder(charset), charset: charset, fallback: false };
  } catch (e) {
    return { decoder: new TextDecoder('utf-8'), charset: charset, fallback: true };
  }
}

// Whether a decoded body actually looks like text.
//
// An earlier version of this refused any response carrying a content-encoding
// header, reasoning that compressed bytes cannot be decoded. That was wrong
// and would have been a bad regression: MDN's StreamFilter examples feed
// ondata straight to TextDecoder, so Firefox hands the filter DECOMPRESSED
// bytes - while the content-encoding header still appears in
// onHeadersReceived. Refusing on the header would therefore have dropped
// bodies on essentially every real site, since almost everything serves gzip
// or br.
//
// Judging the decoded result instead is correct either way: genuinely
// undecodable bytes produce a mass of U+FFFD replacement characters and
// control bytes, which is detectable without having to know what Firefox did
// upstream.
function looksUndecodable(text) {
  if (!text) return false;
  const sample = text.slice(0, 2000);
  let suspicious = 0;
  for (const character of sample) {
    const code = character.codePointAt(0);
    // Replacement character, or a control byte that is not tab/LF/CR.
    if (code === 0xFFFD || code < 0x09 || (code > 0x0D && code < 0x20)) {
      suspicious++;
    }
  }
  return sample.length > 0 && suspicious / sample.length > 0.1;
}

// tabId -> { captureBodies, includeAllTypes }
const loggedTabs = new Map();
// tabId -> finished entries
const networkLogsByTab = new Map();
// webRequest requestId -> in-flight entry
const pendingNetworkRequests = new Map();
// tabId -> how many callers are waiting for the tab to go quiet. Counting
// in-flight requests needs the same listeners logging does, so the two share
// them and whoever leaves last takes them down.
const idleWatchers = new Map();
// A request still open after this long is treated as a persistent channel
// (WebSocket, SSE, long-poll) and no longer counts against network idle: it
// will not complete until the page closes it, so waiting for it is waiting
// forever. It stays in the log; it just stops blocking idle.
const PERSISTENT_REQUEST_MS = 10000;

// tabId -> { requests: Map<requestId, startedAt>, lastActivity: timestamp }
const inFlightByTab = new Map();
let webRequestListenersAttached = false;

// A tab is watched when anything needs its traffic observed.
function isWatchedTab(tabId) {
  return tabId !== undefined && tabId >= 0 &&
    (loggedTabs.has(tabId) || idleWatchers.has(tabId));
}

// Private windows exist so their contents are not retained. If the extension
// has been allowed to run in them, a logging session there would still put
// request and response bodies into a buffer the agent reads, which is the one
// place that expectation must not be quietly broken.
// Fails CLOSED. This returned false when tabs.get threw, so a check that
// could not tell was the reason a private window's traffic reached the
// agent's buffer: with the tab marked private and tabs.get failing,
// startLogging succeeded. A privacy gate that cannot answer must refuse.
async function isPrivateTab(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    return tab.incognito === true;
  } catch (e) {
    return true;
  }
}

function shouldLogRequest(details) {
  const options = loggedTabs.get(details.tabId);
  if (!options) return false;
  if (options.includeAllTypes) return true;
  return DEFAULT_REQUEST_TYPES.includes(details.type);
}

function inFlightFor(tabId) {
  if (!inFlightByTab.has(tabId)) {
    inFlightByTab.set(tabId, { requests: new Map(), lastActivity: Date.now() });
  }
  return inFlightByTab.get(tabId);
}

// Idle counting deliberately covers every request type, not just the ones
// worth logging: a page is not quiet while it is still pulling images.
function noteRequestStarted(details) {
  const state = inFlightFor(details.tabId);
  state.requests.set(details.requestId, Date.now());
  state.lastActivity = Date.now();
}

// Requests that are still young enough to be worth waiting for.
function pendingRequestCount(tabId, persistentAfter = PERSISTENT_REQUEST_MS) {
  const state = inFlightFor(tabId);
  const now = Date.now();
  let count = 0;
  for (const startedAt of state.requests.values()) {
    if (now - startedAt < persistentAfter) count++;
  }
  return count;
}

function noteRequestFinished(details) {
  const state = inFlightByTab.get(details.tabId);
  if (!state) return;
  state.requests.delete(details.requestId);
  state.lastActivity = Date.now();
}

function redactHeaderList(headers) {
  const out = {};
  for (const header of headers || []) {
    const value = REDACTED_HEADERS.has(header.name.toLowerCase())
      ? "***"
      // A header with only a binaryValue gave undefined for its name.
      : (header.value !== undefined ? header.value : "[binary]");
    // A plain assignment collapsed repeats onto one key, and almost every
    // real response carries several Set-Cookie headers - so the log said
    // there was one, with the last value. An array keeps the count.
    if (Object.prototype.hasOwnProperty.call(out, header.name)) {
      const existing = out[header.name];
      out[header.name] = Array.isArray(existing)
        ? existing.concat(value)
        : [existing, value];
    } else {
      out[header.name] = value;
    }
  }
  return out;
}

// Best-effort scrub of credential-shaped values inside a captured body. The
// header allowlist is worthless if the body that mints the token is logged
// verbatim one request earlier. This cannot be complete - a body is arbitrary
// data - so bodies stay off by default for anything but textual responses and
// can be disabled entirely with capture_bodies: false.
// Names that mark a value as a credential. One list, mirrored in
// extension/content.js's CREDENTIAL_NAME_RE - change one, change the other,
// and a test pins them together. Match through looksLikeCredentialName(),
// never directly: it handles camelCase boundaries, which this pattern cannot.
//
// Deliberately loose, because over-redacting a log costs less than
// under-redacting one - but the short and prefix-ambiguous entries are
// anchored, because unanchored they matched words that cannot name a
// credential: `auth` hid every `author` object in a captured API response,
// `ssn` hid `className` (cla-ssn-ame) and `businessName`, `pass` hid `passed`
// and `bypassCache`, `otp` hid `notPublished`.
const SECRET_KEY_RE =
  /(pass(?:word|wd|phrase|code|key)|userpass|(?:^|[^a-z])pass(?:[^a-z]|$)|pwd|secret|token|credential|one[-_]?time[-_]?code|(?:^|[^a-z])otp(?:[^a-z]|$)|oauth|authorization|authenticat|auth(?:z|n)(?:[^a-z]|$)|auth[-_]?(?:token|key|code|header|secret|data)|(?:^|[^a-z])auth(?:[^a-z]|$)|api[-_]?key|private[-_]?key|session[-_]?(?:id|token|key|secret|value)|(?:^|[^a-z])session(?:[^a-z]|$)|sessid|cvv|cvc|card[-_]?number|jwt|bearer|signature|(?:^|[^a-z])ssn(?:[^a-z]|$)|(?:^|[^a-z])pin(?:[^a-z]|$))/i;

// A credential-shaped NAME, from a JSON key, a form field name or an id.
//
// Tested against a copy with camelCase boundaries turned into separators,
// because the anchors below only recognise a non-letter as a boundary. Without
// that step, anchoring `auth` to stop it matching `author` also stopped
// `otpCode`, `sessionValue`, `authData` and `pinCode` matching at all - names
// the unanchored version did catch. So the anchoring that fixed over-redaction
// silently introduced ten under-redactions, which is the worse direction.
// Compound lowercase names have no boundary to find, so the credential ones
// are listed explicitly: passkey, userpass, authz, authn, oauth.
function normaliseNameForMatching(name) {
  return String(name == null ? '' : name)
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2');
}

function looksLikeCredentialName(name) {
  return SECRET_KEY_RE.test(normaliseNameForMatching(name));
}

// Markup carries credentials in attributes, not just in JSON keys: a
// server-rendered form with a prefilled password puts it in value="...",
// which is exactly the thing the DOM-level guard masks. Found by live
// testing - the HTML of a logged page arrived with the password in clear
// while browser_get_page_info was correctly returning "***" for the same
// field.
const SECRET_INPUT_RE =
  /type\s*=\s*["']?(password|hidden)|autocomplete\s*=\s*["'][^"']*(current-password|new-password|one-time-code|cc-number|cc-csc)/i;

function redactHtmlInputValues(text) {
  // Tag-level: find each <input ...> and blank its value when the tag itself
  // looks like a credential field. Regex over HTML is crude, but this is
  // best-effort redaction of a log, not parsing.
  return text.replace(/<input\b[^>]*>/gi, (tag) => {
    const looksSecret = SECRET_INPUT_RE.test(tag) || looksLikeCredentialName(
      (tag.match(/(?:name|id)\s*=\s*["']?([^"'\s>]*)/i) || [])[1] || '');
    if (!looksSecret) return tag;
    return tag.replace(/(\bvalue\s*=\s*)(["'])(?:(?!\2).)*\2/gi, '$1$2***$2')
              .replace(/(\bvalue\s*=\s*)(?!["'])[^\s>]+/gi, '$1***');
  });
}

// Walk a parsed structure, replacing any value under a credential-shaped key.
// Structural, because a regex over the serialised form can only ever match the
// value shapes somebody thought of: {"otp":654321}, {"tokens":["a","b"]} and
// {"auth":{"value":"x"}} all went through the old string pass untouched, and
// webRequest's requestBody.formData is ALWAYS array-valued
// ({"password":["hunter2"]}), so the single most privacy-relevant request the
// extension sees - an HTML form login - logged the password verbatim.
const MAX_REDACT_DEPTH = 12;

function redactStructure(value, depth = 0) {
  if (depth > MAX_REDACT_DEPTH) {
    // Fail CLOSED. This returned the raw subtree, so a credential nested
    // deeper than the limit was logged in clear - and the old flat regex,
    // which this replaced, scrubbed one at any depth. Depth 13 is ordinary
    // in GraphQL and paginated API responses, so this was not a corner case.
    return '[nested too deep; withheld]';
  }
  if (typeof value === "string") {
    // A JSON string can itself hold a rendered form with a prefilled
    // password, so the text passes still have to run over it. Without this,
    // taking the structural path would have been a regression for
    // {"html":"<input type=password value=secret>"}.
    return redactTextPasses(value);
  }
  if (Array.isArray(value)) {
    return value.map(item => redactStructure(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value)) {
      const child = value[key];
      // A boolean or null is never a credential, whatever the key is called,
      // so it is kept even under a credential-shaped name. Without this,
      // `authenticated: true` and `verified: false` were replaced with ***,
      // which destroys the one field that tells you whether the login you are
      // debugging actually worked. A NUMBER is not exempt: an OTP or PIN is a
      // number and is exactly what has to be hidden.
      if (typeof child === 'boolean' || child === null) {
        out[key] = child;
        continue;
      }
      // A secret key hides its whole subtree, whatever shape it is.
      out[key] = looksLikeCredentialName(key)
        ? "***"
        : redactStructure(child, depth + 1);
    }
    return out;
  }
  return value;
}

// name="password" in a multipart frame, whose value is the lines that follow
// it up to the next boundary. This is what a form POST with a file input
// looks like, so it is an ordinary login shape, not an exotic one.
function redactMultipartFields(text) {
  return text.replace(
    /(name\s*=\s*"([^"]*)"[^\r\n]*\r?\n(?:[^\r\n]+\r?\n)*\r?\n)([\s\S]*?)(?=\r?\n--|$)/g,
    (match, head, name, body) =>
      (looksLikeCredentialName(name) ? `${head}***` : match));
}

// The text passes, for a body that is not wholly JSON and for the strings
// inside one that is.
function redactTextPasses(text) {
  // key="value" and key: 'value' - XML and HTML attributes, JS object
  // literals, and JSON that did not parse because it was cut at the cap.
  let out = text.replace(
    /([A-Za-z0-9_\-\[\]."]+)(\s*[:=]\s*)(["'])(?:(?!\3)[^\\]|\\.)*\3/g,
    (match, key, sep, quote) =>
      (looksLikeCredentialName(key) ? `${key}${sep}${quote}***${quote}` : match));
  // Unquoted JSON numbers: "otp": 654321. true/false/null are deliberately
  // not in here - a boolean is never a credential, and masking
  // `authenticated: true` hides the result you were looking for.
  out = out.replace(
    /("(?:[^"\\]|\\.)*"\s*:\s*)(-?\d[\d.eE+-]*)/g,
    (match, keyPart) => (looksLikeCredentialName(keyPart) ? `${keyPart}"***"` : match));
  out = redactMultipartFields(out);
  out = redactHtmlInputValues(out);
  // Form-encoded: key=value, anchored to a real pair separator and stopping
  // at one. Unanchored with a loose value class this ate the rest of the
  // line: `const apiKey=process.env.KEY;let sessionId=1;` came out as
  // `const apiKey=*** sessionId=***`, destroying the following statement,
  // and `a.password==="x"` became `a.password=***"x"` - mangled and still
  // leaking. The (?!=) guard keeps it off JS comparisons.
  out = out.replace(
    /(^|[&?;\s])([A-Za-z0-9_\-\[\].]+)=(?!=)([^&\s<>"';]*)/g,
    (match, lead, key) => (looksLikeCredentialName(key) ? `${lead}${key}=***` : match));
  return out;
}

function redactSecretsInBody(text) {
  if (!text) return text;
  try {
    // A body that is wholly JSON is redacted structurally and re-serialised,
    // which covers every value shape exactly.
    const trimmed = text.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        const scrubbed = JSON.stringify(redactStructure(JSON.parse(trimmed)));
        // Re-serialising is lossy: JSON.stringify(JSON.parse(x)) turns
        // 12345678901234567890 into 12345678901234567000, 1e400 into null and
        // 1.0 into 1. A Snowflake- or Twitter-style id in a captured body
        // came back silently wrong. So only hand back the rebuilt text when
        // rebuilding it actually removed something; otherwise the original
        // bytes are both safe and exact. When something WAS removed the body
        // is rewritten and a large id may lose precision - the *** says so,
        // and redacting a credential is worth more than an exact id.
        if (scrubbed.includes('***') ||
            scrubbed.includes('[nested too deep; withheld]')) {
          return scrubbed;
        }
        return text;
      } catch (e) {
        // Not valid JSON (or truncated); fall through to the text passes.
      }
    }
    return redactTextPasses(text);
  } catch (e) {
    return "[redaction failed; body withheld]";
  }
}

// webRequest hands request bodies over as form fields or raw byte buffers.
function describeRequestBody(requestBody) {
  if (!requestBody) return null;
  try {
    // Firefox sets this when it could not read the body at all (too large,
    // or already consumed). Returning null made that look like a request
    // with no body, so the agent concluded the POST was empty.
    if (requestBody.error) {
      return `[body not available: ${requestBody.error}]`;
    }
    if (requestBody.formData) {
      // Structural, not a scrub of the serialised form: formData values are
      // arrays ({"password":["hunter2"]}), which the string passes cannot see.
      return JSON.stringify(
        redactStructure(requestBody.formData)).substring(0, 1000);
    }
    if (requestBody.raw && requestBody.raw.length) {
      // fatal: false so an undecodable request body yields replacement
      // characters rather than throwing away the whole entry.
      const decoder = new TextDecoder("utf-8", { fatal: false });
      // A chunk with a `file` and no `bytes` is an upload: Firefox gives the
      // filename rather than the contents. Both were dropped silently, so a
      // file upload logged as an empty body.
      const files = requestBody.raw
        .filter(chunk => chunk.file)
        .map(chunk => chunk.file);
      const text = requestBody.raw
        .map(chunk => (chunk.bytes ? decoder.decode(chunk.bytes, { stream: true }) : ""))
        .join("") + decoder.decode();
      if (files.length && !text) {
        return `[file upload: ${files.join(", ")}]`;
      }
      // Scrub a bounded window rather than the whole body: only the first
      // 1000 characters are kept, and a multi-megabyte upload would otherwise
      // run every regex pass over all of it. The window is wide enough that a
      // credential straddling the 1000-character cut is still scrubbed first.
      const scrubbed = redactSecretsInBody(text.substring(0, 4000));
      const body = text.length > 1000
        ? scrubbed.substring(0, 1000) + "…[truncated]"
        : scrubbed;
      return files.length ? `${body} [files: ${files.join(", ")}]` : body;
    }
  } catch (e) {
    return "[could not decode request body]";
  }
  return null;
}

function storeNetworkEntry(tabId, entry) {
  if (!networkLogsByTab.has(tabId)) {
    networkLogsByTab.set(tabId, []);
  }
  const logs = networkLogsByTab.get(tabId);
  logs.push(entry);
  if (logs.length > MAX_LOG_ENTRIES) {
    logs.shift();
  }
}

function finalizeNetworkRequest(requestId, extra) {
  const entry = pendingNetworkRequests.get(requestId);
  if (!entry) return;
  pendingNetworkRequests.delete(requestId);
  Object.assign(entry, extra);
  // Internal bookkeeping, not something the caller should reason about.
  delete entry.filterAttached;
  if (entry.startedAt) {
    entry.duration = Date.now() - entry.startedAt;
    delete entry.startedAt;
  }
  storeNetworkEntry(entry.tabId, entry);
}

const onBeforeRequestListener = (details) => {
  if (!isWatchedTab(details.tabId)) return;
  noteRequestStarted(details);

  if (!shouldLogRequest(details)) return;

  // A runaway page must not grow this map without bound. The oldest entry is
  // usually a persistent channel that will not complete; record that it was
  // dropped rather than losing it silently.
  if (pendingNetworkRequests.size >= MAX_PENDING_REQUESTS) {
    const oldestId = pendingNetworkRequests.keys().next().value;
    const oldest = pendingNetworkRequests.get(oldestId);
    pendingNetworkRequests.delete(oldestId);
    if (oldest) {
      oldest.error = "[dropped: too many requests in flight to track]";
      storeNetworkEntry(oldest.tabId, oldest);
    }
  }

  // capture_bodies: false means no bodies at all, request or response.
  const options = loggedTabs.get(details.tabId);
  pendingNetworkRequests.set(details.requestId, {
    type: details.type,
    method: details.method,
    url: details.url,
    tabId: details.tabId,
    requestBody: (options && options.captureBodies)
      ? describeRequestBody(details.requestBody)
      : undefined,
    startTime: new Date().toISOString(),
    startedAt: Date.now()
  });
};

const onBeforeSendHeadersListener = (details) => {
  const entry = pendingNetworkRequests.get(details.requestId);
  if (entry) {
    entry.requestHeaders = redactHeaderList(details.requestHeaders);
  }
};

const onHeadersReceivedListener = (details) => {
  try {
    captureResponseHeaders(details);
  } catch (e) {
    // This listener is registered as "blocking"; an exception escaping it
    // must never be able to interfere with the response.
    console.error("[ClaudeCodeBrowser] header capture failed:", e);
  }
};

function captureResponseHeaders(details) {
  const entry = pendingNetworkRequests.get(details.requestId);
  if (!entry) return;

  entry.status = details.statusCode;
  entry.statusText = details.statusLine;
  entry.responseHeaders = redactHeaderList(details.responseHeaders);

  const options = loggedTabs.get(details.tabId);
  if (!options || !options.captureBodies) return;

  const headerValue = (name) => {
    const found = (details.responseHeaders || [])
      .find(h => h.name.toLowerCase() === name);
    return found ? found.value : null;
  };

  const contentType = headerValue("content-type");
  if (!contentType || !TEXTUAL_CONTENT_TYPE.test(contentType)) {
    entry.responseBody = "[not captured: non-textual content type]";
    return;
  }

  // Recorded for diagnostics only. Deliberately NOT used to refuse the body:
  // the header is present even when Firefox has already decompressed the
  // bytes, so refusing on it would drop almost every real response.
  const encoding = (headerValue("content-encoding") || "").trim();
  if (encoding) entry.responseEncoding = encoding;

  entry.responseCharset = charsetForBody(contentType);

  // onBeforeRequest fires again for a redirect target under the same
  // requestId, so without this guard a 30x could attach a second filter to
  // the same channel and the two would race over entry.responseBody.
  if (entry.filterAttached) return;
  entry.filterAttached = true;
  attachResponseBodyReader(details.requestId, entry);
}

// Read-only stream filter.
//
// Firefox suspends the response inside the filter until the extension calls
// close() or disconnect(); if neither happens the response is kept alive
// forever and the page's request never completes. So every exit path here ends
// in one of the two, including the paths that are "impossible": a filter that
// neither stops nor errors is released by a watchdog, and any unexpected throw
// disconnects, which hands the rest of the response to Firefox untouched.
//
// Every chunk is written back byte for byte, so what the page receives is
// exactly what it would have received without us.
const FILTER_WATCHDOG_MS = 120000;

function attachResponseBodyReader(requestId, entry) {
  let filter;
  try {
    filter = browser.webRequest.filterResponseData(requestId);
  } catch (e) {
    // No webRequestBlocking permission, or the request is not filterable.
    entry.responseBody = "[not captured: response filtering unavailable]";
    return;
  }

  const { decoder, charset, fallback } = decoderFor(entry.responseCharset);
  if (fallback) {
    // Say so rather than silently producing mojibake.
    entry.charsetNote = `unsupported charset "${charset}"; decoded as utf-8`;
  }
  let collected = "";
  // Counted on every chunk, including the ones past the cap, so a truncated
  // body can say how big it really was. collected.length cannot: collection
  // stops at the cap, so it never exceeded MAX_BODY_CHARS by more than one
  // chunk and landed exactly on it whenever the chunks divided evenly - which
  // is the case where truncation went unflagged entirely.
  let totalBytes = 0;
  let capped = false;
  let released = false;

  // Hand the stream back exactly once, whichever way we got here.
  const release = (how) => {
    if (released) return;
    released = true;
    clearTimeout(watchdog);
    try {
      if (how === "close") {
        filter.close();
      } else {
        // disconnect() lets Firefox deliver whatever is left, unfiltered.
        filter.disconnect();
      }
    } catch (e) {
      // Already closed or disconnected by the browser.
    }
  };

  // Belt and braces: a channel that is cancelled or redirected may deliver
  // neither onstop nor onerror, and a suspended filter would stall the page.
  const watchdog = setTimeout(() => {
    if (!released) {
      entry.responseBody = "[not captured: response filter timed out]";
      release("disconnect");
    }
  }, FILTER_WATCHDOG_MS);

  filter.ondata = (event) => {
    // Collect first, but never let collection stop the pass-through.
    totalBytes += (event.data && event.data.byteLength) || 0;
    try {
      if (collected.length < SCRUB_LIMIT) {
        collected += decoder.decode(event.data, { stream: true });
        if (collected.length > SCRUB_LIMIT) {
          // Hard-bound what the scrubber will ever see. The length check
          // above happens BEFORE appending, so a single large chunk - and
          // Firefox can deliver one response in one chunk - landed in full
          // and the scrubber then ran over all of it. Its passes are
          // quadratic on adversarial input: 5000 characters of quote marks
          // take 30ms, 320,000 take nearly two minutes, and this is the
          // single-threaded background script, so a page could stall every
          // tool call by serving a few hundred KB of punctuation.
          collected = collected.slice(0, SCRUB_LIMIT);
          capped = true;
        }
      } else {
        capped = true;
      }
    } catch (e) {
      // Undecodable chunk (wrong charset, still-compressed bytes): skip it.
    }
    try {
      filter.write(event.data);
    } catch (e) {
      // write() throws if the filter is no longer transferring data. Dropping
      // the chunk would truncate what the page sees, so give the stream back
      // and let Firefox finish the job.
      entry.responseBody = "[not captured: response filter write failed]";
      release("disconnect");
    }
  };

  filter.onstop = () => {
    // Flush whatever the streaming decoder is holding, so a multi-byte
    // character split across the final chunk boundary is not lost.
    try {
      collected += decoder.decode();
    } catch (e) {
      // Nothing buffered.
    }
    if (looksUndecodable(collected)) {
      // Binary noise dressed up as a string is worse than saying nothing: an
      // agent would reason over it as if it were the page's content.
      entry.responseBody = "[not captured: body did not decode as text" +
        (entry.responseEncoding ? ` (content-encoding: ${entry.responseEncoding})` : "") +
        "]";
      release("close");
      return;
    }
    // Scrub first, cut second. The other way round, a credential straddling
    // the cut survived as a fragment: a 5000-character JSON body ended
    // `…","access_token":"SECRET-JWT-` in the log, because the unterminated
    // string no longer matched anything the scrubber looks for. The request
    // path has always done it in this order.
    const scrubbed = redactSecretsInBody(collected);
    entry.responseBody = scrubbed.length > MAX_BODY_CHARS
      ? scrubbed.substring(0, MAX_BODY_CHARS)
      : scrubbed;
    if (capped || collected.length > MAX_BODY_CHARS) {
      // Previously the cap applied with no indication, so the agent could not
      // tell a short body from a truncated one.
      entry.responseBodyTruncated = true;
      entry.responseBodyBytes = totalBytes;
    }
    release("close");
  };

  filter.onerror = () => {
    entry.responseBody = `[not captured: ${filter.error || "stream error"}]`;
    release("disconnect");
  };
}

// A redirect ends this hop. Store it under its own URL so the first hop's
// method, URL and body are not overwritten by the target's.
const onBeforeRedirectListener = (details) => {
  const entry = pendingNetworkRequests.get(details.requestId);
  if (!entry) return;
  pendingNetworkRequests.delete(details.requestId);
  entry.redirectedTo = details.redirectUrl;
  entry.status = details.statusCode;
  if (entry.startedAt) {
    entry.duration = Date.now() - entry.startedAt;
    delete entry.startedAt;
  }
  delete entry.filterAttached;
  storeNetworkEntry(entry.tabId, entry);
};

const onCompletedListener = (details) => {
  noteRequestFinished(details);
  finalizeNetworkRequest(details.requestId, {
    status: details.statusCode,
    fromCache: details.fromCache
  });
};

const onErrorOccurredListener = (details) => {
  noteRequestFinished(details);
  finalizeNetworkRequest(details.requestId, { error: details.error });
};

function attachWebRequestListeners() {
  if (webRequestListenersAttached) return { attached: true };
  const filter = { urls: ["<all_urls>"] };
  try {
    browser.webRequest.onBeforeRequest.addListener(
      onBeforeRequestListener, filter, ["requestBody"]);
    browser.webRequest.onBeforeSendHeaders.addListener(
      onBeforeSendHeadersListener, filter, ["requestHeaders"]);
    // "blocking" is required for filterResponseData() to be callable from
    // this listener; without it the filter is never created and response
    // bodies are silently never captured. The listener returns nothing, so
    // it does not actually alter or delay the response.
    browser.webRequest.onHeadersReceived.addListener(
      onHeadersReceivedListener, filter, ["responseHeaders", "blocking"]);
    browser.webRequest.onBeforeRedirect.addListener(onBeforeRedirectListener, filter);
    browser.webRequest.onCompleted.addListener(onCompletedListener, filter);
    browser.webRequest.onErrorOccurred.addListener(onErrorOccurredListener, filter);
    webRequestListenersAttached = true;
    return { attached: true };
  } catch (e) {
    console.error("[ClaudeCodeBrowser] Could not attach webRequest listeners:", e);
    return { attached: false, error: e.message };
  }
}

function detachWebRequestListeners() {
  if (!webRequestListenersAttached) return;
  try {
    browser.webRequest.onBeforeRequest.removeListener(onBeforeRequestListener);
    browser.webRequest.onBeforeSendHeaders.removeListener(onBeforeSendHeadersListener);
    browser.webRequest.onHeadersReceived.removeListener(onHeadersReceivedListener);
    browser.webRequest.onBeforeRedirect.removeListener(onBeforeRedirectListener);
    browser.webRequest.onCompleted.removeListener(onCompletedListener);
    browser.webRequest.onErrorOccurred.removeListener(onErrorOccurredListener);
  } catch (e) {
    console.error("[ClaudeCodeBrowser] Could not detach webRequest listeners:", e);
  }
  webRequestListenersAttached = false;
  pendingNetworkRequests.clear();
  // Request ids are only removed from inFlightByTab by noteRequestFinished,
  // which cannot run once the listeners are gone. Leaving them behind made
  // every later wait_for_network_idle on that tab time out forever.
  inFlightByTab.clear();
}

function startNetworkLogging(tabId, options = {}) {
  if (parseFlag(options.clearExisting, false)) {
    networkLogsByTab.delete(tabId);
  }
  loggedTabs.set(tabId, {
    captureBodies: parseFlag(options.captureBodies, true),
    includeAllTypes: parseFlag(options.includeAllTypes, false)
  });
  return attachWebRequestListeners();
}

// Listeners come down only when nothing needs them any more.
function releaseWebRequestListenersIfIdle() {
  if (loggedTabs.size === 0 && idleWatchers.size === 0) {
    detachWebRequestListeners();
  }
}

function stopNetworkLogging(tabId) {
  loggedTabs.delete(tabId);
  releaseWebRequestListenersIfIdle();
}

// Wait for a tab's network to go quiet, counted at the network layer. The
// previous implementation wrapped the page's own fetch and XHR to do this;
// webRequest sees more (it catches fetch, which the sandbox hid) and touches
// nothing in the page.
async function waitForNetworkIdleOnTab(tabId, options = {}) {
  const timeout = options.timeout || 10000;
  const idleTime = options.idleTime || 500;
  const persistentAfter = options.persistentAfter || PERSISTENT_REQUEST_MS;
  const startedAt = Date.now();

  idleWatchers.set(tabId, (idleWatchers.get(tabId) || 0) + 1);
  const attached = attachWebRequestListeners();
  if (!attached.attached) {
    idleWatchers.delete(tabId);
    return { success: false, error: `Cannot observe network: ${attached.error}` };
  }

  // Nothing in flight yet still counts as activity, so a request that starts
  // a moment from now is not mistaken for silence.
  inFlightFor(tabId).lastActivity = Date.now();

  try {
    while (Date.now() - startedAt < timeout) {
      const state = inFlightFor(tabId);
      const pending = pendingRequestCount(tabId, persistentAfter);
      if (pending === 0 && Date.now() - state.lastActivity >= idleTime) {
        return { success: true, idle: true, waitedMs: Date.now() - startedAt,
                 pendingRequests: 0 };
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    return {
      success: true,
      idle: false,
      timedOut: true,
      waitedMs: Date.now() - startedAt,
      pendingRequests: pendingRequestCount(tabId, persistentAfter)
    };
  } finally {
    const remaining = (idleWatchers.get(tabId) || 1) - 1;
    if (remaining > 0) {
      idleWatchers.set(tabId, remaining);
    } else {
      idleWatchers.delete(tabId);
    }
    releaseWebRequestListenersIfIdle();
  }
}

function getNetworkLogsFor(tabId, options = {}) {
  let logs = [...(networkLogsByTab.get(tabId) || [])];

  const urlPattern = options.urlPattern || options.url_pattern;
  if (urlPattern) {
    const pattern = compilePattern(urlPattern, "url_pattern", "i");
    logs = logs.filter(log => pattern.test(log.url));
  }
  if (options.method) {
    logs = logs.filter(log => log.method?.toUpperCase() === options.method.toUpperCase());
  }
  if (options.status) {
    // Number(), because a JSON client sends status: "404" and a strict
    // comparison against the numeric status silently returned no logs.
    const wanted = Number(options.status);
    logs = logs.filter(log => log.status === wanted);
  }
  if (parseFlag(options.errorsOnly, false) || parseFlag(options.errors_only, false)) {
    logs = logs.filter(log => log.error || (log.status && log.status >= 400));
  }

  const total = (networkLogsByTab.get(tabId) || []).length;
  const limit = options.limit || 100;
  if (logs.length > limit) {
    logs = logs.slice(-limit);
  }

  return {
    success: true,
    logs: logs,
    totalCount: total,
    returnedCount: logs.length,
    loggingEnabled: loggedTabs.has(tabId),
    source: "webRequest",
    capturesFetch: true
  };
}

function clearNetworkLogs(tabId) {
  networkLogsByTab.delete(tabId);
}

// A logging session is scoped to the page it was started on, not to the tab
// id, which outlives navigation. Otherwise: start logging on a dev server,
// then type your bank's URL into that same tab, and its request and response
// bodies are captured by a session you started for something else.
const loggedOrigins = new Map();

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch (e) {
    return null;
  }
}

if (browser.webNavigation && browser.webNavigation.onCommitted) {
  browser.webNavigation.onCommitted.addListener((details) => {
    // Top-level navigations only; a subframe moving does not end the session.
    if (details.frameId !== 0) return;
    if (!loggedTabs.has(details.tabId)) return;

    const startedOn = loggedOrigins.get(details.tabId);
    const now = originOf(details.url);
    // Same page, same session. Anything else - a different origin, or an
    // origin we cannot determine - ends it: capturing a page the session was
    // not started for is the failure that matters, so unknown fails closed.
    if (startedOn && now && startedOn === now) return;

    console.warn(`[ClaudeCodeBrowser] stopping capture on tab ${details.tabId}: ` +
                 `navigated from ${startedOn} to ${now}`);
    stopNetworkLogging(details.tabId);
    loggedOrigins.delete(details.tabId);
    sendToContentScript(details.tabId, { action: "stopLogging" }).catch(() => {});
  });
}

// Don't keep logs for tabs that no longer exist.
browser.tabs.onRemoved.addListener((tabId) => {
  loggedTabs.delete(tabId);
  networkLogsByTab.delete(tabId);
  idleWatchers.delete(tabId);
  inFlightByTab.delete(tabId);
  loggedOrigins.delete(tabId);
  releaseWebRequestListenersIfIdle();
});

// Calculate exponential backoff delay
function getReconnectDelay() {
  const delay = Math.min(
    INITIAL_RECONNECT_DELAY * Math.pow(1.5, reconnectAttempts),
    MAX_RECONNECT_DELAY
  );
  return delay;
}

// Connect to native messaging host
function connectNativeHost() {
  if (isConnected && nativePort) {
    console.log("[ClaudeCodeBrowser] Already connected");
    return;
  }

  try {
    console.log(`[ClaudeCodeBrowser] Connecting to native host (attempt ${reconnectAttempts + 1})...`);
    nativePort = browser.runtime.connectNative(NATIVE_HOST_NAME);
    isConnected = true;
    reconnectAttempts = 0;  // Reset on successful connection
    console.log("[ClaudeCodeBrowser] Connected to native host");

    nativePort.onMessage.addListener(handleNativeMessage);
    nativePort.onDisconnect.addListener(handleDisconnect);
  } catch (error) {
    console.error("[ClaudeCodeBrowser] Failed to connect to native host:", error);
    isConnected = false;
    nativePort = null;
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    console.error(`[ClaudeCodeBrowser] Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached. Giving up.`);
    // Reset after a long delay to try again eventually
    setTimeout(() => {
      reconnectAttempts = 0;
      connectNativeHost();
    }, 60000);  // Try again after 1 minute
    return;
  }

  const delay = getReconnectDelay();
  reconnectAttempts++;
  console.log(`[ClaudeCodeBrowser] Reconnecting in ${delay}ms (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
  setTimeout(connectNativeHost, delay);
}

function handleDisconnect(port) {
  console.log("[ClaudeCodeBrowser] Disconnected from native host");
  if (port.error) {
    console.error("[ClaudeCodeBrowser] Disconnect error:", port.error);
  }
  isConnected = false;
  nativePort = null;

  // Reject all pending requests with a descriptive error
  for (const [id, { reject }] of pendingRequests) {
    reject(new Error("Native host disconnected - reconnecting..."));
  }
  pendingRequests.clear();

  // Schedule reconnection with exponential backoff
  scheduleReconnect();
}

function handleNativeMessage(message) {
  console.log("[ClaudeCodeBrowser] Received from native host:", message);

  if (message.requestId && pendingRequests.has(message.requestId)) {
    const { resolve, reject } = pendingRequests.get(message.requestId);
    pendingRequests.delete(message.requestId);

    if (message.error) {
      reject(new Error(message.error));
    } else {
      resolve(message);
    }
  } else if (message.action) {
    // Handle incoming commands from native host
    handleCommand(message);
  }
}

function sendToNativeHost(message, retryCount = 0) {
  const MAX_RETRIES = 3;
  const RETRY_DELAY = 1000;

  return new Promise((resolve, reject) => {
    // Try to connect if not connected
    if (!isConnected || !nativePort) {
      connectNativeHost();

      // Wait a bit for connection to establish
      setTimeout(() => {
        if (!isConnected || !nativePort) {
          if (retryCount < MAX_RETRIES) {
            console.log(`[ClaudeCodeBrowser] Not connected, retrying (${retryCount + 1}/${MAX_RETRIES})...`);
            setTimeout(() => {
              sendToNativeHost(message, retryCount + 1)
                .then(resolve)
                .catch(reject);
            }, RETRY_DELAY);
          } else {
            reject(new Error("Not connected to native host after retries"));
          }
          return;
        }

        // Now connected, send the message
        doSend();
      }, 500);
      return;
    }

    doSend();

    function doSend() {
      const requestId = ++requestCounter;
      message.requestId = requestId;
      pendingRequests.set(requestId, { resolve, reject });

      // Timeout after 30 seconds
      const timeoutId = setTimeout(() => {
        if (pendingRequests.has(requestId)) {
          pendingRequests.delete(requestId);
          reject(new Error("Request timeout"));
        }
      }, 30000);

      // Store timeout for potential cleanup
      pendingRequests.get(requestId).timeoutId = timeoutId;

      try {
        nativePort.postMessage(message);
      } catch (error) {
        pendingRequests.delete(requestId);
        clearTimeout(timeoutId);

        // Connection might have died, try to reconnect and retry
        if (retryCount < MAX_RETRIES) {
          console.log(`[ClaudeCodeBrowser] Send failed, reconnecting and retrying...`);
          isConnected = false;
          nativePort = null;
          setTimeout(() => {
            sendToNativeHost(message, retryCount + 1)
              .then(resolve)
              .catch(reject);
          }, RETRY_DELAY);
        } else {
          reject(error);
        }
      }
    }
  });
}

// Handle commands from native host
async function handleCommand(message) {
  const { action, tabId, data } = message;
  let result = { success: false };

  try {
    switch (action) {
      case "screenshot":
        result = await takeScreenshot(tabId, data);
        break;
      case "click":
        result = await performClick(tabId, data);
        break;
      case "type":
        result = await performType(tabId, data);
        break;
      case "scroll":
        result = await performScroll(tabId, data);
        break;
      case "navigate":
        result = await navigateTo(tabId, data);
        break;
      case "getPageInfo":
        result = await getPageInfo(tabId);
        break;
      case "getElements":
        result = await getElements(tabId, data);
        break;
      case "executeScript":
        result = await executeScript(tabId, data);
        break;
      case "highlight":
        result = await highlightElement(tabId, data);
        break;
      case "waitForElement":
        result = await waitForElement(tabId, data);
        break;
      case "getTabs":
        result = await getAllTabs(data);
        break;
      case "createTab":
        result = await createNewTab(data);
        break;
      case "closeTab":
        result = await closeTab(tabId);
        break;
      case "focusTab":
        result = await focusTab(tabId);
        break;
      case "getTabInfo":
        result = await getTabInfo(tabId);
        break;
      case "findTabs":
        result = await findTabs(data);
        break;
      case "screenshotAllTabs":
        result = await screenshotAllTabs(data);
        break;
      case "refresh":
      case "reload":
        result = await refreshTab(tabId, data);
        break;
      case "hardRefresh":
        result = await hardRefreshTab(tabId);
        break;
      case "reloadAll":
        result = await reloadAllTabs(data);
        break;
      case "reloadByUrl":
        result = await reloadTabsByUrl(data);
        break;
      case "goBack":
        result = await navigateHistory(tabId, "back");
        break;
      case "goForward":
        result = await navigateHistory(tabId, "forward");
        break;
      case "requestApproval":
        result = await requestApproval(tabId, data);
        break;
      case "solveCaptcha":
        result = await solveCaptcha(tabId, data);
        break;
      // Element interaction and dynamic-content commands implemented by the
      // content script — forwarded as-is
      case "waitForNetworkIdle":
        result = await waitForNetworkIdle(tabId, data);
        break;
      // Element interaction and dynamic-content commands implemented by the
      // content script — forwarded as-is
      case "getValue":
      case "setValue":
      case "selectOption":
      case "hover":
      case "getAttribute":
      case "focus":
      case "getComputedStyles":
      case "getBoundingRect":
      case "waitForChange":
      case "observeElement":
      case "stopObserving":
      case "scrollAndCapture":
      case "clickAndWait":
      case "pressKey":
      case "getText":
        result = await sendToContentScript(tabId, { action, ...data });
        break;
      // Logging. Console output only exists inside the page, so the content
      // script still handles it; network traffic is captured here.
      case "startLogging":
        result = await startLogging(tabId, data);
        break;
      case "stopLogging":
        result = await stopLogging(tabId);
        break;
      case "getConsoleLogs":
        result = await sendToContentScript(tabId, { action: "getConsoleLogs", ...data });
        break;
      case "getNetworkLogs":
        result = await getNetworkLogs(tabId, data);
        break;
      case "clearLogs":
        result = await clearLogs(tabId, data);
        break;
      default:
        result = { success: false, error: `Unknown action: ${action}` };
    }
  } catch (error) {
    result = { success: false, error: error.message };
  }

  // Send result back to native host
  if (nativePort && message.requestId) {
    nativePort.postMessage({
      requestId: message.requestId,
      ...result
    });
  }

  return result;
}

// Screenshot functionality
async function takeScreenshot(tabId, options = {}) {
  try {
    const currentTab = (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    const targetTab = await tabFor(tabId);

    // Check if we need to temporarily focus the tab for screenshot.
    // Compared on the resolved tab, not the raw argument: `tabId &&` was
    // falsy for tab id 0, and a numeric string never equalled the id.
    const needsFocus = targetTab && targetTab.id !== currentTab?.id;
    const mayFocus = parseFlag(options.allowFocus, true);
    let originalActiveTab = null;

    // captureVisibleTab photographs the window's ACTIVE tab, so without
    // focusing first there is no way to capture a background one. This used
    // to return the active tab's image labelled with the REQUESTED tab's id,
    // url and title, and wasFocused: true - so a screenshot of the user's
    // open mail was filed on disk as a screenshot of some other page. Refuse
    // instead: a wrong image presented as the right one is worse than an
    // error, and allow_focus exists to say "do not disturb my browsing".
    if (needsFocus && !mayFocus) {
      return {
        success: false,
        error: `Cannot screenshot tab ${targetTab.id} without focusing it: ` +
               'Firefox captures the active tab of a window, so the image ' +
               'would be of whichever tab is in front. Either allow focus, ' +
               'or focus the tab yourself first with browser_focus_tab.'
      };
    }

    if (needsFocus && mayFocus) {
      // Store original active tab to restore later
      originalActiveTab = currentTab;

      // Focus the target tab temporarily
      await browser.tabs.update(targetTab.id, { active: true });

      // Small delay to let the tab render
      await new Promise(resolve => setTimeout(resolve, 150));
    }

    try {
      const dataUrl = await browser.tabs.captureVisibleTab(targetTab.windowId, {
        format: options.format || "png",
        quality: options.quality || 90
      });

      // full_page has never worked: captureFullPage only ever returned the
      // page's dimensions, which the server then tried to treat as a data
      // URL and failed on with an AttributeError. A real implementation needs
      // scroll-and-stitch, which belongs in a separate tool rather than a
      // flag that silently means something else. Report the visible capture
      // and say plainly that the flag was not honoured.
      if (parseFlag(options.fullPage, false)) {
        const metrics = await browser.tabs.sendMessage(targetTab.id, {
          action: "captureFullPage"
        }, { frameId: 0 }).catch(() => null);
        return {
          success: true,
          data: dataUrl,
          type: "visible",
          fullPageRequested: true,
          fullPageCaptured: false,
          note: "full_page is not supported in attended Firefox: this is the " +
                "visible viewport only. Use browser_scroll_and_capture to walk " +
                "the page, or the headless backend, which captures full pages.",
          pageMetrics: metrics,
          tab: { id: targetTab.id, url: targetTab.url, title: targetTab.title },
          privateWindow: targetTab.incognito === true,
          wasFocused: needsFocus
        };
      }

      return {
        success: true,
        data: dataUrl,
        type: "visible",
        tab: { id: targetTab.id, url: targetTab.url, title: targetTab.title },
        // The image still goes to the agent, which is what the caller asked
        // for, but saying where it came from lets the server decline to write
        // it to disk for a week. Private windows exist so that a record of
        // them is not left behind.
        privateWindow: targetTab.incognito === true,
        wasFocused: needsFocus
      };
    } finally {
      // Restore original tab if we changed focus
      if (originalActiveTab && parseFlag(options.restoreFocus, true)) {
        await browser.tabs.update(originalActiveTab.id, { active: true });
      }
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Screenshot all tabs (cycles through them)
async function screenshotAllTabs(options = {}) {
  try {
    const tabs = await browser.tabs.query({});
    const currentTab = (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    const results = [];

    // Filter tabs by URL pattern if provided. Private windows are skipped:
    // this is a bulk sweep the caller did not aim at any particular tab, and
    // the screenshot is written to disk and kept, so it would be a
    // longer-lived record of a private window than the network buffer that
    // startLogging already refuses.
    let targetTabs = tabs.filter(t => !t.url.startsWith('about:') &&
                                      !t.url.startsWith('moz-extension:') &&
                                      t.incognito !== true);

    if (options.urlPattern) {
      const regex = compilePattern(options.urlPattern, "url_pattern");
      targetTabs = targetTabs.filter(t => regex.test(t.url));
    }

    for (const tab of targetTabs) {
      try {
        // Focus the tab
        await browser.tabs.update(tab.id, { active: true });
        await new Promise(resolve => setTimeout(resolve, 200));

        // Take screenshot
        const dataUrl = await browser.tabs.captureVisibleTab(tab.windowId, {
          format: options.format || "png",
          quality: options.quality || 90
        });

        results.push({
          success: true,
          tabId: tab.id,
          url: tab.url,
          title: tab.title,
          data: parseFlag(options.includeData, false) ? dataUrl : undefined,
          timestamp: Date.now()
        });
      } catch (e) {
        results.push({
          success: false,
          tabId: tab.id,
          url: tab.url,
          error: e.message
        });
      }
    }

    // Restore original tab
    if (currentTab) {
      await browser.tabs.update(currentTab.id, { active: true });
    }

    return {
      success: true,
      screenshots: results,
      count: results.filter(r => r.success).length,
      failed: results.filter(r => !r.success).length
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Human approval.
//
// The decision is taken in an extension page in its own window, not in the
// page being automated. A content-script banner lives in the page's own DOM:
// the page can restyle it away, read it, and dispatch a click on it, so a
// site could approve its own protected action. An extension page is a
// moz-extension:// document the page cannot touch at all.
//
// Firefox does not support buttons on notifications (only type, title,
// message and iconUrl), so the notification remains an attention-getter and
// the window carries the decision.
const pendingApprovals = new Map();
let approvalCounter = 0;

async function requestApproval(tabId, data = {}) {
  try {
    if (browser.notifications) {
      await browser.notifications.create({
        type: "basic",
        title: "Claude requests approval",
        message: (data.message || "Claude wants to perform an action").slice(0, 200),
        iconUrl: browser.runtime.getURL("icons/icon-48.png")
      });
    }
  } catch (e) {
    // Notifications unavailable — the window is what matters.
  }

  const viaWindow = await requestApprovalInWindow(data);
  if (viaWindow) return viaWindow;

  // Fallback: no window could be opened (no window manager, kiosk, headless
  // Firefox). The in-page banner is weaker - the page shares the DOM with it -
  // so say so in the result rather than letting it pass as equivalent.
  const inPage = await sendToContentScript(tabId, { action: "requestApproval", ...data });
  return { ...inPage, promptSurface: "page", degraded: true };
}

async function requestApprovalInWindow(data) {
  const requestId = `approval_${++approvalCounter}_${Date.now()}`;
  const timeout = data.timeout || 60000;

  return new Promise((resolve) => {
    let settled = false;
    const finish = (payload) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const record = pendingApprovals.get(requestId);
      pendingApprovals.delete(requestId);
      if (record && record.windowId !== undefined && !payload.closedWithoutAnswering) {
        browser.windows.remove(record.windowId).catch(() => {});
      }
      resolve(payload);
    };

    const timer = setTimeout(() => finish({
      success: true, approved: false, timedOut: true,
      promptSurface: "window", decidedAt: new Date().toISOString()
    }), timeout + 2000);

    pendingApprovals.set(requestId, {
      heading: "Claude requests approval",
      message: data.message || "Claude wants to perform an action.",
      detail: data.detail || "",
      protectedUrl: data.protectedUrl || "",
      timeout,
      finish
    });

    const url = browser.runtime.getURL(
      `approve/approve.html?id=${encodeURIComponent(requestId)}`);

    browser.windows.create({
      url,
      type: "popup",
      width: 640,
      height: 520
    }).then((win) => {
      const record = pendingApprovals.get(requestId);
      if (record) record.windowId = win.id;
    }).catch((error) => {
      // Could not open a window: let the caller fall back.
      clearTimeout(timer);
      pendingApprovals.delete(requestId);
      settled = true;
      console.warn("[ClaudeCodeBrowser] approval window unavailable:", error);
      resolve(null);
    });
  });
}

// The approval page asks for its details and reports the decision.
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== "approval") return;

  // Only our own extension pages may answer an approval, and never a tab: a
  // content script must not be able to decide one.
  if (sender.id !== browser.runtime.id || sender.tab) {
    console.warn("[ClaudeCodeBrowser] refused approval message from", sender.id);
    sendResponse({ found: false });
    return;
  }

  const record = pendingApprovals.get(message.requestId);

  if (message.action === "details") {
    if (!record) {
      sendResponse({ found: false });
      return;
    }
    sendResponse({
      found: true,
      heading: record.heading,
      message: record.message,
      detail: record.detail,
      protectedUrl: record.protectedUrl,
      timeout: record.timeout
    });
    return;
  }

  if (message.action === "decide" && record) {
    record.finish({
      success: true,
      approved: message.approved === true,
      closedWithoutAnswering: message.closed === true,
      promptSurface: "window",
      decidedAt: new Date().toISOString()
    });
    sendResponse({ ok: true });
  }
});

// Captcha handoff: notify the human (unless it's a detect-only probe), then
// let the content script show the solve banner and wait for completion
async function solveCaptcha(tabId, data = {}) {
  if (!parseFlag(data.detectOnly, false)) {
    try {
      if (browser.notifications) {
        await browser.notifications.create({
          type: "basic",
          title: "Captcha needs solving",
          message: "Claude paused on a captcha and needs you to solve it.",
          iconUrl: browser.runtime.getURL("icons/icon-48.png")
        });
      }
    } catch (e) {
      // Notifications unavailable — the in-page banner still works
    }
  }
  return sendToContentScript(tabId, { action: "solveCaptcha", ...data });
}

// History navigation (Back/Forward buttons)
async function navigateHistory(tabId, direction) {
  try {
    const tab = await tabFor(tabId);
    if (direction === "back") {
      await browser.tabs.goBack(tab.id);
    } else {
      await browser.tabs.goForward(tab.id);
    }
    // Give the navigation a moment to commit before reporting the new URL
    await new Promise(resolve => setTimeout(resolve, 300));
    const updated = await browser.tabs.get(tab.id);
    return { success: true, url: updated.url, title: updated.title };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Resolve an optional tab id to a concrete one (the active tab when omitted).
// A caller-supplied pattern is data. new RegExp on an unbalanced "(" threw a
// parser message as the tool's whole error, which reads like an internal
// fault rather than "that pattern is not valid".
function compilePattern(pattern, argumentName, flags) {
  try {
    return new RegExp(pattern, flags);
  } catch (e) {
    throw new Error(
      `${argumentName} is not a valid regular expression: ${e.message}`);
  }
}

// Resolve a tab argument, accepting the id 0 and a numeric string. Thirteen
// call sites used `tabId ? await browser.tabs.get(tabId) : activeTab`, which
// is falsy for tab id 0 - so a request naming that tab silently acted on
// whichever tab happened to be in front.
async function tabFor(tabId) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    throw new Error("No tab to act on");
  }
  return await browser.tabs.get(resolved);
}

async function resolveTabId(tabId) {
  if (tabId !== undefined && tabId !== null && tabId !== '') {
    // A JSON client sends tab_id: "7", which browser.tabs.get rejects.
    const asNumber = Number(tabId);
    return Number.isInteger(asNumber) ? asNumber : tabId;
  }
  const active = (await browser.tabs.query({ active: true, currentWindow: true }))[0];
  return active?.id;
}

// Logging spans both halves of the extension: network capture lives here,
// console capture in the content script. Both are started and stopped
// together so the tools keep behaving as one switch.
async function startLogging(tabId, data = {}) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    return { success: false, error: "No tab to log" };
  }

  if (await isPrivateTab(resolved)) {
    return {
      success: false,
      error: "Refusing to log a private-browsing tab: its request and " +
             "response bodies would be retained in a buffer the agent reads. " +
             "Private windows exist so that does not happen."
    };
  }

  // Remember which origin this session belongs to, so navigating away ends it.
  try {
    const tab = await browser.tabs.get(resolved);
    const origin = originOf(tab.url);
    if (origin) loggedOrigins.set(resolved, origin);
  } catch (e) {
    // Tab went away; startNetworkLogging below will still be harmless.
  }

  const network = startNetworkLogging(resolved, data);
  const console_ = await sendToContentScript(resolved, { action: "startLogging", ...data });

  return {
    success: true,
    message: "Logging started",
    tabId: resolved,
    network: {
      capturing: network.attached,
      capturesFetch: network.attached,
      captureBodies: parseFlag(data.captureBodies, true),
      error: network.error
    },
    console: {
      // What a content script can actually see: page errors and unhandled
      // rejections, plus the extension's own output. NOT the page's console -
      // that lives in a world the content script has no access to.
      capturing: console_.success === true,
      capturesPageConsole: false,
      capturesPageErrors: console_.success === true,
      error: console_.success === true ? undefined : console_.error
    }
  };
}

async function stopLogging(tabId) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    return { success: false, error: "No tab to stop logging for" };
  }

  stopNetworkLogging(resolved);
  const console_ = await sendToContentScript(resolved, { action: "stopLogging" });

  return {
    success: true,
    message: "Logging stopped",
    tabId: resolved,
    networkLogsCount: (networkLogsByTab.get(resolved) || []).length,
    consoleLogsCount: console_.consoleLogsCount
  };
}

async function waitForNetworkIdle(tabId, data = {}) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    return { success: false, error: "No tab to wait on" };
  }
  return waitForNetworkIdleOnTab(resolved, data);
}

async function getNetworkLogs(tabId, data = {}) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    return { success: false, error: "No tab to read logs for" };
  }
  return getNetworkLogsFor(resolved, data);
}

async function clearLogs(tabId, data = {}) {
  const resolved = await resolveTabId(tabId);
  if (resolved === undefined) {
    return { success: false, error: "No tab to clear logs for" };
  }
  if (parseFlag(data.network, true)) {
    clearNetworkLogs(resolved);
  }
  if (parseFlag(data.console, true)) {
    await sendToContentScript(resolved, { action: "clearLogs", ...data });
  }
  return { success: true, message: "Logs cleared", tabId: resolved };
}

// Generic helper to send message to content script.
//
// Targets the top frame. The content script runs in every frame
// (all_frames: true), and tabs.sendMessage with no frameId delivers to all of
// them and resolves with whichever answers first -- so an ad or payment iframe
// could answer getText, getPageInfo, or an approval prompt on the page's
// behalf. Pass allFrames: true to opt into the old broadcast.
async function sendToContentScript(tabId, message, { allFrames = false } = {}) {
  try {
    const tab = await tabFor(tabId);
    const options = allFrames ? undefined : { frameId: 0 };
    const result = await browser.tabs.sendMessage(tab.id, message, options);
    return await contentScriptResult(result, message.action, tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// A content script that exists but returns neither true nor a Promise
// resolves the sender's promise with undefined, and `{ success: true,
// ...undefined }` reported success for work that never happened - getText
// returned {success:true} with no text, click reported a click nobody made,
// and startLogging reported console.capturing: true. A missing receiver
// rejects instead, which is already handled; this is the receiver that is
// there but silent, which includes every action the content script's handler
// map does not cover.
async function contentScriptResult(result, action, tabId) {
  if (result === undefined || result === null) {
    return {
      success: false,
      error: `The content script did not answer "${action}". It may not ` +
             `handle that action, or the page may have navigated away.`
    };
  }
  // Re-read the tab's URL after the action. The server's safety guard tracks
  // the current page from tool results, and a click result carried no url at
  // all, so the guard went on judging the next action against whatever page
  // it last heard about. A result that reports its own url keeps it.
  //
  // What this does NOT do: performClick returns as soon as the click is
  // dispatched, so a navigation the click started is usually still in flight
  // and this read returns the pre-navigation URL. It fixes the case where the
  // URL has already changed - a same-document route change, or a navigation
  // the guard simply never heard about - and narrows the window in the rest.
  // browser_click_and_wait is the tool that waits, and the guard is updated
  // from its result too.
  let url;
  try {
    if (tabId !== undefined) url = (await browser.tabs.get(tabId)).url;
  } catch (e) {
    // Tab closed between the action and this read; the caller still gets its
    // result, and the guard is no worse off than before.
  }
  return url ? { success: true, url, ...result } : { success: true, ...result };
}

// Click functionality
async function performClick(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "click",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "click", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Type functionality
async function performType(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "type",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "type", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Scroll functionality
async function performScroll(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "scroll",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "scroll", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Navigation
async function navigateTo(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    await browser.tabs.update(tab.id, { url: data.url });

    // Wait for page to load
    return new Promise((resolve) => {
      const listener = async (updatedTabId, changeInfo) => {
        if (updatedTabId === tab.id && changeInfo.status === "complete") {
          browser.tabs.onUpdated.removeListener(listener);
          // Report where the tab actually landed, not what was requested. A
          // redirect meant the safety guard's URL tracker recorded the
          // short link and not the destination, so later actions on the
          // destination were checked against the wrong page.
          let landedUrl = data.url;
          try {
            const updated = await browser.tabs.get(tab.id);
            landedUrl = updated.url || data.url;
          } catch (e) {
            // Tab closed during navigation; fall back to the requested URL.
          }
          resolve({ success: true, url: landedUrl, requestedUrl: data.url });
        }
      };
      browser.tabs.onUpdated.addListener(listener);

      // Timeout after 30 seconds
      setTimeout(() => {
        browser.tabs.onUpdated.removeListener(listener);
        resolve({ success: true, url: data.url, note: "Navigation initiated but completion not confirmed" });
      }, 30000);
    });
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Get page information
async function getPageInfo(tabId) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "getPageInfo"
    }, { frameId: 0 });

    return {
      success: true,
      tab: { id: tab.id, url: tab.url, title: tab.title },
      ...result
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Get elements by selector
async function getElements(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "getElements",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "getElements", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Execute arbitrary script
async function executeScript(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.executeScript(tab.id, {
      code: data.script
    });

    return { success: true, result: result[0] };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Highlight element
async function highlightElement(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "highlight",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "highlight", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Wait for element
async function waitForElement(tabId, data) {
  try {
    const tab = await tabFor(tabId);

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "waitForElement",
      ...data
    }, { frameId: 0 });

    return await contentScriptResult(result, "waitForElement", tab.id);
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Tab management
// A caller-supplied result cap, clamped. Number.isInteger refused the string
// "1" that a JSON client sends and silently fell back to 50 - so a caller
// asking for one tab got all of them - and nothing stopped limit: 100000,
// which defeated the cap these tools exist to enforce.
const MAX_TAB_RESULTS = 50;

function tabResultLimit(raw, fallback = MAX_TAB_RESULTS) {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(Math.floor(parsed), MAX_TAB_RESULTS);
}

async function getAllTabs(options = {}) {
  try {
    // Default scope: the window Claude is actually driving, not every
    // window/tab in the user's browser. With dozens of tabs open, querying
    // {} and returning full metadata per tab blows past response size
    // limits. Opt into the wider view explicitly when needed.
    // The server's camelize_args() rewrites these keys before dispatch, so
    // both spellings have to be accepted or the options are silently dropped.
    const currentWindowOnlyOpt = options.currentWindowOnly !== undefined
      ? options.currentWindowOnly : options.current_window_only;
    const currentWindowOnly = parseFlag(currentWindowOnlyOpt, true);
    const includeFavicon = parseFlag(options.includeFavicon, false)
      || parseFlag(options.include_favicon, false);
    const rawPattern = options.urlPattern || options.url_pattern;
    const urlPattern = rawPattern ? compilePattern(rawPattern, "url_pattern") : null;
    const limit = tabResultLimit(options.limit);

    const queryOpts = currentWindowOnly ? { currentWindow: true } : {};
    let tabs = await browser.tabs.query(queryOpts);
    const windows = await browser.windows.getAll();
    const windowMap = new Map(windows.map(w => [w.id, w]));

    const totalMatched = urlPattern ? tabs.filter(t => urlPattern.test(t.url)).length : tabs.length;
    if (urlPattern) {
      tabs = tabs.filter(t => urlPattern.test(t.url));
    }

    const truncated = tabs.length > limit;
    const pageTabs = tabs.slice(0, limit);

    return {
      success: true,
      tabs: pageTabs.map(t => {
        const win = windowMap.get(t.windowId);
        const base = {
          id: t.id,
          url: t.url,
          title: t.title,
          active: t.active,
          windowId: t.windowId,
          pinned: t.pinned,
          status: t.status,  // "loading" or "complete"
          audible: t.audible  // playing audio
        };
        if (includeFavicon) {
          base.favIconUrl = t.favIconUrl;
        }
        if (!currentWindowOnly) {
          base.windowFocused = win?.focused || false;
        }
        return base;
      }),
      returnedTabs: pageTabs.length,
      truncated,
      scope: currentWindowOnly ? 'current_window' : 'all_windows',
      windowCount: windows.length,
      totalTabs: totalMatched,
      summary: {
        active: tabs.filter(t => t.active).length,
        loading: tabs.filter(t => t.status === 'loading').length,
        discarded: tabs.filter(t => t.discarded).length,
        audible: tabs.filter(t => t.audible).length
      }
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Get detailed info about a specific tab
async function getTabInfo(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    const win = await browser.windows.get(tab.windowId);

    // Try to get page info from content script
    let pageInfo = null;
    try {
      pageInfo = await browser.tabs.sendMessage(tabId, { action: "getPageInfo" },
                                                { frameId: 0 });
    } catch (e) {
      // Content script may not be loaded
      pageInfo = { error: "Content script not available" };
    }

    return {
      success: true,
      tab: {
        id: tab.id,
        url: tab.url,
        title: tab.title,
        active: tab.active,
        windowId: tab.windowId,
        index: tab.index,
        pinned: tab.pinned,
        status: tab.status,
        discarded: tab.discarded,
        audible: tab.audible,
        favIconUrl: tab.favIconUrl
      },
      window: {
        id: win.id,
        focused: win.focused,
        state: win.state,
        type: win.type
      },
      pageInfo: pageInfo
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Find tabs by URL pattern
async function findTabs(options = {}) {
  try {
    // active: false and audible: false matched essentially every tab, so one
    // boolean defeated the filter requirement entirely and returned the
    // user's whole browsing surface. Only a filter that actually narrows
    // counts: a URL, a title, or one of the booleans set to true.
    const hasNarrowingFilter =
      ['url', 'urlPattern', 'url_pattern', 'title']
        .some(key => options[key]) ||
      ['active', 'audible']
        .some(key => options[key] !== undefined && parseFlag(options[key], false));
    if (!hasNarrowingFilter) {
      // With no filter this returned every tab in every window, which is the
      // user's whole browsing surface handed over by a tool that reads like
      // a search. Make that an explicit request.
      return {
        success: false,
        error: "browser_find_tabs needs a filter that narrows the result: " +
               "url, url_pattern, title, or active/audible set to true. " +
               "active: false matches almost every tab, so it is not a " +
               "filter. To list tabs deliberately, use browser_get_tabs."
      };
    }

    const tabs = await browser.tabs.query({});
    let filtered = tabs;

    if (options.url) {
      filtered = filtered.filter(t => t.url.startsWith(options.url));
    }
    if (options.urlPattern) {
      const regex = compilePattern(options.urlPattern, "url_pattern");
      filtered = filtered.filter(t => regex.test(t.url));
    }
    if (options.title) {
      const titleLower = options.title.toLowerCase();
      filtered = filtered.filter(t => t.title?.toLowerCase().includes(titleLower));
    }
    // parseFlag, because active: "true" from a JSON client matched no tab at
    // all under a strict comparison.
    if (options.active !== undefined) {
      const want = parseFlag(options.active, true);
      filtered = filtered.filter(t => (t.active === true) === want);
    }
    if (options.audible !== undefined) {
      const want = parseFlag(options.audible, true);
      filtered = filtered.filter(t => (t.audible === true) === want);
    }

    // Cap it, like browser_get_tabs already does, and say when it bit.
    const limit = tabResultLimit(options.limit);
    const matched = filtered.length;
    const page = filtered.slice(0, limit);

    return {
      success: true,
      tabs: page.map(t => ({
        id: t.id,
        url: t.url,
        title: t.title,
        active: t.active,
        windowId: t.windowId,
        status: t.status
      })),
      count: page.length,
      totalMatched: matched,
      truncated: matched > page.length
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

async function createNewTab(data) {
  try {
    const tab = await browser.tabs.create({
      url: data.url || "about:blank",
      active: parseFlag(data.active, true)
    });
    return {
      success: true,
      tab: { id: tab.id, url: tab.url, title: tab.title }
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

async function closeTab(tabId) {
  try {
    await browser.tabs.remove(tabId);
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

async function focusTab(tabId) {
  try {
    const tab = await browser.tabs.update(tabId, { active: true });
    await browser.windows.update(tab.windowId, { focused: true });
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Refresh/Reload functionality
async function refreshTab(tabId, options = {}) {
  try {
    const tab = await tabFor(tabId);

    // bypassCache: true = hard refresh (Ctrl+Shift+R), false = normal refresh
    // (F5). Parsed once and used for both the reload and the report: the
    // reload took `|| false` while the result reported parseFlag(..., true),
    // so the two disagreed and the reported field was a lie. The schema's
    // default for browser_refresh is false.
    const bypassCache = parseFlag(options.bypassCache, false);
    await browser.tabs.reload(tab.id, { bypassCache });

    // Wait for page to load if requested. parseFlag, because wait_for_load
    // arrives as the string "false" from a JSON client: `!== false` was true
    // for it, so declining to wait made browser_refresh block for the full
    // 30-second timeout instead of returning at once.
    if (parseFlag(options.waitForLoad, true)) {
      return new Promise((resolve) => {
        const listener = (updatedTabId, changeInfo) => {
          if (updatedTabId === tab.id && changeInfo.status === "complete") {
            browser.tabs.onUpdated.removeListener(listener);
            resolve({
              success: true,
              refreshed: true,
              tab: { id: tab.id, url: tab.url, title: tab.title },
              bypassCache
            });
          }
        };
        browser.tabs.onUpdated.addListener(listener);

        // Timeout after 30 seconds
        setTimeout(() => {
          browser.tabs.onUpdated.removeListener(listener);
          resolve({ success: true, refreshed: true, note: "Refresh initiated but completion not confirmed" });
        }, 30000);
      });
    }

    return { success: true, refreshed: true, bypassCache,
             tab: { id: tab.id, url: tab.url } };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Hard refresh - bypass cache (like Ctrl+Shift+R)
async function hardRefreshTab(tabId) {
  return refreshTab(tabId, { bypassCache: true });
}

// Reload all tabs (useful when restarting dev servers)
async function reloadAllTabs(options = {}) {
  try {
    const tabs = await browser.tabs.query({});
    const results = [];

    for (const tab of tabs) {
      // Skip special browser pages
      if (tab.url.startsWith('about:') || tab.url.startsWith('moz-extension:')) {
        continue;
      }

      // Filter by URL pattern if provided
      if (options.urlPattern) {
        const regex = compilePattern(options.urlPattern, "url_pattern");
        if (!regex.test(tab.url)) {
          continue;
        }
      }

      // The schema documents bypass_cache as defaulting to true, and the
      // tool exists for picking up a restarted dev server, where a cached
      // response is the thing you are trying to avoid.
      await browser.tabs.reload(tab.id, { bypassCache: parseFlag(options.bypassCache, true) });
      results.push({ id: tab.id, url: tab.url, reloaded: true });
    }

    return {
      success: true,
      reloadedCount: results.length,
      tabs: results,
      bypassCache: parseFlag(options.bypassCache, true)
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Reload tabs matching a specific URL or pattern (great for dev servers)
async function reloadTabsByUrl(options) {
  try {
    if (!options.url && !options.urlPattern) {
      return { success: false, error: "Must provide url or urlPattern" };
    }

    const tabs = await browser.tabs.query({});
    const results = [];

    for (const tab of tabs) {
      let matches = false;

      if (options.url) {
        // Exact URL match or starts with
        matches = tab.url === options.url || tab.url.startsWith(options.url);
      } else if (options.urlPattern) {
        // Regex pattern match
        const regex = compilePattern(options.urlPattern, "url_pattern");
        matches = regex.test(tab.url);
      }

      if (matches) {
        await browser.tabs.reload(tab.id, { bypassCache: parseFlag(options.bypassCache, true) });
        results.push({ id: tab.id, url: tab.url, reloaded: true });
      }
    }

    return {
      success: true,
      reloadedCount: results.length,
      tabs: results,
      bypassCache: parseFlag(options.bypassCache, true)
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Listen for messages from content scripts
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object") return;
  if (message.target === "background") {
    handleCommand({ ...message, tabId: sender.tab?.id })
      .then(sendResponse);
    return true; // Keep channel open for async response
  }
});

// Refuse external messages. Nothing legitimate uses this path — the MCP
// server reaches the extension via native messaging and HTTP polling, never
// runtime.sendMessage — and an open forward here would let any co-installed
// extension run arbitrary commands (including executeScript on any tab),
// bypassing the API token and the localhost boundary entirely.
browser.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  console.warn("[ClaudeCodeBrowser] Refused external message from", sender?.id);
  sendResponse({ success: false, error: "External messages are not accepted" });
  return false;
});

// Context menu for quick actions
browser.contextMenus.create({
  id: "claude-screenshot",
  title: "Take Screenshot for Claude",
  contexts: ["page"]
});

browser.contextMenus.create({
  id: "claude-inspect",
  title: "Inspect Element for Claude",
  contexts: ["all"]
});

browser.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "claude-screenshot") {
    takeScreenshot(tab.id).then(result => {
      if (!result.success || !nativePort) return;
      // Not from a private window. The native host writes the file itself,
      // so the server's refusal to persist a private-window screenshot does
      // not cover this path.
      if (result.privateWindow === true) {
        console.warn("[ClaudeCodeBrowser] not saving a screenshot of a " +
                     "private window");
        return;
      }
      // "saveScreenshot", not "screenshotTaken": the native host has no
      // handler for the latter, so the message fell through to the dead
      // /browser/command endpoint and this menu item did nothing at all.
      nativePort.postMessage({
        action: "saveScreenshot",
        data: result.data,
        tab: { id: tab.id, url: tab.url, title: tab.title }
      });
    });
  } else if (info.menuItemId === "claude-inspect") {
    browser.tabs.sendMessage(tab.id, {
      action: "inspectElement",
      x: info.pageX,
      y: info.pageY
    });
  }
});

// Initialize
connectNativeHost();
console.log("[ClaudeCodeBrowser] Background script initialized");
