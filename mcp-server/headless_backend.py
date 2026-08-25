#!/usr/bin/env python3
"""
Headless browser backend for ClaudeCodeBrowser.

Uses Playwright to drive Firefox, Chromium, or WebKit without a display.
Activated when CLAUDE_BROWSER_HEADLESS=1 or --headless is passed.
Pick the engine with CLAUDE_BROWSER_ENGINE=firefox|chromium|webkit (default firefox).

Install: pip install playwright && playwright install firefox   (or chromium/webkit)
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger('ClaudeCodeBrowser.Headless')

SCREENSHOTS_DIR = Path(os.environ.get(
    'CLAUDE_BROWSER_SCREENSHOTS_DIR',
    '/tmp/claudecodebrowser/screenshots'
))
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Firefox vs Chromium vs WebKit: default Firefox to match the visible-mode extension
BROWSER_TYPE = os.environ.get('CLAUDE_BROWSER_ENGINE', 'firefox')

# Optional path to a browser executable. Lets headless mode use a system
# browser or a pre-installed Playwright build at a nonstandard revision,
# instead of requiring "playwright install".
EXECUTABLE_PATH = os.environ.get('CLAUDE_BROWSER_EXECUTABLE')


class HeadlessBrowser:
    """Playwright-backed headless browser. One persistent context per server lifetime."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        # Real tab management: id -> Page, mirroring the extension's tab ids
        self._tabs = {}
        self._next_tab_id = 1
        self._active_tab_id = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install firefox"
            )

        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, BROWSER_TYPE)
        launch_kwargs = {'headless': True}
        if EXECUTABLE_PATH:
            launch_kwargs['executable_path'] = EXECUTABLE_PATH
        self._browser = await launcher.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            viewport={'width': 1280, 'height': 800}
        )
        self._page = await self._context.new_page()
        self._register_tab(self._page)

        # Wire up persistent console + network logging to stderr
        self._page.on('console', lambda m: logger.debug(f"[browser:console:{m.type}] {m.text}"))
        self._page.on('pageerror', lambda e: logger.warning(f"[browser:pageerror] {e}"))
        self._page.on('request', lambda r: logger.debug(f"[browser:request] {r.method} {r.url}"))
        self._page.on('response', lambda r: logger.debug(f"[browser:response] {r.status} {r.url}"))

        logger.info(f"Headless {BROWSER_TYPE} started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Headless browser stopped")

    def is_ready(self) -> bool:
        """True once start() has finished and a page is available for commands."""
        return self._page is not None

    def _register_tab(self, page) -> int:
        """Track a page under a stable tab id; untrack it when it closes."""
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        self._tabs[tab_id] = page
        self._active_tab_id = tab_id
        page.on('close', lambda: self._forget_tab(tab_id))
        return tab_id

    def _forget_tab(self, tab_id: int):
        self._tabs.pop(tab_id, None)
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(iter(self._tabs), None)
            self._page = self._tabs.get(self._active_tab_id)

    async def _get_page(self, tab_id: Optional[int] = None):
        """Return the page for tab_id, or the active page when not specified."""
        if tab_id is not None:
            page = self._tabs.get(tab_id)
            if page is None:
                raise RuntimeError(f"No headless tab with id {tab_id}")
            return page
        if self._page is None:
            raise RuntimeError("Headless browser not started")
        return self._page

    async def _assert_not_password(self, page, selector: str, args: Dict[str, Any]):
        """Refuse to fill password fields unless the safety config allows it."""
        if args.get('allow_password') is True:
            return
        try:
            is_password = await page.eval_on_selector(
                selector,
                "el => el.tagName === 'INPUT' && (el.type === 'password' || "
                "['current-password','new-password'].includes(el.getAttribute('autocomplete')))"
            )
        except Exception:
            return  # selector didn't resolve; the fill will report its own error
        if is_password:
            raise RuntimeError(
                'Refused: target is a password field. Credentials belong in a '
                'password manager, not automated typing. Set '
                '"allow_password_typing": true in ~/.claudecodebrowser/safety.json '
                'to override.'
            )

    async def execute(self, action: str, tab_id: Optional[int], arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                return await self._dispatch(action, tab_id, arguments)
            except Exception as e:
                logger.error(f"Headless {action} failed: {e}")
                return {'success': False, 'error': str(e)}

    async def _dispatch(self, action: str, tab_id, args: Dict[str, Any]) -> Dict[str, Any]:
        page = await self._get_page(tab_id)

        if action == 'navigate':
            url = args.get('url', '')
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'screenshot':
            from datetime import datetime
            filename = Path(args.get('filename') or f'screenshot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png').name
            filepath = SCREENSHOTS_DIR / filename
            await page.screenshot(path=str(filepath), full_page=args.get('full_page', False))
            data = filepath.read_bytes()
            return {
                'success': True,
                'filepath': str(filepath),
                'filename': filename,
                'size': len(data),
                'message': f'Screenshot saved to {filepath}'
            }

        elif action == 'click':
            selector = args.get('selector')
            x, y = args.get('x'), args.get('y')
            if selector:
                await page.click(selector, timeout=10000)
            elif x is not None and y is not None:
                await page.mouse.click(float(x), float(y))
            else:
                return {'success': False, 'error': 'click requires selector or x+y coordinates'}
            return {'success': True}

        elif action == 'type':
            selector = args.get('selector')
            text = args.get('text', '')
            if selector:
                await self._assert_not_password(page, selector, args)
                await page.fill(selector, text)
            else:
                await page.keyboard.type(text)
            return {'success': True}

        elif action == 'scroll':
            x = args.get('x', 0)
            y = args.get('y', 0)
            delta_x = args.get('deltaX', 0)
            delta_y = args.get('deltaY', 300)
            await page.mouse.wheel(float(delta_x), float(delta_y))
            return {'success': True}

        elif action == 'getPageInfo':
            return {
                'success': True,
                'url': page.url,
                'title': await page.title(),
            }

        elif action == 'getElements':
            selector = args.get('selector', 'a, button, input, select, textarea')
            elements = await page.query_selector_all(selector)
            results = []
            for el in elements[:50]:
                try:
                    tag = await el.evaluate('e => e.tagName.toLowerCase()')
                    text = (await el.inner_text())[:100]
                    box = await el.bounding_box()
                    results.append({'tag': tag, 'text': text, 'box': box})
                except Exception:
                    pass
            return {'success': True, 'elements': results}

        elif action == 'executeScript':
            script = args.get('script', '')
            result = await page.evaluate(script)
            return {'success': True, 'result': result}

        elif action == 'waitForElement':
            selector = args.get('selector', '')
            timeout = args.get('timeout', 10000)
            await page.wait_for_selector(selector, timeout=timeout)
            return {'success': True}

        elif action == 'waitForNetworkIdle':
            timeout = args.get('timeout', 10000)
            await page.wait_for_load_state('networkidle', timeout=timeout)
            return {'success': True}

        elif action == 'getTabs':
            tabs = []
            for tid, p in list(self._tabs.items()):
                try:
                    tabs.append({
                        'id': tid,
                        'url': p.url,
                        'title': await p.title(),
                        'active': tid == self._active_tab_id
                    })
                except Exception:
                    pass
            return {'success': True, 'tabs': tabs, 'totalTabs': len(tabs)}

        elif action == 'createTab':
            url = args.get('url', 'about:blank')
            new_page = await self._context.new_page()
            if url != 'about:blank':
                await new_page.goto(url)
            self._page = new_page
            new_id = self._register_tab(new_page)
            return {'success': True, 'tabId': new_id, 'url': new_page.url}

        elif action == 'closeTab':
            if tab_id is None or tab_id not in self._tabs:
                return {'success': False, 'error': f'No headless tab with id {tab_id}'}
            await self._tabs[tab_id].close()
            return {'success': True, 'closedTabId': tab_id}

        elif action == 'focusTab':
            if tab_id is None or tab_id not in self._tabs:
                return {'success': False, 'error': f'No headless tab with id {tab_id}'}
            self._active_tab_id = tab_id
            self._page = self._tabs[tab_id]
            await self._page.bring_to_front()
            return {'success': True, 'tabId': tab_id, 'url': self._page.url}

        elif action == 'getValue':
            selector = args.get('selector', '')
            value = await page.eval_on_selector(selector, 'el => el.value')
            return {'success': True, 'value': value}

        elif action == 'setValue':
            selector = args.get('selector', '')
            value = args.get('value', '')
            await self._assert_not_password(page, selector, args)
            await page.fill(selector, value)
            return {'success': True}

        elif action == 'requestApproval':
            return {'success': False, 'approved': False,
                    'error': 'No human is present in headless mode; use the '
                             'confirm_token flow for protected actions instead.'}

        elif action == 'hover':
            selector = args.get('selector', '')
            await page.hover(selector)
            return {'success': True}

        elif action == 'selectOption':
            selector = args.get('selector', '')
            if args.get('value') is not None:
                await page.select_option(selector, value=args['value'])
            elif args.get('text') is not None:
                await page.select_option(selector, label=args['text'])
            elif args.get('index') is not None:
                await page.select_option(selector, index=int(args['index']))
            else:
                return {'success': False, 'error': 'selectOption requires value, text, or index'}
            return {'success': True}

        elif action == 'goBack':
            await page.go_back(wait_until='domcontentloaded', timeout=15000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'goForward':
            await page.go_forward(wait_until='domcontentloaded', timeout=15000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'pressKey':
            key = args.get('key', '')
            if not key:
                return {'success': False, 'error': 'pressKey requires key'}
            modifiers = [name for flag, name in
                         [('ctrl', 'Control'), ('shift', 'Shift'), ('alt', 'Alt'), ('meta', 'Meta')]
                         if args.get(flag)]
            combo = '+'.join(modifiers + [key])
            selector = args.get('selector')
            if selector:
                await page.focus(selector)
            await page.keyboard.press(combo)
            return {'success': True, 'key': combo}

        elif action == 'getText':
            selector = args.get('selector') or 'body'
            max_length = int(args.get('max_length', 20000))
            text = await page.inner_text(selector, timeout=10000)
            truncated = len(text) > max_length
            return {
                'success': True,
                'text': text[:max_length],
                'truncated': truncated,
                'total_length': len(text),
                'url': page.url
            }

        elif action == 'refresh':
            await page.reload()
            return {'success': True}

        elif action == 'highlight':
            selector = args.get('selector', '')
            await page.eval_on_selector(
                selector,
                "el => { el.style.outline = '3px solid red'; setTimeout(() => el.style.outline = '', 2000); }"
            )
            return {'success': True}

        elif action == 'evalChain':
            steps = args.get('steps', [])
            results = []
            prev = None
            for i, step in enumerate(steps):
                script = step.get('script', '')
                label = step.get('label', f'step_{i}')
                capture = step.get('capture_console', True)
                stop_on_error = step.get('stop_on_error', True)

                console_msgs = []
                if capture:
                    page.on('console', lambda m: console_msgs.append({'type': m.type, 'text': m.text}))

                try:
                    # Inject $prev into execution context
                    wrapped = f"(function($prev) {{ return ({script}); }})({json.dumps(prev)})"
                    result = await page.evaluate(wrapped)
                    prev = result
                    results.append({'label': label, 'result': result, 'console': console_msgs, 'error': None})
                except Exception as e:
                    results.append({'label': label, 'result': None, 'console': console_msgs, 'error': str(e)})
                    if stop_on_error:
                        break
                finally:
                    if capture:
                        page.remove_listener('console', lambda m: None)

            return {'success': True, 'steps': results, 'final': prev}

        elif action == 'waitAndAct':
            condition = args.get('condition', 'true')
            action_script = args.get('action_script', '')
            poll_ms = args.get('poll_interval_ms', 200)
            timeout_ms = args.get('timeout_ms', 15000)
            elapsed = 0
            while elapsed < timeout_ms:
                try:
                    ready = await page.evaluate(condition)
                    if ready:
                        result = await page.evaluate(action_script)
                        return {'success': True, 'result': result, 'elapsed_ms': elapsed}
                except Exception:
                    pass
                await asyncio.sleep(poll_ms / 1000)
                elapsed += poll_ms
            return {'success': False, 'error': f'Condition not met within {timeout_ms}ms'}

        elif action == 'injectObserver':
            selector = args.get('selector', 'body')
            observe_attrs = args.get('observe_attributes', True)
            observe_children = args.get('observe_child_list', True)
            observe_subtree = args.get('observe_subtree', True)
            script = f"""
                (function() {{
                    if (window.__ccb_observer) window.__ccb_observer.disconnect();
                    window.__ccb_mutations = window.__ccb_mutations || [];
                    const target = document.querySelector({json.dumps(selector)}) || document.body;
                    window.__ccb_observer = new MutationObserver(mutations => {{
                        mutations.forEach(m => window.__ccb_mutations.push({{
                            type: m.type,
                            target: m.target.tagName + (m.target.id ? '#' + m.target.id : ''),
                            addedNodes: m.addedNodes.length,
                            removedNodes: m.removedNodes.length,
                            attributeName: m.attributeName,
                            ts: Date.now()
                        }}));
                    }});
                    window.__ccb_observer.observe(target, {{
                        attributes: {'true' if observe_attrs else 'false'},
                        childList: {'true' if observe_children else 'false'},
                        subtree: {'true' if observe_subtree else 'false'}
                    }});
                    return 'observer installed on ' + target.tagName;
                }})()
            """
            result = await page.evaluate(script)
            return {'success': True, 'message': result}

        else:
            return {'success': False, 'error': f'Unsupported headless action: {action}'}


# Module-level singleton
_headless_browser: Optional[HeadlessBrowser] = None


def get_headless_browser() -> Optional[HeadlessBrowser]:
    return _headless_browser


async def init_headless_browser() -> HeadlessBrowser:
    global _headless_browser
    _headless_browser = HeadlessBrowser()
    await _headless_browser.start()
    return _headless_browser
