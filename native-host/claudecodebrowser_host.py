#!/usr/bin/env python3
"""
ClaudeCodeBrowser Native Messaging Host

This script acts as a bridge between the Firefox extension and the MCP server.
It receives messages from the extension via native messaging and forwards them
to the MCP server via HTTP or WebSocket.

Features:
- Auto-starts MCP server if not running
- Monitors server health and restarts on failure
- Self-healing with exponential backoff
- No external process managers required

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com
"""

import sys
import json
import struct
import threading
import queue
import logging
import os
import socket
import time
import re
import signal
import subprocess
from pathlib import Path
from datetime import datetime

# Configure logging
LOG_DIR = Path.home() / '.claudecodebrowser' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'native_host.log'

# Rotate log if too large (>5MB)
if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
    backup = LOG_FILE.with_suffix('.log.old')
    if backup.exists():
        backup.unlink()
    LOG_FILE.rename(backup)

# INFO, not DEBUG. At DEBUG this file was a verbatim transcript of every
# message in both directions - page text, tab URLs and titles, typed text,
# base64 screenshots - in cleartext, with no redaction anywhere in this file,
# defeating the masking that server.py and safety.py apply to their own logs.
# CLAUDE_BROWSER_HOST_DEBUG=1 restores it for debugging; see describe_message.
_HOST_DEBUG = os.environ.get('CLAUDE_BROWSER_HOST_DEBUG') == '1'

