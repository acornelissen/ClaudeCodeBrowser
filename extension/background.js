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

const MAX_LOG_ENTRIES = 500;
const MAX_BODY_CHARS = 5000;
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
// tabId -> { requests: Set<requestId>, lastActivity: timestamp }
const inFlightByTab = new Map();
let webRequestListenersAttached = false;

// A tab is watched when anything needs its traffic observed.
function isWatchedTab(tabId) {
  return tabId !== undefined && tabId >= 0 &&
    (loggedTabs.has(tabId) || idleWatchers.has(tabId));
}

function shouldLogRequest(details) {
  const options = loggedTabs.get(details.tabId);
  if (!options) return false;
  if (options.includeAllTypes) return true;
  return DEFAULT_REQUEST_TYPES.includes(details.type);
}

function inFlightFor(tabId) {
  if (!inFlightByTab.has(tabId)) {
    inFlightByTab.set(tabId, { requests: new Set(), lastActivity: Date.now() });
  }
  return inFlightByTab.get(tabId);
}

// Idle counting deliberately covers every request type, not just the ones
// worth logging: a page is not quiet while it is still pulling images.
function noteRequestStarted(details) {
  const state = inFlightFor(details.tabId);
  state.requests.add(details.requestId);
  state.lastActivity = Date.now();
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
    out[header.name] = REDACTED_HEADERS.has(header.name.toLowerCase())
      ? "***"
      : header.value;
  }
  return out;
}

