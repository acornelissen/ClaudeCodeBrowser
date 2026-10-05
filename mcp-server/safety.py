#!/usr/bin/env python3
"""
Safety guard for ClaudeCodeBrowser.

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.

Every tool call passes through SafetyGuard.check() before it reaches the
browser. The guard enforces, in order:

1. URL scheme guard      - navigation is limited to http/https/about:blank;
                           file:, javascript:, data:, chrome:, resource: and
                           moz-extension: URLs are always refused.
2. Blocklist/allowlist   - regex patterns matched against the page the tool
                           will act on (its url argument when it has one, the
                           tracked current URL otherwise). An empty allowlist
                           means "everything not blocked". The blocklist
                           matches loosely (re.search); the allowlist grants
                           access, so a pattern there has to cover a whole
                           URL prefix or a whole host - see _permitted(). A
                           granting list whose patterns all fail to compile
                           keeps the restriction on and permits nothing.
                           URLs are normalised the way the browser parses
                           them before any of this - see _normalise_url().
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
                           browser_audit_page is covered too: it only
                           observes, but it does so by running a fixed script
                           in the page, so the toggle and the protected-site
                           script refusal both apply to it.
6. Rate limit            - sliding-window cap on actions per minute, so a
                           runaway agent cannot machine-gun the browser.
7. Audit log             - every decision (allowed, denied, confirmation
                           requested) is appended to
                           ~/.claudecodebrowser/logs/audit.jsonl with
                           sensitive argument values redacted. A URL keeps
                           its host and path, which is what makes the entry
                           useful, and loses the userinfo, query and fragment,
                           which is where credentials live - see redact_url.

Configuration lives in ~/.claudecodebrowser/safety.json (created with safe
defaults on first run). Environment overrides:

  CLAUDE_BROWSER_READ_ONLY=1        force read-only mode
  CLAUDE_BROWSER_ALLOW_SCRIPTS=0    disable script-execution tools
  CLAUDE_BROWSER_SAFETY_CONFIG=path alternate config file

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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

logger = logging.getLogger('ClaudeCodeBrowser.Safety')

_CONFIG_FILE = Path(os.environ.get(
    'CLAUDE_BROWSER_SAFETY_CONFIG',
    str(Path.home() / '.claudecodebrowser' / 'safety.json')
))
_AUDIT_FILE = Path.home() / '.claudecodebrowser' / 'logs' / 'audit.jsonl'

# An audit log written by an older version is world-readable; tighten it on
# import rather than waiting for the next entry.
try:
    if _AUDIT_FILE.exists() and _AUDIT_FILE.stat().st_mode & 0o077:
        _AUDIT_FILE.chmod(0o600)
except OSError:
    pass

# Schemes a navigation target may use. Anything else (file:, javascript:,
# data:, chrome:, resource:, moz-extension:, about:config ...) is refused.
_SAFE_URL_RE = re.compile(r'^(https?://|about:blank$)', re.IGNORECASE)

# Firefox navigates with tabs.update({url}), which parses the string the
# WHATWG way; every pattern below is matched with Python's re, which does
# not. Where the two disagree the guard judges a URL the browser never
# loads. new URL('https://www.irs.gov\\payments').href is
# 'https://www.irs.gov/payments', so one backslash walked straight past the
# protected-domain delimiter class while landing on the same page.
# _normalise_url closes that gap: match what the browser will actually load.
_C0_AND_SPACE = ''.join(chr(c) for c in range(0x21))
_REMOVED_URL_CHARS = str.maketrans('', '', '\t\n\r')
# For these schemes the browser reads any run of slashes after the colon,
# including none, as "//": https:///evil.com/ loads evil.com.
_SPECIAL_SCHEME_SLASHES_RE = re.compile(r'^(https?|wss?|ftp):/*', re.IGNORECASE)
# Characters that end the authority or a path segment. A pattern that stops
# anywhere else has only matched part of a name.
_URL_DELIMITERS = ':/?#'

# Code points a host cannot contain (WHATWG "forbidden host code point").
# A host still holding one of these once its escapes are decoded is a URL the
# browser refuses outright, so there is nothing to normalise it towards.
_FORBIDDEN_HOST_CHARS = frozenset('\x00\t\n\r #/:<>?@[\\]^|%')


def _clean_url_text(url: str) -> str:
    """Strip the characters the browser ignores before it parses a URL.

    Shared by _normalise_url and redact_url so both read the same string the
    browser does: leading and trailing C0 controls and spaces go, tabs and
    line breaks go wherever they appear, a backslash counts as a forward
    slash, and any run of slashes after http: or https: is read as "//".
    Without that last step https:///evil.com/ had no host here, so anchored
    patterns never matched the site the browser loaded.
    """
    cleaned = url.strip(_C0_AND_SPACE).translate(_REMOVED_URL_CHARS).replace(
        '\\', '/')
    return _SPECIAL_SCHEME_SLASHES_RE.sub(
        lambda m: m.group(1).lower() + '://', cleaned)


def _strip_userinfo_text(url: str) -> str:
    """Drop 'user:pass@' from a URL that cannot be parsed.

    The authority is what follows '//' up to the next '/', '?' or '#'. Only
    the authority's own last '@' counts: an '@' in the path or query is
    ordinary data.
    """
    marker = url.find('//')
    if marker < 0:
        return url
    start = marker + 2
    end = len(url)
    for ch in '/?#':
        found = url.find(ch, start)
        if found >= 0:
            end = min(end, found)
    authority = url[start:end]
    if '@' not in authority:
        return url
    return url[:start] + authority.rpartition('@')[2] + url[end:]


def _normalise_url(url: str) -> str:
    """Return the URL the browser would load, for matching purposes.

    The WHATWG rules that change the host or the first delimiter, which are
    the ones the guard reads: leading and trailing C0 controls and spaces are
    stripped, tabs and line breaks are removed wherever they appear, a
    backslash counts as a forward slash, and the host is percent-decoded,
    lowercased and stripped of trailing dots. The backslash rule applies to
    http(s) only in the standard, and http(s) and about:blank are the only
    schemes this guard lets through anyway, so it is applied unconditionally
    rather than parsed for.

    The host rules are not cosmetic: new URL('https://%63hase.com/transfer')
    loads chase.com, and so does 'https://chase.com./transfer', while the
    guard matched the text as written and saw neither a protected domain nor
    a blocklist hit.

    Userinfo is dropped too, on every exit from this function. That is not a
    normalisation the browser performs - it sends the credentials - but ':' is
    both this guard's delimiter and the userinfo password separator, so
    'https://localhost:3000@evil.com/' satisfied a permit pattern anchored on
    'https://localhost' while loading evil.com. "On every exit" is the part
    that was wrong: the drop used to happen only after the host checks
    passed, and an IPv6 literal never reached it, so the fix covered domains
    and not addresses. Only this copy is rewritten; the URL handed to the
    browser is the one the caller sent.
    """
    if not isinstance(url, str):
        return url
    cleaned = _clean_url_text(url)
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        # Not a URL the browser will load either, so the host is not ours to
        # rewrite - but the userinfo still goes. urlsplit raises on, among
        # other things, a bracketed host that is not an address, and that
        # exception used to carry 'user:pass@' out of the function with it.
        # Done on the text, since there is nothing parsed to work from.
        return _strip_userinfo_text(cleaned)

    # Separate from the split, because .hostname does its own parsing and
    # raises on things urlsplit accepted - and that exception used to carry
    # the userinfo out with it.
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        # about:blank or no authority at all, in which case there is nowhere
        # for userinfo to hide; or a host .hostname could not parse, in which
        # case it is not ours to rewrite but the userinfo still goes.
        if '@' not in parts.netloc:
            return cleaned
        return urlunsplit((parts.scheme, parts.netloc.rpartition('@')[2],
                           parts.path, parts.query, parts.fragment))
    # Everything up to and including the last '@' is userinfo. Taken FIRST,
    # and used on every exit below: this used to be computed after the host
    # checks, so any URL whose host could not be normalised kept its
    # userinfo - and an IPv6 literal always took that exit, because urlsplit
    # strips the brackets and the bare address therefore contains ':', which
    # is in _FORBIDDEN_HOST_CHARS. So the whole userinfo fix applied to
    # domains and not to IPv6 targets:
    # 'https://localhost:3000@[2606:4700::1]/steal' satisfied a permit
    # pattern anchored on 'https://localhost' while loading 2606:4700::1.
    # The port text is kept verbatim because parts.port raises on a port that
    # is not a number, and a URL the browser rejects is not ours to rewrite.
    hostport = parts.netloc.rpartition('@')[2]
    is_ipv6 = hostport.startswith('[')

    def rebuilt(authority):
        return urlunsplit((parts.scheme, authority, parts.path, parts.query,
                           parts.fragment))

    host = unquote(host).rstrip('.').lower()
    # ':' is legal inside an IPv6 literal and forbidden everywhere else.
    forbidden = (_FORBIDDEN_HOST_CHARS - {':'}) if is_ipv6 \
        else _FORBIDDEN_HOST_CHARS
    if not host or forbidden.intersection(host):
        # The host is not ours to rewrite, but the userinfo still goes.
        return rebuilt(hostport)
    if is_ipv6:
        # urlsplit strips the brackets off an IPv6 literal; put them back or
        # the colon reads as a port separator.
        host = f'[{host}]'
    if is_ipv6:
        tail = hostport.partition(']')[2]
        port = tail if tail.startswith(':') else ''
    else:
        _, found, tail = hostport.rpartition(':')
        port = f':{tail}' if found else ''
    return rebuilt(host + port)


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
    # How to treat a domain that is not in protected_url_patterns.
    #   "allow"   - act freely (the default, and the historical behaviour)
    #   "confirm" - require confirmation unless it matches
    #               trusted_url_patterns
    # protected_url_patterns is a denylist of ~16 finance/health/government
    # patterns, so everything else - your mail, your cloud console, your
    # admin panels - is unprotected by default. "confirm" inverts that. It
    # will prompt a lot until trusted_url_patterns covers your normal work,
    # and prompt fatigue is its own hazard, so it is opt-in.
    "unlisted_domains": "allow",
    "trusted_url_patterns": [],
    # browser_execute_script can read any field, including a password, so the
    # credential guard is advisory while scripts are enabled. Refusing scripts
    # on protected sites closes that where it matters most.
    "deny_scripts_on_protected_urls": True,
    "max_actions_per_minute": 120,
    "audit_log": True,
    # Regexes matched against target URLs. Empty allowlist = allow everything
    # that is not blocked. blocked_url_patterns is matched with re.search, so
    # a loose pattern catches more and errs towards refusing. The two lists
    # that *grant* access - allowed_url_patterns and trusted_url_patterns -
    # cannot afford that, so they are matched as a whole URL prefix or a whole
    # host; see _permitted().
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

# Tools whose url argument filters the tab list rather than naming a target.
TAB_FILTER_TOOLS = frozenset({'browser_find_tabs'})

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

# Tools that only observe, but do it by running a fixed script of our own.
# They stay available in read-only mode - read-only is about not changing the
# page, and these change nothing - but they are still JavaScript running in
# the user's page, so the script toggle and the protected-site script refusal
# apply. browser_audit_page was an observe tool and nothing else, so the
# guard returned early and the server dispatched executeScript itself: a user
# who set allow_script_execution: false to keep JavaScript out of their pages
# got it anyway, on a protected site, in read-only mode.
OBSERVE_SCRIPT_TOOLS = {
    'browser_audit_page',
}

# Everything else (click, type, navigate, tab management, refresh, ...) is a
# state-changing ACT tool: blocked in read-only mode, confirmation required on
# protected domains.

# Argument keys whose values never appear in a log - the audit log here and
# the application log in server.py, which imports this list rather than
# keeping a second one. There were two copies and a comment claiming they
# agreed; 'url' was in server.py's only, so a password-reset link went to
# audit.jsonl in clear.
#
# 'key' is here because browser_press_key logged the key it pressed, one
# entry per press and in order, so a typed sequence could be read straight
# off the log. Synthetic KeyboardEvents cannot type in attended Firefox, but
# the headless backend's keyboard.press does.
#
# 'url_pattern' is a regex matched against whole tab URLs, query string
# included, so it can name a reset token to find the tab that holds it. It is
# not a URL, so redact_url cannot reduce it - escapes and quantifiers give its
# '?' and '/' other meanings - and it is masked outright.
SENSITIVE_ARGS = frozenset({
    'text', 'script', 'value', 'password', 'steps', 'action_script',
    'condition', 'key', 'url_pattern',
})

# Argument keys holding a URL. Masking these outright would cost the audit log
# its point - it exists to say what was done - so they are reduced instead;
# see redact_url.
URL_ARGS = frozenset({'url'})


def redact_url(url: Any) -> Any:
    """Keep the part of a URL a log needs and drop the parts that carry secrets.

    The scheme, host and path say which page was acted on, which is the whole
    diagnostic value. Userinfo, the query and the fragment are where
    credentials live: 'https://alice:hunter2@intranet/' sends that password,
    and a password-reset or SSO callback link puts the token in the query or
    the fragment. Each dropped part leaves a marker so the entry does not
    read as though the URL never had one.

    A scheme the guard refuses outright keeps only its name: a javascript: or
    data: URL is a payload rather than a location, and the refusal is what
    the log is recording.

    Anything that is not a string is masked whole. The schema says string,
    but the agent writes the JSON, and {'href': 'https://a:pw@x/'} used to
    be handed back untouched - into audit.jsonl and the server log both.
    Picking a URL out of an arbitrary structure is guesswork, and a value
    the browser cannot navigate to says nothing a log needs. None stays None:
    it is how the audit entry says no URL was known.
    """
    if url is None or url == '':
        return url
    if not isinstance(url, str):
        return '***'
    normalised = _normalise_url(url)
    try:
        parts = urlsplit(normalised)
    except ValueError:
        return '***'
    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        if normalised.lower() == 'about:blank':
            return normalised
        return f'{scheme}:***' if scheme else '***'
    try:
        had_userinfo = '@' in urlsplit(_clean_url_text(url)).netloc
    except ValueError:
        had_userinfo = True
    # _normalise_url has already dropped the userinfo from netloc.
    redacted = f"{scheme}://{'***@' if had_userinfo else ''}{parts.netloc}{parts.path}"
    if parts.query:
        redacted += '?***'
    if parts.fragment:
        redacted += '#***'
    return redacted


def redact_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """A log-safe copy of a tool's arguments, at every depth.

    Top-level keys only used to be looked at, and the agent writes the JSON,
    so {"options": {"password": ...}} reached both logs whole.
    """
    def walk(value):
        if isinstance(value, dict):
            return {k: ('***' if k in SENSITIVE_ARGS
                        else redact_url(v) if k in URL_ARGS
                        else walk(v))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value
    return walk(arguments)


# Accepted values for the two string-valued policy choices. Read through
# SafetyGuard._choice so that "Human" or " human" lands where it was meant to.
_APPROVAL_MODES = ('auto', 'human', 'token')
_UNLISTED_MODES = ('allow', 'confirm')

# Stands in for a permit list whose every pattern failed to compile, so the
# restriction stays switched on and permits nothing instead of evaporating.
_NEVER_MATCHES = re.compile(r'(?!)')

# How long a confirmation token stays valid, and how many can be outstanding.
_TOKEN_TTL_SECONDS = 120
_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_MAX_PENDING_TOKENS = 32


# Screenshots accumulate one file per browser_screenshot call, with
# save_to_file defaulting to true, and nothing ever removed them. They hold
# whatever was on screen, so an unbounded pile of them is the longest-lived
# copy of the user's browsing in the whole project.
def _env_number(name: str, default, cast):
    """Read a numeric environment variable without being able to kill startup.

    These are read at import time, and the native host launches the server
    with stderr=DEVNULL, so a bare float()/int() here turned
    RETENTION_DAYS=7d into a server that never starts and a user who sees
    only restart backoff with no traceback anywhere.
    """
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring {name}={raw!r}: not a number. "
                       f"Using {default}.")
        return default
    if value < 0:
        logger.warning(f"Ignoring {name}={raw!r}: negative. "
                       f"Using {default}.")
        return default
    return value


SCREENSHOT_RETENTION_DAYS = _env_number(
    'CLAUDE_BROWSER_SCREENSHOT_RETENTION_DAYS', 7.0, float)
SCREENSHOT_MAX_FILES = _env_number(
    'CLAUDE_BROWSER_SCREENSHOT_MAX_FILES', 500, int)

# Pruning only ever touches a directory this project created, and this file is
# how it knows. CLAUDE_BROWSER_SCREENSHOTS_DIR can point anywhere - ~/Pictures,
# ~/Desktop, a repo's docs/screenshots - and the retention pass deletes every
# *.png older than the window with no way to tell ours from the user's. So an
# override aimed at an existing directory is never pruned: a privacy feature
# that silently deletes holiday photographs is a worse bug than the retention
# it closes. resolve_screenshots_dir() writes the marker for directories it
# creates; a user who wants an existing directory swept can create it by hand.
OWNED_DIR_MARKER = '.ccb-screenshots'


def prune_screenshots(directory: Path) -> Dict[str, int]:
    """Delete screenshots that are too old or too numerous.

    Retention is a deliberate default rather than "keep everything": set
    CLAUDE_BROWSER_SCREENSHOT_RETENTION_DAYS=0 and
    CLAUDE_BROWSER_SCREENSHOT_MAX_FILES=0 to disable, which is a choice to
    keep an indefinite visual record.
    """
    removed_age = 0
    removed_count = 0
    if not (directory / OWNED_DIR_MARKER).is_file():
        logger.debug(f"Not pruning {directory}: no {OWNED_DIR_MARKER} marker, "
                     "so this directory was not created by us")
        return {'removed_age': 0, 'removed_count': 0}
    try:
        # Case-folded, not glob('*.png'): the filename comes from the caller,
        # so capture.PNG is reachable and was kept for ever.
        shots = sorted(
            (p for p in directory.iterdir()
             if p.suffix.lower() == '.png' and p.is_file()),
            key=lambda p: p.stat().st_mtime)
    except OSError as e:
        logger.warning(f"Could not list screenshots for pruning: {e}")
        return {'removed_age': 0, 'removed_count': 0}

    if SCREENSHOT_RETENTION_DAYS > 0:
        cutoff = time.time() - SCREENSHOT_RETENTION_DAYS * 86400
        remaining = []
        for shot in shots:
            try:
                if shot.stat().st_mtime < cutoff:
                    shot.unlink()
                    removed_age += 1
                else:
                    remaining.append(shot)
            except OSError:
                remaining.append(shot)
        shots = remaining

    if SCREENSHOT_MAX_FILES > 0 and len(shots) > SCREENSHOT_MAX_FILES:
        for shot in shots[:len(shots) - SCREENSHOT_MAX_FILES]:
            try:
                shot.unlink()
                removed_count += 1
            except OSError:
                pass

    if removed_age or removed_count:
        logger.info(f"Pruned screenshots: {removed_age} older than "
                    f"{SCREENSHOT_RETENTION_DAYS}d, {removed_count} over the "
                    f"{SCREENSHOT_MAX_FILES}-file cap")
    return {'removed_age': removed_age, 'removed_count': removed_count}


def _mark_as_ours(path: Path) -> None:
    """Record that this project created the directory, so prune may run."""
    marker = path / OWNED_DIR_MARKER
    if marker.exists():
        return
    try:
        marker.write_text(
            'Created by ClaudeCodeBrowser. Its presence allows the retention '
            'policy to delete *.png files in this directory. Remove it to '
            'keep screenshots indefinitely.\n')
    except OSError as e:
        logger.warning(f"Could not mark {path} as prunable: {e}")


def screenshot_filename(requested: Any, generated: str) -> str:
    """The name a screenshot is written under: no directories, always .png.

    Both rules exist for a bug, and both paths need both of them.

    Path('..').name is '..' and Path('.').name is '', so a filename made of
    nothing but directory components survived the .name strip and resolved to
    the screenshots directory itself or its parent, where the write failed
    with an IsADirectoryError naming a path the caller never asked for.

    And the payload is always a PNG, while prune_screenshots and
    GET /screenshots both look for *.png - so filename "dashboard.jpg" was
    written, never listed and never pruned, leaving the longest-lived copy of
    the user's screen in the project. The suffix is corrected rather than the
    sweep widened: pruning must never consider a file this project did not
    write. _save_screenshot in server.py corrected it for the attended path
    and this branch was missed, so the retention fix the CHANGELOG describes
    as project-wide did not apply in headless mode at all.
    """
    filename = Path(requested or generated).name
    if filename in ('', '.', '..'):
        filename = generated
    if not filename.endswith('.png'):
        filename = Path(filename).with_suffix('.png').name
    return filename


def resolve_screenshots_dir() -> Path:
    """Return the directory screenshots are written to, creating it if needed.

    A screenshot can contain anything that was on screen — open mail, a
    logged-in dashboard — so the default lives in the user's own directory with
    0700 permissions rather than a world-readable shared /tmp. An explicit
    CLAUDE_BROWSER_SCREENSHOTS_DIR is honoured as given: the location is then
    the user's choice and its permissions are left alone. A directory this
    function creates is marked as ours so retention can prune it; one that
    already existed is not, because its other contents are not ours to delete.
    """
    override = os.environ.get('CLAUDE_BROWSER_SCREENSHOTS_DIR')
    if override:
        path = Path(override).expanduser()
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            _mark_as_ours(path)
        return path

    path = Path.home() / '.claudecodebrowser' / 'screenshots'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _mark_as_ours(path)
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
        # Patterns that would not compile, so browser_safety_status can say
        # so. The only other record is a log line, and the native host runs
        # the server with stderr=DEVNULL.
        self._pattern_errors: List[str] = []

        def record_error(key, pattern, error):
            self._pattern_errors.append(f'{key}: {pattern!r} ({error})')
            logger.error(f"Invalid regex in safety.json {key}: {pattern!r} ({error})")

        def compile_list(key):
            patterns = []
            for pattern in self.config.get(key, []) or []:
                try:
                    patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    record_error(key, pattern, e)
            return patterns

        def compile_permit_list(key):
            """Compile a list whose patterns grant access, not refuse it.

            re.search was wrong for these. The pattern "^https://localhost"
            also matched https://localhost.evil.com/x and
            https://localhostile.io/, so in allowlist mode - the strictest
            setting on offer - any name an attacker can register that merely
            starts with an allowed one opened the guard, and then reads on
            that page were free too. A permit pattern has to cover a whole
            URL prefix (anchored at the start, ending where the authority or
            a path segment ends) or match the host outright; _permitted()
            does both tests on the match.

            The pattern is compiled exactly as written. Wrapping it in
            "(?:...)(?=...)" to do the delimiter test moved a leading "(?i)",
            "(?s)" or "(?m)" off position 0, which Python rejects outright -
            so every such pattern was dropped, and a permit list with nothing
            left in it is a permit list that permits everything.
            """
            configured = self.config.get(key, []) or []
            patterns = []
            for pattern in configured:
                try:
                    patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    record_error(key, pattern, e)
            if configured and not patterns:
                # Fail closed. allowed_url_patterns is only consulted when it
                # is non-empty, so a list whose patterns all failed to
                # compile switched allowlist mode off and allowed every URL:
                # the user asked for the strictest confinement on offer and
                # got none of it. The sentinel matches nothing, so the mode
                # stays on and refuses everything until the config is fixed.
                self._pattern_errors.append(
                    f'{key}: no pattern compiled, so nothing is permitted')
                logger.error(
                    f"Every pattern in safety.json {key} failed to compile. "
                    f"The restriction stays on and permits nothing - fix the "
                    f"patterns in {_CONFIG_FILE}.")
                patterns.append(_NEVER_MATCHES)
            return patterns

        self._blocked = compile_list('blocked_url_patterns')
        self._allowed = compile_permit_list('allowed_url_patterns')
        self._protected = compile_list('protected_url_patterns')
        self._trusted = compile_permit_list('trusted_url_patterns')

    # ------------------------------------------------------------------ #
    # Public API

    def check(self, tool_name: str, arguments: Dict[str, Any],
              human_approved: bool = False) -> Optional[Dict[str, Any]]:
        """Return None if the call may proceed, or an error dict to send back.

        human_approved=True means the person at the browser explicitly
        approved this exact call (via the in-page Approve/Deny overlay), which
        satisfies the protected-domain confirmation requirement.
        """
        # browser_find_tabs takes url as a prefix to filter the tab list by,
        # not a page to go to. Judged as a target, a bare host was refused as
        # a bad scheme, an allowlist refused listing tabs - which url_pattern
        # and browser_get_tabs do unchecked - and on a blocked page the prefix
        # was judged instead of the page, so this was the one call that still
        # went through. browser_reload_by_url acts on what it matches, so its
        # prefix is still the target.
        target_url = (None if tool_name in TAB_FILTER_TOOLS
                      else self._target_url(arguments))

        # The schema says string, but the agent writes the JSON. A dict or a
        # list was not judged as a target at all - the current page was
        # judged instead - and was then forwarded to the browser unchecked.
        url_arg = arguments.get('url')
        if url_arg is not None and not isinstance(url_arg, str):
            denial = self._deny('invalid_url',
                                f'{tool_name} refused: "url" must be a string.')
            self._audit(tool_name, arguments, None, 'invalid_url')
            return denial

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
        """Track the browser's current URL from a tool result, if it has one.

        Results come in two shapes: navigate and getPageInfo report {"url":
        ...} at the top level, while a screenshot reports {"tab": {"id",
        "url", "title"}}. The screenshot shape is the one that always carries
        a URL, so both have to be read here or the guard keeps judging the
        page the browser has already left.
        """
        if not isinstance(result, dict):
            return
        tab = result.get('tab')
        url = result.get('url')
        if not url and isinstance(tab, dict):
            url = tab.get('url')
        if isinstance(url, str) and url:
            with self._lock:
                self._current_url = _normalise_url(url)

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
            'protected_approval': self._choice('protected_approval',
                                               _APPROVAL_MODES, 'auto'),
            'allow_password_typing': self.config.get('allow_password_typing', False),
            # browser_execute_script can read any field regardless of the
            # credential guard, so say which of the two states we are in.
            'credential_guard': (
                'advisory' if self.config.get('allow_script_execution', True)
                and not self.config.get('deny_scripts_on_protected_urls', True)
                else 'enforced_except_scripts'
                if self.config.get('allow_script_execution', True)
                else 'enforced'),
            'credential_guard_note': (
                'browser_execute_script can read any field, including password '
                'fields, so the credential guard constrains the dedicated tools '
                'but not arbitrary JavaScript. Set allow_script_execution: false '
                'to close that, or rely on deny_scripts_on_protected_urls for '
                'protected sites only.'),
            'unlisted_domains': self._choice('unlisted_domains',
                                             _UNLISTED_MODES, 'allow'),
            'trusted_url_patterns': self.config.get('trusted_url_patterns', []),
            'deny_scripts_on_protected_urls': self.config.get(
                'deny_scripts_on_protected_urls', True),
            # Every tool those two settings cover, including the one that
            # only observes: browser_audit_page runs a fixed script of ours,
            # which is still JavaScript in the page. How far the toggle
            # reaches is what a person inspects this tool to find out.
            'script_tools': sorted(SCRIPT_TOOLS | OBSERVE_SCRIPT_TOOLS),
            'max_actions_per_minute': self.config.get('max_actions_per_minute', 120),
            'actions_in_last_minute': recent,
            'pending_confirmations': pending,
            'current_url': current_url,
            'blocked_url_patterns': self.config.get('blocked_url_patterns', []),
            'allowed_url_patterns': self.config.get('allowed_url_patterns', []),
            'protected_url_patterns': self.config.get('protected_url_patterns', []),
            # A pattern that would not compile is a policy the user asked
            # for and did not get, so it is part of the status, not just a log
            # line the native host throws away.
            'pattern_errors': list(self._pattern_errors),
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
            if self._allowed and not self._permitted(self._allowed, policy_url):
                return self._deny('not_allowlisted',
                                  f"URL {policy_url!r} does not match allowed_url_patterns "
                                  f"in safety.json (allowlist mode is active). A pattern "
                                  f"there has to cover a whole URL prefix or match the "
                                  f"whole host: \"example\\.com\" covers example.com and "
                                  f"not www.example.com, so write "
                                  f"\"(.+\\.)?example\\.com\" for a whole domain tree.")

        # 3b. A tool that observes by running a script of ours is still
        #     JavaScript in the page, so the script rules are checked before
        #     the observe shortcut, which is where this one slipped through.
        if tool_name in OBSERVE_SCRIPT_TOOLS:
            denial = self._script_denial(tool_name, target_url)
            if denial is not None:
                return denial

        if is_observe:
            return None

        # 3. Read-only mode blocks all state-changing tools.
        if self.config.get('read_only', False):
            return self._deny('read_only',
                              f"{tool_name} refused: safety guard is in read-only mode "
                              f"(read_only in safety.json or CLAUDE_BROWSER_READ_ONLY=1). "
                              f"Observation tools like browser_screenshot remain available.")

        # 4. Script rules: the toggle, and the protected-site refusal.
        if is_script:
            denial = self._script_denial(tool_name, target_url)
            if denial is not None:
                return denial

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
                    'approval_mode': self._choice('protected_approval',
                                                  _APPROVAL_MODES, 'auto'),
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

    def _script_denial(self, tool_name: str,
                       target_url: Optional[str]) -> Optional[Dict[str, Any]]:
        """The two rules that apply to any tool running JavaScript in a page.

        Shared by the script tools and by the observe tools that run a script
        of ours, so the answer cannot differ between them.
        """
        if not self.config.get('allow_script_execution', True):
            return self._deny('scripts_disabled',
                              f"{tool_name} refused: script execution is disabled "
                              f"(allow_script_execution in safety.json or "
                              f"CLAUDE_BROWSER_ALLOW_SCRIPTS=0).")

        # Arbitrary JavaScript on a protected site is refused outright, not
        # merely confirmed. A script can read any field - the credential guard
        # does not apply to it - and a confirmation the agent itself can
        # satisfy is no control over that.
        if self.config.get('deny_scripts_on_protected_urls', True):
            script_url = target_url if target_url is not None else self._current_url
            matched = self._matched_protected(script_url)
            if matched:
                return self._deny(
                    'scripts_denied_on_protected_url',
                    f"{tool_name} refused: {script_url!r} matches protected pattern "
                    f"{matched!r}, and arbitrary JavaScript is not confirmable on a "
                    f"protected site - a script can read any field on the page, "
                    f"including credentials. Use the specific tool for what you "
                    f"need, or set \"deny_scripts_on_protected_urls\": false in "
                    f"safety.json if you accept that.")
        return None

    def _target_url(self, arguments: Dict[str, Any]) -> Optional[str]:
        url = arguments.get('url')
        if not (isinstance(url, str) and url):
            return None
        return _normalise_url(url)

    @staticmethod
    def _permitted(patterns: List[Any], url: str) -> bool:
        """True when one permit pattern covers the whole of what it names."""
        try:
            host = urlsplit(url).hostname or ''
        except ValueError:
            host = ''
        # A permit pattern may not be satisfied by the query string or the
        # fragment. That text is whatever the page author put there, so
        # README's own ".*\.stripe\.com" granted
        # https://evil.com/?x=a.stripe.com. A pattern that genuinely
        # constrains a query string stops working here, and fails closed.
        granting_end = len(url)
        for found in (url.find('?'), url.find('#')):
            if found != -1:
                granting_end = min(granting_end, found)
        # The prefixes a pattern is allowed to cover: the text up to each
        # delimiter, and the whole granting part. Asking whether the pattern
        # covers one of these, rather than testing where its own match
        # happened to land, is what makes the rule mean what it says. A
        # single pattern.match() returns the engine's first greedy match, so
        # with "^https://example\.com(/foo)?" that match ran into "foobar"
        # and ended at no delimiter at all: https://example.com/foobar was
        # refused while https://example.com/bar was permitted, even though
        # the pattern covers "https://example.com" in both.
        prefix_ends = [i for i, char in enumerate(url[:granting_end])
                       if char in _URL_DELIMITERS]
        prefix_ends.append(granting_end)
        for pattern in patterns:
            # Anchored at the start of the URL and required to stop where the
            # authority or a path segment stops: "^https://localhost" covers
            # https://localhost:3000/app because it covers the prefix ending
            # at the ':', and cannot cover https://localhost.evil.com/x
            # because no prefix there ends after "localhost".
            if any(pattern.fullmatch(url, 0, end) for end in prefix_ends):
                return True
            # A bare host pattern ("localhost", r".*\.example\.com") names a
            # host, so it has to match all of one. It covers that host only:
            # r"example\.com" does not cover www.example.com, which is a
            # deliberate break with the old re.search behaviour - write
            # r"(.+\.)?example\.com" for the whole tree.
            if host and pattern.fullmatch(host):
                return True
        return False

    def _choice(self, key: str, valid: Tuple[str, ...], default: str) -> str:
        """Read a string config choice without being picky about case.

        These were compared with ==, so "Human" or " human" matched nothing
        and fell through to whichever branch the mismatch landed in - for
        protected_approval that is the weaker agent-side token flow, which is
        the opposite of what the person who typed it asked for. An
        unrecognised value falls back to the documented default rather than
        to whatever the comparison happens to miss.
        """
        raw = self.config.get(key, default)
        value = raw.strip().lower() if isinstance(raw, str) else ''
        if value in valid:
            return value
        logger.warning(f"Ignoring {key}={raw!r} in safety.json: expected one "
                       f"of {', '.join(valid)}. Using {default!r}.")
        return default

    def _matched_protected(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        for pattern in self._protected:
            if pattern.search(url):
                return pattern.pattern

        # Inverted mode: anything not explicitly trusted is protected.
        if self._choice('unlisted_domains', _UNLISTED_MODES, 'allow') == 'confirm':
            if self._permitted(self._trusted, url):
                return None
            return 'unlisted_domains: confirm'
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
            # Reduced the same way as a url argument: this field is the
            # normalised URL, which drops userinfo but keeps a query string,
            # so a reset token used to be recorded here in clear even when
            # the argument was masked.
            'url': redact_url(target_url or self._current_url),
            'args': redact_arguments(arguments),
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
