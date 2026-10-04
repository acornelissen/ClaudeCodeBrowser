#!/usr/bin/env python3
"""
Safety guard for ClaudeCodeBrowser.

Every tool call passes through SafetyGuard.check() before it reaches the
browser. The guard enforces, in order:

1. URL scheme guard      - navigation is limited to http/https/about:blank;
                           file:, javascript:, data:, chrome:, resource: and
                           moz-extension: URLs are always refused.
2. Blocklist/allowlist   - regex patterns matched against the page the tool
                           will act on (its url argument when it has one, the
                           tracked current URL otherwise). An empty allowlist
                           means "everything not blocked".
3. Protected domains     - banking / payment / healthcare / government login
                           pages (configurable). State-changing actions there
                           require an explicit confirmation: in-browser human
                           approval, or a confirm_token bound to that exact
                           call (tool, arguments and URL). Observation and
                           low-risk acts (scroll, hover, highlight, focus) are
                           unaffected.
4. Read-only mode        - blocks every state-changing action while still
                           allowing screenshots, inspection and log reading.
5. Script toggle         - browser_execute_script and friends can be turned
                           off entirely (allow_script_execution: false).
6. Rate limit            - sliding-window cap on actions per minute, so a
                           runaway agent cannot machine-gun the browser.
7. Audit log             - every decision (allowed, denied, confirmation
                           requested) is appended to
                           ~/.claudecodebrowser/logs/audit.jsonl with
                           sensitive argument values redacted.

Configuration lives in ~/.claudecodebrowser/safety.json (created with safe
defaults on first run). Environment overrides:

  CLAUDE_BROWSER_READ_ONLY=1        force read-only mode
  CLAUDE_BROWSER_ALLOW_SCRIPTS=0    disable script-execution tools
  CLAUDE_BROWSER_SAFETY_CONFIG=path alternate config file

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger('ClaudeCodeBrowser.Safety')

_CONFIG_FILE = Path(os.environ.get(
    'CLAUDE_BROWSER_SAFETY_CONFIG',
    str(Path.home() / '.claudecodebrowser' / 'safety.json')
))
_AUDIT_FILE = Path.home() / '.claudecodebrowser' / 'logs' / 'audit.jsonl'

# Schemes a navigation target may use. Anything else (file:, javascript:,
# data:, chrome:, resource:, moz-extension:, about:config ...) is refused.
_SAFE_URL_RE = re.compile(r'^(https?://|about:blank$)', re.IGNORECASE)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "read_only": False,
    "allow_script_execution": True,
    "confirm_protected_actions": True,
    # How protected actions get approved:
    #   "auto"  - ask the human in their browser (overlay + OS notification)
    #             when an attended browser is connected, else confirm_token
    #   "human" - always require in-browser human approval
    #   "token" - always use the agent-side confirm_token round trip
    "protected_approval": "auto",
    # Typing into <input type="password"> fields is refused by default.
    # Credentials belong in the browser's own password manager (autofill),
    # so they never pass through the AI or its logs. Set true to override.
    "allow_password_typing": False,
    "max_actions_per_minute": 120,
    "audit_log": True,
    # Regexes matched against target URLs. Empty allowlist = allow everything
    # that is not blocked.
    "blocked_url_patterns": [],
    "allowed_url_patterns": [],
    # State-changing actions on URLs matching these patterns need explicit
    # confirmation (two-step confirm_token flow). Tune to taste.
    # Matched with re.search against the whole URL, so these are substrings
    # unless anchored. Host-ish patterns are written to match the authority
    # part followed by a delimiter, so "irs.gov?x=1" and "irs.gov:443/" are
    # caught as well as "irs.gov/" - appending a query string used to switch
    # the guard off entirely.
    "protected_url_patterns": [
        r"paypal\.com",
        r"venmo\.com",
        r"coinbase\.com",
        r"binance\.com",
        r"kraken\.com",
        r"chase\.com",
        r"bankofamerica\.com",
        r"wellsfargo\.com",
        r"schwab\.com",
        r"fidelity\.com",
        r"vanguard\.com",
        r"robinhood\.com",
        r"stripe\.com/dashboard",
        r"\.gov([:/?#]|$)",
        r"healthcare",
        r"mychart",
    ],
}

# Tools that only observe the page. Allowed in read-only mode and never
# require protected-domain confirmation.
OBSERVE_TOOLS = {
    'browser_screenshot',
    'browser_get_page_info', 'browser_get_elements', 'browser_get_value',
    'browser_get_text', 'browser_get_tabs', 'browser_get_tab_info',
    'browser_find_tabs', 'browser_wait_for_element', 'browser_wait_for_change',
    'browser_wait_for_network_idle', 'browser_observe_element',
    'browser_stop_observing',
    'browser_start_logging', 'browser_stop_logging',
    'browser_get_console_logs', 'browser_get_network_logs',
    'browser_clear_logs', 'browser_safety_status',
    'browser_request_approval', 'browser_audit_page', 'browser_solve_captcha',
}

# browser_screenshot_all_tabs is deliberately NOT an observe tool: it activates
# every tab in every window in turn and photographs whatever is on screen,
# which is the broadest reach in the tool set and nothing like reading the page
# the agent is working on.

# Tools that change something but carry little risk on their own: they move
# the viewport or the focus, or dispatch a pointer event. They are not
# observation (read-only mode blocks them, as it says it does) but asking for
# protected-domain confirmation on every scroll would train the person to
# click Approve without reading, which costs more than it buys.
LOW_RISK_ACT_TOOLS = {
    'browser_scroll', 'browser_hover', 'browser_highlight', 'browser_focus_tab',
    'browser_scroll_and_capture',
}

# Tools that run arbitrary JavaScript in the page. Subject to the
# allow_script_execution toggle on top of the ACT rules.
SCRIPT_TOOLS = {
    'browser_execute_script', 'browser_eval_chain', 'browser_wait_and_act',
    'browser_inject_observer',
}

# Everything else (click, type, navigate, tab management, refresh, ...) is a
# state-changing ACT tool: blocked in read-only mode, confirmation required on
# protected domains.

# Argument keys whose values never appear in the audit log.
_SENSITIVE_ARGS = {'text', 'script', 'value', 'password', 'steps', 'action_script', 'condition'}

# How long a confirmation token stays valid, and how many can be outstanding.
_TOKEN_TTL_SECONDS = 120
_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_MAX_PENDING_TOKENS = 32


def resolve_screenshots_dir() -> Path:
    """Return the directory screenshots are written to, creating it if needed.

    A screenshot can contain anything that was on screen — open mail, a
    logged-in dashboard — so the default lives in the user's own directory with
    0700 permissions rather than a world-readable shared /tmp. An explicit
    CLAUDE_BROWSER_SCREENSHOTS_DIR is honoured as given: the location is then
    the user's choice and its permissions are left alone.
    """
    override = os.environ.get('CLAUDE_BROWSER_SCREENSHOTS_DIR')
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    path = Path.home() / '.claudecodebrowser' / 'screenshots'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # mkdir's mode only applies on creation; tighten an existing directory
        # left behind by an earlier version.
        path.chmod(0o700)
    except OSError as e:
        logger.warning(f"Could not restrict permissions on {path}: {e}")
    return path


def _load_config() -> Dict[str, Any]:
    """Load safety.json, writing defaults on first run. Unknown keys are kept."""
    config = dict(DEFAULT_CONFIG)
    try:
        if _CONFIG_FILE.exists():
            user_config = json.loads(_CONFIG_FILE.read_text())
            if not isinstance(user_config, dict):
                raise ValueError('safety.json must contain a JSON object')
            config.update(user_config)
        else:
            _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + '\n')
            logger.info(f"Wrote default safety config to {_CONFIG_FILE}")
    except Exception as e:
        # A broken config must not silently disable the guard - keep defaults.
        logger.error(f"Failed to load {_CONFIG_FILE}, using defaults: {e}")

    if os.environ.get('CLAUDE_BROWSER_READ_ONLY') == '1':
        config['read_only'] = True
    if os.environ.get('CLAUDE_BROWSER_ALLOW_SCRIPTS') == '0':
        config['allow_script_execution'] = False
    return config


class SafetyGuard:
    """Central policy check for every tool invocation."""

    def __init__(self):
        self.config = _load_config()
        self._lock = threading.Lock()
        self._action_times: deque = deque()
        # confirm_token -> (tool_name, issued_at)
        self._pending_tokens: Dict[str, Tuple[str, float]] = {}
        # Best-effort record of the URL the browser is currently on, updated
        # from tool results that carry one. Used for protected-domain checks
        # on actions that do not name a URL themselves (click, type, ...).
        self._current_url: Optional[str] = None
        self._compile_patterns()

    def _compile_patterns(self):
        def compile_list(key):
            patterns = []
            for pattern in self.config.get(key, []):
                try:
                    patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    logger.error(f"Invalid regex in safety.json {key}: {pattern!r} ({e})")
            return patterns
        self._blocked = compile_list('blocked_url_patterns')
        self._allowed = compile_list('allowed_url_patterns')
        self._protected = compile_list('protected_url_patterns')

    # ------------------------------------------------------------------ #
    # Public API

    def check(self, tool_name: str, arguments: Dict[str, Any],
              human_approved: bool = False) -> Optional[Dict[str, Any]]:
        """Return None if the call may proceed, or an error dict to send back.

        human_approved=True means the person at the browser explicitly
        approved this exact call (via the in-page Approve/Deny overlay), which
        satisfies the protected-domain confirmation requirement.
        """
        target_url = self._target_url(arguments)

        # enabled: false turns off policy, not the scheme allowlist. Letting
        # one config key re-enable file:// and javascript: navigation is not a
        # policy choice anyone would make deliberately.
        if not self.config.get('enabled', True):
            arguments.pop('confirm_token', None)
            if target_url is not None and not _SAFE_URL_RE.match(target_url):
                return self._deny('blocked_scheme',
                                  f"Navigation to {target_url!r} refused: only http://, "
                                  f"https:// and about:blank targets are allowed.")
            return None
        confirm_token = arguments.pop('confirm_token', None)

        denial = self._check_inner(tool_name, target_url, confirm_token,
                                   human_approved, arguments)
        self._audit(tool_name, arguments, target_url,
                    ('allowed_by_human' if human_approved and denial is None else
                     'allowed' if denial is None else
                     denial.get('safety_decision', 'denied')))
        return denial

    def note_url(self, result: Dict[str, Any]):
        """Track the browser's current URL from a tool result, if it has one."""
        url = result.get('url') or (result.get('tab') or {}).get('url')
        if isinstance(url, str) and url:
            with self._lock:
                self._current_url = url

    def status(self) -> Dict[str, Any]:
        """Snapshot of the active policy, for the browser_safety_status tool."""
        with self._lock:
            recent = len(self._action_times)
            pending = len(self._pending_tokens)
            current_url = self._current_url
        return {
            'success': True,
            'enabled': self.config.get('enabled', True),
            'read_only': self.config.get('read_only', False),
            'allow_script_execution': self.config.get('allow_script_execution', True),
            'confirm_protected_actions': self.config.get('confirm_protected_actions', True),
            'protected_approval': self.config.get('protected_approval', 'auto'),
            'allow_password_typing': self.config.get('allow_password_typing', False),
            'max_actions_per_minute': self.config.get('max_actions_per_minute', 120),
            'actions_in_last_minute': recent,
            'pending_confirmations': pending,
            'current_url': current_url,
            'blocked_url_patterns': self.config.get('blocked_url_patterns', []),
            'allowed_url_patterns': self.config.get('allowed_url_patterns', []),
            'protected_url_patterns': self.config.get('protected_url_patterns', []),
            'config_file': str(_CONFIG_FILE),
            'audit_log': str(_AUDIT_FILE) if self.config.get('audit_log', True) else None,
        }

    # ------------------------------------------------------------------ #
    # Policy internals

    def _check_inner(self, tool_name: str, target_url: Optional[str],
                     confirm_token: Optional[str],
                     human_approved: bool = False,
                     arguments: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        arguments = arguments if arguments is not None else {}
        is_observe = tool_name in OBSERVE_TOOLS
        is_script = tool_name in SCRIPT_TOOLS

        # 1. Rate limit applies to everything, including observation.
        if not self._within_rate_limit():
            return self._deny('rate_limited',
                              f"Rate limit exceeded: more than "
                              f"{self.config.get('max_actions_per_minute')} actions in the "
                              f"last minute. Wait a few seconds and retry.")

        # 2. Scheme guard on explicit navigation targets.
        if target_url is not None and not _SAFE_URL_RE.match(target_url):
            return self._deny('blocked_scheme',
                              f"Navigation to {target_url!r} refused: only http://, "
                              f"https:// and about:blank targets are allowed.")

        # 3. Blocklist/allowlist. Checked against the page the tool will act
        #    on, not only against a url argument: no read tool takes a url, so
        #    blocking a domain used to stop navigating there while leaving
        #    browser_get_text and browser_screenshot free on an already-open
        #    tab, which is the opposite of what a blocklist is for.
        policy_url = target_url if target_url is not None else self._current_url
        if policy_url is not None:
            if any(p.search(policy_url) for p in self._blocked):
                return self._deny('blocked_url',
                                  f"URL {policy_url!r} matches blocked_url_patterns in "
                                  f"safety.json.")
            if self._allowed and not any(p.search(policy_url) for p in self._allowed):
                return self._deny('not_allowlisted',
                                  f"URL {policy_url!r} does not match allowed_url_patterns "
                                  f"in safety.json (allowlist mode is active).")

        if is_observe:
            return None

        # 3. Read-only mode blocks all state-changing tools.
        if self.config.get('read_only', False):
            return self._deny('read_only',
                              f"{tool_name} refused: safety guard is in read-only mode "
                              f"(read_only in safety.json or CLAUDE_BROWSER_READ_ONLY=1). "
                              f"Observation tools like browser_screenshot remain available.")

        # 4. Script execution toggle.
        if is_script and not self.config.get('allow_script_execution', True):
            return self._deny('scripts_disabled',
                              f"{tool_name} refused: script execution is disabled "
                              f"(allow_script_execution in safety.json or "
                              f"CLAUDE_BROWSER_ALLOW_SCRIPTS=0).")

        # 5. Protected-domain confirmation for state-changing actions.
        if (self.config.get('confirm_protected_actions', True)
                and tool_name not in LOW_RISK_ACT_TOOLS):
            check_url = target_url if target_url is not None else self._current_url
            matched = self._matched_protected(check_url)
            if matched:
                if human_approved:
                    return None
                fingerprint = self._call_fingerprint(tool_name, arguments, check_url)
                if confirm_token and self._consume_token(confirm_token, fingerprint):
                    return None
                token = self._issue_token(tool_name, fingerprint)
                return {
                    'success': False,
                    'safety_decision': 'confirmation_required',
                    'confirmation_required': True,
                    'confirm_token': token,
                    'approval_mode': self.config.get('protected_approval', 'auto'),
                    'protected_url': check_url,
                    'error': (
                        f"{tool_name} targets a protected site ({check_url!r} matches "
                        f"pattern {matched!r} in safety.json). This is a guard against "
                        f"unintended actions on sensitive sites (banking, payments, "
                        f"health, government). If the user really wants this, repeat the "
                        f"exact same call with \"confirm_token\": \"{token}\" added to "
                        f"the arguments. The token is single-use and expires in "
                        f"{_TOKEN_TTL_SECONDS}s."
                    ),
                }

        return None

    def _target_url(self, arguments: Dict[str, Any]) -> Optional[str]:
        url = arguments.get('url')
        return url if isinstance(url, str) and url else None

    def _matched_protected(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        for pattern in self._protected:
            if pattern.search(url):
                return pattern.pattern
        return None

    def _within_rate_limit(self) -> bool:
        limit = self.config.get('max_actions_per_minute', 120)
        if not limit or limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._action_times and now - self._action_times[0] > 60.0:
                self._action_times.popleft()
            if len(self._action_times) >= limit:
                return False
            self._action_times.append(now)
            return True

    @staticmethod
    def _call_fingerprint(tool_name: str, arguments: Dict[str, Any],
                          check_url: Optional[str]) -> str:
        """Identify the exact call a token is good for.

        The denial says "repeat the exact same call", and this is what makes
        that true. Binding to the tool name alone meant a token earned by
        clicking #help authorised a click on #transfer-submit, and a token for
        one protected domain authorised a different one.
        """
        payload = {
            'tool': tool_name,
            'url': check_url,
            'args': {k: v for k, v in sorted(arguments.items())
                     if k != 'confirm_token'},
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _issue_token(self, tool_name: str, fingerprint: str) -> str:
        token = secrets.token_hex(8)
        now = time.monotonic()
        with self._lock:
            # Drop expired tokens, and the oldest if too many are pending.
            expired = [t for t, (_, ts) in self._pending_tokens.items()
                       if now - ts > _TOKEN_TTL_SECONDS]
            for t in expired:
                del self._pending_tokens[t]
            while len(self._pending_tokens) >= _MAX_PENDING_TOKENS:
                oldest = min(self._pending_tokens, key=lambda t: self._pending_tokens[t][1])
                del self._pending_tokens[oldest]
            self._pending_tokens[token] = (fingerprint, now)
        return token

    def _consume_token(self, token: str, fingerprint: str) -> bool:
        now = time.monotonic()
        with self._lock:
            entry = self._pending_tokens.pop(token, None)
        if entry is None:
            return False
        issued_for, issued_at = entry
        return (secrets.compare_digest(issued_for, fingerprint)
                and now - issued_at <= _TOKEN_TTL_SECONDS)

    def _deny(self, decision: str, message: str) -> Dict[str, Any]:
        return {'success': False, 'safety_decision': decision, 'error': message}

    def _audit(self, tool_name: str, arguments: Dict[str, Any],
               target_url: Optional[str], decision: str):
        if not self.config.get('audit_log', True):
            return
        entry = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'tool': tool_name,
            'decision': decision,
            'url': target_url or self._current_url,
            'args': {k: ('***' if k in _SENSITIVE_ARGS else v)
                     for k, v in arguments.items()},
        }
        try:
            _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                _AUDIT_FILE.parent.chmod(0o700)
            except OSError:
                pass
            # This file reconstructs where the agent, and so the user, went.
            # Create it 0600 rather than at the prevailing umask.
            if not _AUDIT_FILE.exists():
                os.close(os.open(str(_AUDIT_FILE),
                                 os.O_CREAT | os.O_WRONLY, 0o600))
            elif _AUDIT_FILE.stat().st_mode & 0o077:
                _AUDIT_FILE.chmod(0o600)

            # Rotate: it had no cap of any kind.
            if _AUDIT_FILE.stat().st_size > _AUDIT_MAX_BYTES:
                backup = _AUDIT_FILE.with_suffix('.jsonl.1')
                if backup.exists():
                    backup.unlink()
                _AUDIT_FILE.rename(backup)
                os.close(os.open(str(_AUDIT_FILE),
                                 os.O_CREAT | os.O_WRONLY, 0o600))

            with open(_AUDIT_FILE, 'a') as f:
                f.write(json.dumps(entry, default=str) + '\n')
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")


# Module-level singleton, mirroring the connection manager in server.py.
_guard: Optional[SafetyGuard] = None


def get_safety_guard() -> SafetyGuard:
    global _guard
    if _guard is None:
        _guard = SafetyGuard()
    return _guard