class _PrivateFileHandler(logging.FileHandler):
    """File handler that keeps its file to this user, including on re-open."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


# The log lives next to the API token; keep both to this user.
try:
    LOG_DIR.chmod(0o700)
except OSError:
    pass

logging.basicConfig(
    level=logging.DEBUG if _HOST_DEBUG else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        _PrivateFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)


def describe_message(message):
    """A log-safe summary of a native message.

    Shape and size only: never the payload. A command's data can hold typed
    text and a response's can hold page text or a screenshot, and this file is
    not the place for either.
    """
    if not isinstance(message, dict):
        return f'<{type(message).__name__}>'
    parts = []
    for key in ('action', 'requestId', 'tabId'):
        if key in message:
            parts.append(f'{key}={message[key]!r}')
    if 'success' in message:
        parts.append(f'success={message["success"]!r}')
    if 'error' in message:
        parts.append('error=yes')
    data = message.get('data')
    if isinstance(data, dict):
        # A command's keys are our own argument names. A response's come from
        # the page (a script can return a copy of localStorage), so only count
        # them.
        if 'action' in message:
            parts.append(f'data_keys={sorted(data.keys())}')
        else:
            parts.append(f'data_keys={len(data)}')
    for key in ('data', 'result', 'text', 'logs', 'elements'):
        value = message.get(key)
        if isinstance(value, str):
            parts.append(f'{key}_len={len(value)}')
    return ' '.join(parts) or '<no action>' 

# Configuration
# Use 127.0.0.1 to avoid IPv6 resolution issues (localhost may resolve to ::1 first)
MCP_SERVER_HOST = os.environ.get('CLAUDE_MCP_HOST', '127.0.0.1')
MCP_SERVER_PORT = int(os.environ.get('CLAUDE_MCP_PORT', '8765'))
MCP_SERVER_URL = f'http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}'

# API token for authenticating requests to the MCP server
_TOKEN_FILE = Path.home() / '.claudecodebrowser' / 'api_token'


def _mcp_headers() -> dict:
    """Return HTTP headers including the API token if the token file exists."""
    headers = {'Content-Type': 'application/json'}
    try:
        if _TOKEN_FILE.exists():
            headers['X-API-Key'] = _TOKEN_FILE.read_text().strip()
    except Exception:
        pass
    return headers


# Health monitoring settings
HEALTH_CHECK_INTERVAL = 10  # seconds
MAX_RESTART_ATTEMPTS = 10
BACKOFF_BASE = 2  # seconds, exponential backoff

# Message queues for async communication
incoming_queue = queue.Queue()
outgoing_queue = queue.Queue()

# Server process tracking
server_process = None
server_pid_file = LOG_DIR.parent / 'mcp_server.pid'
restart_attempts = 0
last_restart_time = 0
health_monitor_running = True


# Native messaging framing.
#
# '@I' is NATIVE byte order, and that is correct: Firefox documents the length
# prefix as "a 32-bit value in native byte order", so '<I' would be wrong on a
# big-endian host rather than more portable.
NATIVE_LENGTH_FORMAT = '@I'

# The documented size limits are asymmetric, so these two are not the same
# number: a message from the add-on to this application may be up to 4 GB,
# while a message from this application back to the add-on must stay under
# 1 MB. The inbound cap is therefore NOT 1 MB - one screenshot data URL is
# routinely larger than that, and a 1 MB inbound cap would drop real
# screenshots. It is a sanity bound only: a corrupt length prefix must not
# make us attempt a multi-gigabyte read.
MAX_INCOMING_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_OUTGOING_MESSAGE_BYTES = 1024 * 1024


class FramingError(Exception):
    """The stdin byte stream is no longer aligned to a message boundary.

    After a truncated or impossible length prefix there is no way to find
    where the next message starts, so the connection cannot continue. Kept
    distinct from EOF, which is Firefox closing the pipe normally.
    """


class MessageDecodeError(Exception):
    """One frame's bytes were not valid JSON.

    The frame was read in full, so the stream is still aligned: this one
    message is dropped and the next one can be read.
    """


def _read_exactly(stream, count):
    """Read exactly count bytes, or raise FramingError.

    read() on a pipe may return fewer bytes than asked for. The old code read
    the whole message in one call and used whatever came back, so a short read
    silently truncated the message and surfaced as a JSON error that looked
    like the extension had sent nonsense.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise FramingError(
                f'stream ended {remaining} bytes short of a {count}-byte frame')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def read_message(stream=None):
    """Read one message from the extension using the native messaging protocol.

    Returns None at EOF, which means Firefox closed the pipe. Raises
    MessageDecodeError for a single unreadable frame (the stream is still
    usable) and FramingError when the stream can no longer be trusted. All
    three used to return None, so the caller could not tell a closed pipe from
    one bad frame and shut the host down either way.
    """
    stream = sys.stdin.buffer if stream is None else stream

    raw_length = stream.read(4)
    if len(raw_length) == 0:
        return None
    if len(raw_length) < 4:
        # A prefix split across two reads is legitimate; a stream that ends
        # inside one is not.
        try:
            raw_length += _read_exactly(stream, 4 - len(raw_length))
        except FramingError as e:
            raise FramingError(f'truncated length prefix: {e}') from e

    message_length = struct.unpack(NATIVE_LENGTH_FORMAT, raw_length)[0]
    if message_length == 0:
        raise FramingError('length prefix claims a zero-length message')
    if message_length > MAX_INCOMING_MESSAGE_BYTES:
        raise FramingError(
            f'length prefix claims {message_length} bytes, over the '
            f'{MAX_INCOMING_MESSAGE_BYTES}-byte sanity limit')

    payload = _read_exactly(stream, message_length)
    try:
        message = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise MessageDecodeError(str(e)) from e
    if not isinstance(message, dict):
        # `5`, `"hi"`, `[1,2,3]` and `true` are all valid JSON, so they used
        # to come straight back and reach message.get() in main() as an
        # AttributeError - logged as "Fatal error", then sys.exit(1). A frame
        # of `null` was worse: it returned None, which main() cannot tell
        # from Firefox closing the pipe. The frame was read in full, so the
        # stream is still aligned: one bad message, not a dead stream.
        raise MessageDecodeError(
            f'expected a JSON object, got {type(message).__name__}')
    return message


_send_lock = threading.Lock()


