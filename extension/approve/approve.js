/**
 * ClaudeCodeBrowser - Approval prompt
 *
 * Runs in an extension page (moz-extension:// origin) opened in its own
 * window. The page being automated cannot reach this document, restyle it,
 * read it, or synthesise a click on it - which is the whole reason the
 * decision was moved out of the content script.
 *
 * MIT License
 * Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
 * Author: dre@ligandal.com
 */

(function() {
  'use strict';

  const params = new URLSearchParams(window.location.search);
  const requestId = params.get('id');

  const heading = document.getElementById('heading');
  const message = document.getElementById('message');
  const detail = document.getElementById('detail');
  const detailHeading = document.getElementById('detail-heading');
  const site = document.getElementById('site');
  const countdown = document.getElementById('countdown');
  const approveBtn = document.getElementById('approve');
  const denyBtn = document.getElementById('deny');

  let settled = false;
  let deadline = null;
  let ticker = null;

  function decide(approved) {
    if (settled) return;
    settled = true;
    if (ticker) clearInterval(ticker);
    approveBtn.disabled = true;
    denyBtn.disabled = true;
    browser.runtime
      .sendMessage({ target: 'approval', action: 'decide', requestId, approved })
      .catch(() => {})
      .then(() => window.close());
  }

  // Only trusted events count, exactly as in the content script: a decision
  // has to come from a person. Nothing can reach this page to dispatch one,
  // but the check costs nothing and the property is the point.
  function onHumanClick(element, handler) {
    element.addEventListener('click', (event) => {
      if (!event.isTrusted) return;
      handler();
    });
  }

  onHumanClick(approveBtn, () => decide(true));
  onHumanClick(denyBtn, () => decide(false));

  // Enter must not approve by accident; Escape denies.
  document.addEventListener('keydown', (event) => {
    if (!event.isTrusted) return;
    if (event.key === 'Escape') decide(false);
  });

  // Closing the window is a denial, not a timeout.
  window.addEventListener('beforeunload', () => {
    if (!settled) {
      settled = true;
      browser.runtime
        .sendMessage({ target: 'approval', action: 'decide', requestId,
                       approved: false, closed: true })
        .catch(() => {});
    }
  });

  function renderCountdown() {
    if (deadline === null) return;
    const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    countdown.textContent = left > 0
      ? `Denied automatically in ${left}s if you do not choose.`
      : 'Timed out - denied.';
    if (left === 0) decide(false);
  }

  browser.runtime
    .sendMessage({ target: 'approval', action: 'details', requestId })
    .then((request) => {
      if (!request || !request.found) {
        message.textContent =
          'This request is no longer waiting for an answer. You can close this window.';
        detailHeading.hidden = true;
        detail.hidden = true;
        approveBtn.disabled = true;
        denyBtn.textContent = 'Close';
        return;
      }

      // textContent throughout: the message and detail come from the agent,
      // and must not be able to inject markup into the trusted prompt.
      heading.textContent = request.heading || 'Claude requests approval';
      message.textContent = request.message || 'Claude wants to perform an action.';

      if (request.detail) {
        detail.textContent = request.detail;
      } else {
        detailHeading.hidden = true;
        detail.hidden = true;
      }

      if (request.protectedUrl) {
        site.textContent = `Site: ${request.protectedUrl}`;
      }

      document.title = request.heading || 'Approve action';

      if (request.timeout) {
        deadline = Date.now() + request.timeout;
        renderCountdown();
        ticker = setInterval(renderCountdown, 1000);
      }
    })
    .catch(() => {
      message.textContent =
        'Could not load the request details. Deny unless you know what this is.';
      detailHeading.hidden = true;
      detail.hidden = true;
    });
})();