// webRequest hands request bodies over as form fields or raw byte buffers.
function describeRequestBody(requestBody) {
  if (!requestBody) return null;
  try {
    if (requestBody.formData) {
      return JSON.stringify(requestBody.formData).substring(0, 1000);
    }
    if (requestBody.raw && requestBody.raw.length) {
      const decoder = new TextDecoder("utf-8");
      const text = requestBody.raw
        .map(chunk => (chunk.bytes ? decoder.decode(chunk.bytes) : ""))
        .join("");
      return text.substring(0, 1000);
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

  // A runaway page must not grow this map without bound.
  if (pendingNetworkRequests.size >= MAX_PENDING_REQUESTS) {
    const oldest = pendingNetworkRequests.keys().next().value;
    pendingNetworkRequests.delete(oldest);
  }

  pendingNetworkRequests.set(details.requestId, {
    type: details.type,
    method: details.method,
    url: details.url,
    tabId: details.tabId,
    requestBody: describeRequestBody(details.requestBody),
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
  const entry = pendingNetworkRequests.get(details.requestId);
  if (!entry) return;

  entry.status = details.statusCode;
  entry.statusText = details.statusLine;
  entry.responseHeaders = redactHeaderList(details.responseHeaders);

  const options = loggedTabs.get(details.tabId);
  if (!options || !options.captureBodies) return;

  const contentType = (details.responseHeaders || [])
    .find(h => h.name.toLowerCase() === "content-type");
  if (!contentType || !TEXTUAL_CONTENT_TYPE.test(contentType.value)) {
    entry.responseBody = "[not captured: non-textual content type]";
    return;
  }

  attachResponseBodyReader(details.requestId, entry);
};

// Read-only stream filter. Every chunk is written back byte for byte and the
// stream is always closed, so the page receives exactly what it would have
// without us. Anything unexpected disconnects the filter, which hands the
// remainder of the response straight through untouched.
function attachResponseBodyReader(requestId, entry) {
  let filter;
  try {
    filter = browser.webRequest.filterResponseData(requestId);
  } catch (e) {
    entry.responseBody = "[not captured: response filtering unavailable]";
    return;
  }

  const decoder = new TextDecoder("utf-8");
  let collected = "";

  filter.ondata = (event) => {
    try {
      if (collected.length < MAX_BODY_CHARS) {
        collected += decoder.decode(event.data, { stream: true });
      }
    } catch (e) {
      // Undecodable chunk: keep passing data through regardless.
    }
    filter.write(event.data);
  };

  filter.onstop = () => {
    entry.responseBody = collected.substring(0, MAX_BODY_CHARS);
    try {
      filter.close();
    } catch (e) {
      // Already closed.
    }
  };

  filter.onerror = () => {
    entry.responseBody = `[not captured: ${filter.error || "stream error"}]`;
  };
}

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
    browser.webRequest.onHeadersReceived.addListener(
      onHeadersReceivedListener, filter, ["responseHeaders"]);
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
    browser.webRequest.onCompleted.removeListener(onCompletedListener);
    browser.webRequest.onErrorOccurred.removeListener(onErrorOccurredListener);
  } catch (e) {
    console.error("[ClaudeCodeBrowser] Could not detach webRequest listeners:", e);
  }
  webRequestListenersAttached = false;
  pendingNetworkRequests.clear();
}

function startNetworkLogging(tabId, options = {}) {
  if (options.clearExisting) {
    networkLogsByTab.delete(tabId);
  }
  loggedTabs.set(tabId, {
    captureBodies: options.captureBodies !== false,
    includeAllTypes: options.includeAllTypes === true
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
      const pending = state.requests.size;
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
      pendingRequests: inFlightFor(tabId).requests.size
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
    const pattern = new RegExp(urlPattern, "i");
    logs = logs.filter(log => pattern.test(log.url));
  }
  if (options.method) {
    logs = logs.filter(log => log.method?.toUpperCase() === options.method.toUpperCase());
  }
  if (options.status) {
    logs = logs.filter(log => log.status === options.status);
  }
  if (options.errorsOnly || options.errors_only) {
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

// Don't keep logs for tabs that no longer exist.
browser.tabs.onRemoved.addListener((tabId) => {
  loggedTabs.delete(tabId);
  networkLogsByTab.delete(tabId);
  idleWatchers.delete(tabId);
  inFlightByTab.delete(tabId);
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
    const targetTab = tabId ? await browser.tabs.get(tabId) : currentTab;

    // Check if we need to temporarily focus the tab for screenshot
    const needsFocus = tabId && tabId !== currentTab?.id;
    let originalActiveTab = null;

    if (needsFocus && options.allowFocus !== false) {
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

      // If full page screenshot requested, use content script
      if (options.fullPage) {
        const fullPageData = await browser.tabs.sendMessage(targetTab.id, {
          action: "captureFullPage",
          format: options.format || "png"
        });
        return { success: true, data: fullPageData, type: "fullPage" };
      }

      return {
        success: true,
        data: dataUrl,
        type: "visible",
        tab: { id: targetTab.id, url: targetTab.url, title: targetTab.title },
        wasFocused: needsFocus
      };
    } finally {
      // Restore original tab if we changed focus
      if (originalActiveTab && options.restoreFocus !== false) {
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

    // Filter tabs by URL pattern if provided
    let targetTabs = tabs.filter(t => !t.url.startsWith('about:') && !t.url.startsWith('moz-extension:'));

    if (options.urlPattern) {
      const regex = new RegExp(options.urlPattern);
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
          data: options.includeData ? dataUrl : undefined,
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

// Human approval: OS notification to catch the user's attention, plus an
// in-page Approve/Deny banner (content script) that carries the decision
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
    // Notifications unavailable — the in-page banner still works
  }
  return sendToContentScript(tabId, { action: "requestApproval", ...data });
}

// Captcha handoff: notify the human (unless it's a detect-only probe), then
// let the content script show the solve banner and wait for completion
async function solveCaptcha(tabId, data = {}) {
  if (!data.detectOnly) {
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
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
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
async function resolveTabId(tabId) {
  if (tabId !== undefined && tabId !== null) return tabId;
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

  const network = startNetworkLogging(resolved, data);
  const console_ = await sendToContentScript(resolved, { action: "startLogging", ...data });

  return {
    success: true,
    message: "Logging started",
    tabId: resolved,
    network: {
      capturing: network.attached,
      capturesFetch: network.attached,
      captureBodies: data.captureBodies !== false,
      error: network.error
    },
    console: {
      capturing: console_.success === true,
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
  if (data.network !== false) {
    clearNetworkLogs(resolved);
  }
  if (data.console !== false) {
    await sendToContentScript(resolved, { action: "clearLogs", ...data });
  }
  return { success: true, message: "Logs cleared", tabId: resolved };
}

// Generic helper to send message to content script
async function sendToContentScript(tabId, message) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    const result = await browser.tabs.sendMessage(tab.id, message);
    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Click functionality
async function performClick(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "click",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Type functionality
async function performType(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "type",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Scroll functionality
async function performScroll(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "scroll",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Navigation
async function navigateTo(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    await browser.tabs.update(tab.id, { url: data.url });

    // Wait for page to load
    return new Promise((resolve) => {
      const listener = (updatedTabId, changeInfo) => {
        if (updatedTabId === tab.id && changeInfo.status === "complete") {
          browser.tabs.onUpdated.removeListener(listener);
          resolve({ success: true, url: data.url });
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
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "getPageInfo"
    });

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
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "getElements",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Execute arbitrary script
async function executeScript(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

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
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "highlight",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Wait for element
async function waitForElement(tabId, data) {
  try {
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    const result = await browser.tabs.sendMessage(tab.id, {
      action: "waitForElement",
      ...data
    });

    return { success: true, ...result };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Tab management
async function getAllTabs(options = {}) {
  try {
    // Default scope: the window Claude is actually driving, not every
    // window/tab in the user's browser. With dozens of tabs open, querying
    // {} and returning full metadata per tab blows past response size
    // limits. Opt into the wider view explicitly when needed.
    const currentWindowOnly = options.current_window_only !== false;
    const includeFavicon = options.include_favicon === true;
    const urlPattern = options.url_pattern ? new RegExp(options.url_pattern) : null;
    const limit = Number.isInteger(options.limit) ? options.limit : 50;

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
      pageInfo = await browser.tabs.sendMessage(tabId, { action: "getPageInfo" });
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
async function findTabs(options) {
  try {
    const tabs = await browser.tabs.query({});
    let filtered = tabs;

    if (options.url) {
      filtered = filtered.filter(t => t.url.startsWith(options.url));
    }
    if (options.urlPattern) {
      const regex = new RegExp(options.urlPattern);
      filtered = filtered.filter(t => regex.test(t.url));
    }
    if (options.title) {
      const titleLower = options.title.toLowerCase();
      filtered = filtered.filter(t => t.title?.toLowerCase().includes(titleLower));
    }
    if (options.active !== undefined) {
      filtered = filtered.filter(t => t.active === options.active);
    }
    if (options.audible !== undefined) {
      filtered = filtered.filter(t => t.audible === options.audible);
    }

    return {
      success: true,
      tabs: filtered.map(t => ({
        id: t.id,
        url: t.url,
        title: t.title,
        active: t.active,
        windowId: t.windowId,
        status: t.status
      })),
      count: filtered.length
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

async function createNewTab(data) {
  try {
    const tab = await browser.tabs.create({
      url: data.url || "about:blank",
      active: data.active !== false
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
    const tab = tabId ? await browser.tabs.get(tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];

    // bypassCache: true = hard refresh (Ctrl+Shift+R), false = normal refresh (F5)
    await browser.tabs.reload(tab.id, { bypassCache: options.bypassCache || false });

    // Wait for page to load if requested
    if (options.waitForLoad !== false) {
      return new Promise((resolve) => {
        const listener = (updatedTabId, changeInfo) => {
          if (updatedTabId === tab.id && changeInfo.status === "complete") {
            browser.tabs.onUpdated.removeListener(listener);
            resolve({
              success: true,
              refreshed: true,
              tab: { id: tab.id, url: tab.url, title: tab.title },
              bypassCache: options.bypassCache || false
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

    return { success: true, refreshed: true, tab: { id: tab.id, url: tab.url } };
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
        const regex = new RegExp(options.urlPattern);
        if (!regex.test(tab.url)) {
          continue;
        }
      }

      await browser.tabs.reload(tab.id, { bypassCache: options.bypassCache || false });
      results.push({ id: tab.id, url: tab.url, reloaded: true });
    }

    return {
      success: true,
      reloadedCount: results.length,
      tabs: results,
      bypassCache: options.bypassCache || false
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
        const regex = new RegExp(options.urlPattern);
        matches = regex.test(tab.url);
      }

      if (matches) {
        await browser.tabs.reload(tab.id, { bypassCache: options.bypassCache !== false });
        results.push({ id: tab.id, url: tab.url, reloaded: true });
      }
    }

    return {
      success: true,
      reloadedCount: results.length,
      tabs: results,
      bypassCache: options.bypassCache !== false
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Listen for messages from content scripts
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
      if (result.success && nativePort) {
        nativePort.postMessage({
          action: "screenshotTaken",
          data: result.data,
          tab: { id: tab.id, url: tab.url, title: tab.title }
        });
      }
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