def send_message(message, stream=None):
    """Send a message to the extension using the native messaging protocol."""
    stream = sys.stdout.buffer if stream is None else stream
    try:
        encoded = json.dumps(message).encode('utf-8')

        if len(encoded) > MAX_OUTGOING_MESSAGE_BYTES:
            # Firefox drops a message over its 1 MB limit and tears the port
            # down, so sending it anyway loses the message AND the
            # connection: the extension sees the host vanish with no
            # explanation, and whoever was waiting on this requestId waits
            # out its timeout. Send a small failure in its place instead,
            # carrying the same requestId so the waiter gets an answer.
            logger.error(
                f"Outgoing message is {len(encoded)} bytes, over the "
                f"{MAX_OUTGOING_MESSAGE_BYTES}-byte native messaging limit; "
                f"sending a failure in its place")
            replacement = {
                'success': False,
                'error': (f'The native host had a {len(encoded)}-byte response, '
                          f'over the {MAX_OUTGOING_MESSAGE_BYTES}-byte native '
                          f'messaging limit, so it was not sent.'),
            }
            if isinstance(message, dict) and message.get('requestId') is not None:
                replacement['requestId'] = message['requestId']
            encoded = json.dumps(replacement).encode('utf-8')

            if len(encoded) > MAX_OUTGOING_MESSAGE_BYTES:
                # The requestId is copied straight out of the message that
                # did not fit, so it can be the reason it did not fit: a
                # 2 MB requestId made the replacement break the same limit,
                # and Firefox drops THAT and tears the port down - the exact
                # outcome this replacement exists to prevent. Drop the
                # correlation id rather than the port. Whoever was waiting
                # on it waits out its timeout, but the connection survives
                # and every later command still works.
                logger.error(
                    f"The replacement failure is {len(encoded)} bytes too, "
                    f"because the requestId itself is oversized; sending it "
                    f"without the requestId")
                replacement.pop('requestId', None)
                encoded = json.dumps(replacement).encode('utf-8')

        # One frame at a time: the polling thread and the main thread both
        # send, and a length prefix from one followed by a body from the
        # other is a corrupt frame that makes Firefox tear the port down.
        with _send_lock:
            stream.write(struct.pack(NATIVE_LENGTH_FORMAT, len(encoded)))
            stream.write(encoded)
            stream.flush()

        logger.debug(f"Sent message: {describe_message(message)}")
    except Exception as e:
        logger.error(f"Error sending message: {e}")


def forward_to_mcp_server(message):
    """Forward a message to the MCP server via HTTP."""
    import urllib.request
    import urllib.error

    try:
        url = f'{MCP_SERVER_URL}/browser/command'
        data = json.dumps(message).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers=_mcp_headers()
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result

    except urllib.error.URLError as e:
        logger.error(f"Failed to connect to MCP server: {e}")
        return {'success': False, 'error': f'MCP server connection failed: {str(e)}'}
    except Exception as e:
        logger.error(f"Error forwarding to MCP server: {e}")
        return {'success': False, 'error': str(e)}


def check_mcp_server():
    """True only if OUR MCP server is listening on the port.

    The old check accepted any response containing "ok", which meant anything
    that could bind 127.0.0.1:8765 before Firefox started was treated as the
    server: it then received the API token on every poll and every command
    response, and anything it returned from /browser/poll was executed in the
    browser with safety.py never consulted, because the real server was never
    started. So identity is now proved against the shared token, which only
    our server can have read from the 0600 token file.

    /health stays unauthenticated (it is the liveness probe); the identity
    check is a separate authenticated request.
    """
    import urllib.request
    import urllib.error

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((MCP_SERVER_HOST, MCP_SERVER_PORT))
        sock.close()
        if result != 0:
            return False
    except Exception:
        return False

    return _server_proves_identity()


def _server_proves_identity():
    """Ask the listener for something only our server can answer.

    A squatter without the token gets 403 from every authenticated endpoint,
    and one that echoes 200 for everything fails the token-mismatch probe.
    """
    import urllib.request
    import urllib.error

    token = _read_api_token()
    if not token:
        # No token file yet: nothing has started a server, so there is nothing
        # of ours on that port to find.
        logger.warning("No API token available; cannot verify the listener on "
                       f"port {MCP_SERVER_PORT}")
        return False

    def status_for(api_key):
        req = urllib.request.Request(f'{MCP_SERVER_URL}/mcp/tools')
        if api_key is not None:
            req.add_header('X-API-Key', api_key)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, response.read(4096)
        except urllib.error.HTTPError as e:
            return e.code, b''
        except Exception:
            return None, b''

    # 1. The real token must be accepted and return the tool list.
    status, body = status_for(token)
    if status != 200 or b'browser_screenshot' not in body:
        logger.error(f"Listener on port {MCP_SERVER_PORT} did not answer an "
                     f"authenticated request as our MCP server would "
                     f"(status {status}). Treating it as foreign.")
        return False

    # 2. A wrong token must be refused. An endpoint that returns 200 for
    #    anything is not enforcing our token and is not our server.
    status, _ = status_for('0' * 64)
    if status == 200:
        logger.error(f"Listener on port {MCP_SERVER_PORT} accepted an invalid "
                     f"API key. Treating it as foreign.")
        return False

    return True


def _read_api_token():
    """The shared secret, or None if it does not exist yet."""
    try:
        if _TOKEN_FILE.exists():
            return _TOKEN_FILE.read_text().strip() or None
    except Exception as e:
        logger.warning(f"Could not read the API token: {e}")
    return None


# Our own server is the only process this host may ever kill. Anything else
# listening on the port belongs to the user — a dev server, a database, a
# container proxy — and Firefox launches this host automatically, so a
# mistaken kill would be an unprompted termination of someone's work.
_SERVER_SCRIPT_MARKERS = ('mcp-server/server.py', r'mcp-server\server.py')


def _pids_on_port(port):
    """PIDs listening on a TCP port, or [] when they cannot be determined."""
    try:
        result = subprocess.run(
            [_LSOF, '-ti', f'TCP:{port}', '-sTCP:LISTEN'],
            capture_output=True,
            text=True
        )
    except FileNotFoundError:
        logger.warning("lsof not available: cannot identify what holds the port, "
                       "so nothing will be killed")
        return []
    except Exception as e:
        logger.warning(f"Could not list processes on port {port}: {e}")
        return []

    pids = []
    for line in result.stdout.split():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return pids


# Absolute paths: these run with whatever PATH Firefox inherited, and a
# planted lsof/ps would turn the terminate path into an arbitrary-PID killer.
_PS = '/bin/ps'
_LSOF = '/usr/sbin/lsof' if os.path.exists('/usr/sbin/lsof') else '/usr/bin/lsof'


def _process_command(pid):
    """The full command line of a PID, or None when it cannot be read."""
    try:
        result = subprocess.run(
            [_PS, '-p', str(pid), '-o', 'command='],
            capture_output=True,
            text=True
        )
    except Exception as e:
        logger.warning(f"Could not read the command line of PID {pid}: {e}")
        return None
    command = result.stdout.strip()
    return command or None


def _process_identity(pid):
    """(start time, command) for a PID - enough to notice PID reuse.

    SIGTERM, a 2 second wait and then SIGKILL leaves a window in which the OS
    can recycle the PID onto an unrelated process. Re-checking identity before
    each signal closes it.
    """
    try:
        result = subprocess.run(
            [_PS, '-p', str(pid), '-o', 'lstart=,command='],
            capture_output=True,
            text=True
        )
    except Exception:
        return None
    line = result.stdout.strip()
    return line or None


# A process is ours only if a Python interpreter is running our script. The
# marker alone is not enough: as a bare substring it matched
# "vim .../mcp-server/server.py", and even as an argument it still matches
# "tail -f .../mcp-server/server.py" - an editor or tail holding that path
# open is not a server, and killing it would be someone's unsaved work.
_INTERPRETER_RE = re.compile(r'(^|/)(python|python3|python3\.\d+|pythonw)$')


def _is_our_server(pid):
    """True only when the PID is a Python interpreter whose *script argument*
    is our server script.

    Checking every argument was still too loose: "python3 -m http.server
    8765 # mcp-server/server.py" has the marker in a trailing comment. Only
    the script Python was actually told to run counts.
    """
    command = _process_command(pid)
    if not command:
        return False
    parts = command.split()
    if not parts or not _INTERPRETER_RE.search(parts[0]):
        return False

    script = None
    arguments = parts[1:]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in ('-m', '-c'):
            # Running a module or an inline program, not a script file.
            return False
        if argument.startswith('-'):
            index += 1
            continue
        script = argument
        break

    if script is None:
        return False
    return any(script.endswith(marker) for marker in _SERVER_SCRIPT_MARKERS)


def _terminate(pid):
    """SIGTERM, then SIGKILL if it is still alive - re-verifying identity."""
    identity = _process_identity(pid)
    if identity is None:
        return
    try:
        logger.info(f"Sending SIGTERM to our MCP server: PID {pid}")
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning(f"Not permitted to signal PID {pid}")
        return

    time.sleep(2.0)

    # The PID may have been recycled during the wait. Only escalate if it is
    # still the same process AND still ours.
    if _process_identity(pid) != identity or not _is_our_server(pid):
        logger.info(f"PID {pid} is no longer the process we signalled; "
                    f"not escalating to SIGKILL")
        return
    try:
        os.kill(pid, 0)
        logger.warning(f"PID {pid} still alive after SIGTERM, sending SIGKILL")
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_existing_server():
    """Clear a stale MCP server off the port.

    Only processes running our own server script are terminated. If something
    else holds the port, it is left alone and this returns False — the port is
    not ours to take, and the caller reports a startup failure instead.
    """
    killed = False
    for pid in _pids_on_port(MCP_SERVER_PORT):
        if _is_our_server(pid):
            _terminate(pid)
            killed = True
        else:
            # Deliberately not logging the foreign command line: this file is
            # readable by other local users and another process's argv can
            # contain their credentials.
            logger.error(
                f"Port {MCP_SERVER_PORT} is held by PID {pid}, which is not our MCP "
                f"server. Leaving it alone. Set CLAUDE_MCP_PORT to use a "
                f"different port."
            )

    if killed:
        time.sleep(0.5)
    return killed


# Children we have spawned, so they can be waited on rather than left as
# zombies. The server is started with start_new_session, but it is still our
# child until reaped.
#
# The lock matters: this list is read-modify-written from the health-monitor
# thread (through start_mcp_server), from the main thread and from the signal
# handler (through shutdown). Rebuilding it unlocked could drop a child that
# another thread had just appended - exactly the one it was meant to reap.
#
# RLock, not Lock, precisely because of that signal handler. Python runs
# handlers on the main thread, so SIGTERM can land while the main thread is
# already inside _reap_finished_children: the handler calls shutdown(), which
# reaps again, and a non-reentrant lock made that a self-deadlock. The host
# then ignored SIGTERM and SIGINT and needed SIGKILL - and Firefox SIGTERMs
# native hosts when the port closes, so they accumulated.
_spawned_children = []
_spawned_children_lock = threading.RLock()


def _reap_finished_children():
    """Reap any finished child so it does not linger as a zombie.

    poll() does the reaping: it is waitpid(WNOHANG) and it sets returncode.
    The old code followed a non-None poll() with child.wait(timeout=0), which
    could only ever be a no-op on a process poll() had already reaped.
    """
    with _spawned_children_lock:
        still_running = []
        for child in _spawned_children:
            try:
                finished = child.poll() is not None
            except Exception as e:
                # Cannot tell; keep it and try again on the next pass rather
                # than forgetting a child we may still have to reap.
                logger.debug(f"Could not poll child: {e}")
                finished = False
            if not finished:
                still_running.append(child)
        _spawned_children[:] = still_running


def start_mcp_server():
    """Start the MCP server if it's not running."""
    global server_process, restart_attempts, last_restart_time

    # Before the backoff check, not after it: a dead child must be reaped even
    # on a call that gives up early.
    _reap_finished_children()

    # Check backoff
    now = time.time()
    if restart_attempts > 0:
        backoff_time = min(BACKOFF_BASE ** restart_attempts, 60)  # Cap at 60 seconds
        time_since_last = now - last_restart_time
        if time_since_last < backoff_time:
            logger.debug(f"Backoff: waiting {backoff_time - time_since_last:.1f}s before restart")
            return False

    # Reset attempts after 5 minutes of stability
    if restart_attempts > 0 and now - last_restart_time > 300:
        logger.info("Resetting restart counter after stability period")
        restart_attempts = 0

    if restart_attempts >= MAX_RESTART_ATTEMPTS:
        logger.error(f"Max restart attempts ({MAX_RESTART_ATTEMPTS}) reached. Manual intervention required.")
        return False

    # Find server script - check multiple locations
    possible_paths = [
        Path(__file__).parent.parent / 'mcp-server' / 'server.py',
        Path.home() / '.claudecodebrowser' / 'mcp-server' / 'server.py',
    ]

    mcp_server_path = None
    for path in possible_paths:
        if path.exists():
            mcp_server_path = path
            break

    if not mcp_server_path:
        logger.error(f"MCP server script not found in any location")
        return False

    try:
        # Start server as a detached subprocess
        server_process = subprocess.Popen(
            [sys.executable, str(mcp_server_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True  # Detach from parent process
        )

        restart_attempts += 1
        last_restart_time = time.time()

        with _spawned_children_lock:
            _spawned_children.append(server_process)
        logger.info(f"Started MCP server (PID: {server_process.pid}, attempt #{restart_attempts})")

        # Save PID for tracking
        try:
            with open(server_pid_file, 'w') as f:
                f.write(str(server_process.pid))
        except Exception:
            pass

        # Wait briefly for server to start
        for _ in range(15):  # Try for up to 3 seconds
            time.sleep(0.2)
            if check_mcp_server():
                logger.info("MCP server is now available")
                restart_attempts = 0  # Reset on successful start
                return True

        logger.warning("MCP server started but not yet responding")
        return True  # Server started, may just need more time

    except Exception as e:
        logger.error(f"Failed to start MCP server: {e}")
        return False


def is_port_in_use():
    """Check if the MCP server port is in use (regardless of whether it responds)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((MCP_SERVER_HOST, MCP_SERVER_PORT))
        sock.close()
        return result == 0
    except Exception:
        return False


def ensure_mcp_server():
    """Ensure MCP server is running, start it if not."""
    # Check if server is running and responding
    if check_mcp_server():
        logger.info("MCP server already running and responding")
        return True

    # The port is busy with something that did not prove it is ours: either a
    # stale server of ours (kill it) or a foreign process (leave it, and do
    # not hand it anything).
    if is_port_in_use():
        logger.warning("Port in use but the listener did not prove it is our "
                       "server - clearing a stale process if it is ours")
        if not kill_existing_server():
            logger.error(
                f"Port {MCP_SERVER_PORT} is held by a process that is not our MCP "
                f"server. Refusing to talk to it or to start a server that cannot "
                f"bind. Free the port or set CLAUDE_MCP_PORT.")
            return False

    logger.info("MCP server not running, attempting to start...")
    return start_mcp_server()


# Mirrors OWNED_DIR_MARKER in mcp-server/safety.py: the server prunes only a
# directory carrying it. A test holds the two names together.
OWNED_DIR_MARKER = '.ccb-screenshots'


def _screenshots_dir():
    """The screenshots directory, created and marked as resolve_screenshots_dir
    in mcp-server/safety.py does it; this host is installed on its own and
    cannot import that.

    A screenshot can contain anything that was on screen, so the default is
    the user's own directory at 0700, not a shared /tmp. A directory created
    here is marked so the server's retention can prune it: without the
    marker, a custom directory the host happened to create first was never
    pruned. One that already existed is left unmarked - its other contents
    are not ours to delete.
    """
    override = os.environ.get('CLAUDE_BROWSER_SCREENSHOTS_DIR')
    if override:
        path = Path(override).expanduser()
        created = not path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
    else:
        path = Path.home() / '.claudecodebrowser' / 'screenshots'
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = True
        try:
            # mkdir's mode only applies on creation.
            path.chmod(0o700)
        except OSError as e:
            logger.warning(f"Could not restrict permissions on {path}: {e}")
    marker = path / OWNED_DIR_MARKER
    if created and not marker.exists():
        try:
            marker.write_text(
                'Created by ClaudeCodeBrowser. Its presence allows the retention '
                'policy to delete *.png files in this directory. Remove it to '
                'keep screenshots indefinitely.\n')
        except OSError as e:
            logger.warning(f"Could not mark {path} as prunable: {e}")
    return path


def handle_local_command(message):
    """Handle commands that don't need MCP server."""
    action = message.get('action')

    if action == 'ping':
        return {'success': True, 'pong': True, 'timestamp': time.time()}

    elif action == 'status':
        mcp_available = check_mcp_server()
        return {
            'success': True,
            'mcp_server': {
                'url': MCP_SERVER_URL,
                'available': mcp_available
            },
            'native_host': {
                'version': '1.0.0',
                'pid': os.getpid()
            }
        }

    elif action == 'saveScreenshot':
        # Save screenshot to file. Only reached when the extension explicitly
        # asks for it: responses that merely happen to carry image data are
        # left to server.py, which saves them under the caller's own filename
        # (see process_message).
        try:
            data = message.get('data', '')
            # Strip directory components to prevent path traversal
            generated = f'screenshot_{int(time.time())}.png'
            filename = Path(message.get('filename') or generated).name
            # Path('.').name is '' and Path('..').name is '..', so a filename
            # made only of directory components pointed at the screenshots
            # directory or its parent instead of a file in it.
            if filename in ('', '.', '..'):
                filename = generated
            # Retention only ever considers *.png, so anything else was kept
            # for ever. Mirrors screenshot_filename() in safety.py.
            if not filename.endswith('.png'):
                filename = Path(filename).with_suffix('.png').name

            screenshots_dir = _screenshots_dir()

            filepath = screenshots_dir / filename

            # Handle base64 data URL
            if data.startswith('data:image'):
                import base64
                header, encoded = data.split(',', 1)
                image_data = base64.b64decode(encoded)
            else:
                import base64
                image_data = base64.b64decode(data)

            # 0600 and O_NOFOLLOW, matching _save_screenshot in server.py: a
            # plain open() wrote the file at umask permissions (0644) and
            # followed a symlink, so with a shared
            # CLAUDE_BROWSER_SCREENSHOTS_DIR someone could pre-create a
            # predictable name as a symlink and have another file of this
            # user's truncated.
            flags = (os.O_CREAT | os.O_WRONLY | os.O_TRUNC |
                     getattr(os, 'O_NOFOLLOW', 0))
            fd = os.open(str(filepath), flags, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(image_data)

            return {'success': True, 'filepath': str(filepath)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    return None


def process_message(message):
    """Process an incoming message from the extension."""
    logger.info(f"Processing message: {message.get('action', 'unknown')}")

    # If this is a response from the extension (has requestId but no action),
    # forward it to the server so execute_tool() can wake up its waiter.
    if 'requestId' in message and 'action' not in message:
        logger.info(f"Forwarding response for requestId: {message.get('requestId')}")

        # Nothing is written to disk here, deliberately. server.py's
        # _save_screenshot already saves every screenshot that comes back,
        # under the filename the caller asked for, with O_NOFOLLOW and mode
        # 0600, and prunes the directory afterwards. This used to write a
        # SECOND copy named screenshot_<epoch>.png through a plain open():
        # umask permissions (0644 rather than 0600), symlinks followed, and
        # the 500-file retention cap consumed at double rate. And because a
        # response here carries no action, any response whose `data` happened
        # to be a data:image/ URL - getValue on an input holding an inline
        # image - landed on disk as a .png.
        forward_response_to_server(message)
        # Nothing goes back to the extension: it posts results without
        # waiting for a reply, and ignores one. Returning the message here
        # sent every result straight back to it, so a 3 MB screenshot broke
        # the 1 MB host-to-extension limit on a trip nobody needed.
        return None

    # Try to handle locally first
    local_result = handle_local_command(message)
    if local_result is not None:
        return local_result

    # Forward to MCP server
    if check_mcp_server():
        return forward_to_mcp_server(message)
    else:
        return {
            'success': False,
            'error': 'MCP server is not available. Please start the server first.',
            'requestId': message.get('requestId')
        }


def forward_response_to_server(response):
    """Forward a response from the extension to the MCP server."""
    import urllib.request
    import urllib.error

    try:
        url = f'{MCP_SERVER_URL}/browser/response'
        data = json.dumps(response).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers=_mcp_headers()
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))

    except Exception as e:
        logger.error(f"Failed to forward response to server: {e}")
        return None


def handle_incoming(message):
    """Process one message from the extension and send any reply it needs."""
    response = process_message(message)
    if response is None:
        return
    # Include request ID for correlation
    if 'requestId' in message:
        response['requestId'] = message['requestId']
    send_message(response)


def deliver_command(command):
    """Send a server command on to the extension, if it fits.

    Firefox drops a host-to-extension message over 1 MB. send_message would
    put a small failure in its place, but that goes to the extension, which
    ignores a reply nobody asked for - so the server, the one waiting, timed
    out with no reason given. Answer the server directly instead.
    """
    size = len(json.dumps(command).encode('utf-8'))
    if size <= MAX_OUTGOING_MESSAGE_BYTES:
        send_message(command)
        return
    logger.error(f"Command {command.get('action')} is {size} bytes, over the "
                 f"{MAX_OUTGOING_MESSAGE_BYTES}-byte native messaging limit; "
                 f"failing it at the server instead of sending it")
    forward_response_to_server({
        'requestId': command.get('requestId'),
        'success': False,
        'error': (f'The {command.get("action")} command was {size} bytes, '
                  f'over Firefox\'s {MAX_OUTGOING_MESSAGE_BYTES}-byte limit '
                  f'for a message to the extension, so it was not sent. '
                  f'Send less in one call.'),
    })


def input_thread():
    """Thread for reading messages from the extension."""
    while True:
        try:
            message = read_message()
        except MessageDecodeError as e:
            logger.error(f"Dropping one unreadable message: {e}")
            continue
        except FramingError as e:
            logger.error(f"Native messaging stream is corrupt ({e}); stopping")
            break
        if message is None:
            logger.info("Extension closed the pipe")
            break

        logger.debug(f"Received message: {describe_message(message)}")
        incoming_queue.put(message)


def output_thread():
    """Thread for sending messages to the extension."""
    while True:
        try:
            message = outgoing_queue.get(timeout=1)
            send_message(message)
        except queue.Empty:
            continue


def poll_for_commands():
    """Poll MCP server for pending commands and forward to extension."""
    import urllib.request
    import urllib.error

    consecutive_failures = 0

    while health_monitor_running:
        try:
            url = f'{MCP_SERVER_URL}/browser/poll'
            req = urllib.request.Request(url, headers=_mcp_headers())

            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                consecutive_failures = 0  # Reset on success

                if data.get('command'):
                    command = data['command']
                    logger.info(f"Got command from server: {command.get('action')}")
                    deliver_command(command)

        except urllib.error.URLError:
            consecutive_failures += 1
            # Server not available - health monitor will handle restart
            if consecutive_failures == 3:
                logger.warning("Poll: MCP server not responding (health monitor will handle)")
        except Exception as e:
            logger.debug(f"Poll error: {e}")

        time.sleep(0.5)  # Poll every 500ms


def health_monitor_thread():
    """Background thread that monitors MCP server health and restarts if needed."""
    global health_monitor_running, restart_attempts

    logger.info("Health monitor started")
    consecutive_failures = 0
    last_healthy = time.time()

    while health_monitor_running:
        try:
            time.sleep(HEALTH_CHECK_INTERVAL)

            if not health_monitor_running:
                break

            # Reap on every pass. Reaping used to happen only inside the next
            # start_mcp_server(), which does not run while the backoff is in
            # effect and never runs again once MAX_RESTART_ATTEMPTS is hit -
            # so the last dead child stayed a zombie for the life of the
            # host. A poll() per tick is cheap and this loop is already here.
            _reap_finished_children()

            # Check server health
            if check_mcp_server():
                if consecutive_failures > 0:
                    logger.info(f"MCP server recovered after {consecutive_failures} failures")
                consecutive_failures = 0
                last_healthy = time.time()
                restart_attempts = 0  # Reset on confirmed health
            else:
                consecutive_failures += 1
                logger.warning(f"Health check failed ({consecutive_failures} consecutive)")

                # Attempt restart after 2 consecutive failures
                if consecutive_failures >= 2:
                    logger.info("Attempting to restart MCP server...")

                    # Clear a stale server off the port. If the port belongs to
                    # something else, leave it and stop trying to restart.
                    if is_port_in_use() and not kill_existing_server():
                        logger.error(
                            f"Port {MCP_SERVER_PORT} is held by another program; "
                            f"not restarting.")
                        continue
                    time.sleep(0.5)

                    if start_mcp_server():
                        logger.info("MCP server restart initiated")
                        consecutive_failures = 0
                    else:
                        logger.error("Failed to restart MCP server")

        except Exception as e:
            logger.error(f"Health monitor error: {e}")

    logger.info("Health monitor stopped")


def shutdown():
    """Clean shutdown of all threads."""
    global health_monitor_running
    logger.info("Shutting down...")
    health_monitor_running = False
    _reap_finished_children()


def main():
    """Main entry point."""
    global health_monitor_running

    logger.info("=" * 60)
    logger.info("ClaudeCodeBrowser Native Host starting...")
    logger.info(f"MCP Server URL: {MCP_SERVER_URL}")
    logger.info(f"Health check interval: {HEALTH_CHECK_INTERVAL}s")
    logger.info("=" * 60)

    # Setup signal handlers for clean shutdown
    def signal_handler(signum, frame):
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Auto-start MCP server if not running
    ensure_mcp_server()

    logger.info(f"MCP Server available: {check_mcp_server()}")

    # Start output thread
    output_handler = threading.Thread(target=output_thread, daemon=True, name="output")
    output_handler.start()

    # Start polling thread for commands from MCP server
    poll_handler = threading.Thread(target=poll_for_commands, daemon=True, name="poll")
    poll_handler.start()

    # Start health monitor thread - this is the key for auto-restart!
    health_handler = threading.Thread(target=health_monitor_thread, daemon=True, name="health")
    health_handler.start()
    logger.info("Health monitor thread started - will auto-restart server on crashes")

    # Process messages in main thread
    while health_monitor_running:
        try:
            message = read_message()
        except MessageDecodeError as e:
            # The frame was read in full, so the stream is still aligned:
            # drop this message and carry on rather than treating a single
            # bad message as a disconnect.
            logger.error(f"Dropping one unreadable message: {e}")
            continue
        except FramingError as e:
            logger.error(f"Native messaging stream is corrupt ({e}); exiting")
            break
        if message is None:
            logger.info("Extension closed the pipe, exiting")
            break

        logger.debug(f"Received: {describe_message(message)}")
        handle_incoming(message)

    shutdown()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting")
        shutdown()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        shutdown()
        sys.exit(1)
