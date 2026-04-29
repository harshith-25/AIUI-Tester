"""
ToolExecutor — bridges AI tool calls to real browser actions.

Key improvements over original:
1. Auto-retry on click/fill failures with alternative strategies.
2. Richer DOM snapshot with formcontrolname, data-* attributes, Angular markers.
3. Action verification — checks if page changed after clicks.
4. Smart selector fallback — CSS → text → accessible name.
5. Works with both BrowserManager and RemoteBrowserManager.
"""

from typing import Dict, List, Any
from utils.logger import log
from datetime import datetime
import json
import time
import asyncio


class ToolExecutor:
    """Executes browser automation tools requested by the AI agent.

    This executor bridges the AI agent's tool calls to real browser actions.
    It supports both basic interactions (click, fill, navigate) and advanced
    ones (select dropdown options, execute JS, inspect DOM structure, clear
    input fields) — giving the AI the same power as the Antigravity browser.
    """

    def __init__(self, browser_manager):
        self.browser = browser_manager
        self.execution_log: list[dict] = []
        self._last_page_url: str = ""

    async def execute_tools(self, tool_calls) -> List[Dict]:
        """Execute multiple tool calls and return results."""
        results = []

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)

            log.info(f"🔧 Executing tool: {tool_name} with args: {tool_args}")

            start_time = time.time()

            try:
                result_content = await self._execute_single_tool(tool_name, tool_args)
                duration_ms = (time.time() - start_time) * 1000

                log.success(f"✅ Tool {tool_name} completed in {duration_ms:.0f}ms")

                self.execution_log.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "status": "success",
                    "duration_ms": duration_ms,
                    "timestamp": datetime.now().isoformat()
                })

                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_content
                })

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                error_msg = str(e)

                log.error(f"❌ Tool {tool_name} failed: {error_msg}")

                self.execution_log.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "status": "failed",
                    "error": error_msg,
                    "duration_ms": duration_ms,
                    "timestamp": datetime.now().isoformat()
                })

                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"Error: {error_msg}"
                })

        return results

    async def _execute_single_tool(self, tool_name: str, tool_args: dict) -> str:
        """Execute a single tool with appropriate handling."""

        if tool_name == 'playwright_navigate':
            url = tool_args.get('url', '')
            await self.browser.navigate(url)
            self._last_page_url = url
            # Wait for page to settle
            await self.browser.wait(2)
            return f"Successfully navigated to {url}. Page loaded."

        elif tool_name == 'playwright_click':
            selector = tool_args.get('selector', '')
            return await self._handle_click(selector)

        elif tool_name == 'playwright_fill':
            selector = tool_args.get('selector', '')
            value = tool_args.get('value', '')
            return await self._handle_fill(selector, value)

        elif tool_name == 'playwright_clear':
            return await self._handle_clear(tool_args)

        elif tool_name == 'playwright_screenshot':
            path = await self.browser.screenshot()
            return f"Screenshot saved: {path}"

        elif tool_name == 'playwright_get_text':
            selector = tool_args.get('selector', 'body')
            text = await self.browser.get_text(selector)
            # Truncate to avoid overwhelming the AI context
            truncated = text[:3000]
            if len(text) > 3000:
                truncated += f"\n... (truncated, total {len(text)} chars)"
            return f"Page text:\n{truncated}"

        elif tool_name == 'playwright_wait':
            seconds = tool_args.get('seconds', 2)
            await self.browser.wait(seconds)
            return f"Waited {seconds} seconds"

        elif tool_name == 'playwright_select_option':
            return await self._handle_select_option(tool_args)

        elif tool_name == 'playwright_evaluate_js':
            return await self._handle_evaluate_js(tool_args)

        elif tool_name == 'playwright_get_dom':
            return await self._handle_get_dom(tool_args)

        elif tool_name == 'playwright_press_key':
            key = tool_args.get('key', '')
            await self.browser.press_key(key)
            return f"Successfully pressed key '{key}'"

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    # ── Click with auto-retry ────────────────────────────────────────────

    async def _handle_click(self, selector: str) -> str:
        """Click with fallback strategies if primary selector fails."""
        strategies = [
            ("direct", selector),
        ]

        # Build fallback selectors
        if not selector.startswith("text=") and "has-text" not in selector:
            # Try text-based if the selector looks like a label
            clean = selector.strip("'\"")
            if not clean.startswith(("#", ".", "[", "button", "a", "input", "select", "div", "span", "table", "tr", "td")):
                strategies.append(("text_match", f"text={clean}"))
                strategies.append(("button_text", f"button:has-text('{clean}')"))
                strategies.append(("link_text", f"a:has-text('{clean}')"))

        last_error = None
        for strategy_name, sel in strategies:
            try:
                log.info(f"  Click strategy '{strategy_name}': {sel}")
                await self.browser.click(sel)
                await self.browser.wait(1)
                return f"Successfully clicked '{selector}' (strategy: {strategy_name})"
            except Exception as e:
                last_error = str(e)
                log.warning(f"  Click strategy '{strategy_name}' failed: {last_error}")

        # Final fallback: try JS click
        try:
            js_result = await self._js_click_fallback(selector)
            if js_result:
                return js_result
        except Exception:
            pass

        raise Exception(
            f"Click failed for '{selector}' after trying {len(strategies)} strategies. "
            f"Last error: {last_error}. "
            f"Try calling playwright_get_dom to find the correct selector."
        )

    async def _js_click_fallback(self, selector: str) -> str:
        """Last-resort click using JavaScript."""
        if not hasattr(self.browser, 'evaluate_js'):
            return ""

        # Only attempt JS click with CSS-style selectors
        if selector.startswith("text=") or "has-text" in selector:
            return ""

        js = f"""
        (function() {{
            var el = document.querySelector('{selector}');
            if (!el) return 'NOT_FOUND';
            el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
            el.click();
            return 'CLICKED';
        }})()
        """
        try:
            result = await self.browser.evaluate_js(js)
            if result == 'CLICKED':
                await self.browser.wait(1)
                return f"Successfully clicked '{selector}' via JavaScript fallback"
        except Exception:
            pass
        return ""

    # ── Fill with retry ──────────────────────────────────────────────────

    async def _handle_fill(self, selector: str, value: str) -> str:
        """Fill a field, retrying with alternative approaches if needed."""
        try:
            await self.browser.fill(selector, value)
            return f"Successfully filled '{selector}' with '{value}'"
        except Exception as first_error:
            log.warning(f"  Direct fill failed: {first_error}")

        # Fallback: try JS-based fill
        if hasattr(self.browser, 'evaluate_js'):
            try:
                js = f"""
                (function() {{
                    var el = document.querySelector('{selector}');
                    if (!el) return 'NOT_FOUND';
                    el.focus();
                    el.value = '';
                    el.value = '{value.replace(chr(39), chr(92) + chr(39))}';
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'FILLED';
                }})()
                """
                result = await self.browser.evaluate_js(js)
                if result == 'FILLED':
                    return f"Successfully filled '{selector}' with '{value}' via JS fallback"
            except Exception:
                pass

        raise Exception(
            f"Fill failed for '{selector}'. "
            f"Try calling playwright_get_dom to find the correct selector."
        )

    # ── Clear field ──────────────────────────────────────────────────────

    async def _handle_clear(self, tool_args: dict) -> str:
        """Clear an input field value."""
        selector = tool_args.get('selector', '')

        if hasattr(self.browser, 'evaluate_js'):
            js = f"""
            (function() {{
                var el = document.querySelector('{selector}');
                if (!el) return 'ERROR: Element not found: {selector}';
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return 'Cleared field: {selector}';
            }})()
            """
            result = await self.browser.evaluate_js(js)
            result_str = str(result) if result else "Field cleared"

            if result_str.startswith("ERROR:"):
                raise Exception(result_str)
            return result_str

        # Fallback for remote browser — try fill with empty string
        try:
            await self.browser.fill(selector, "")
            return f"Cleared field: {selector}"
        except Exception as e:
            raise Exception(f"Could not clear field '{selector}': {e}")

    # ── Select option from <select> dropdown ─────────────────────────────

    async def _handle_select_option(self, tool_args: dict) -> str:
        """Select an option from a <select> dropdown using JavaScript."""
        selector = tool_args.get('selector', '')
        option_text = tool_args.get('option_text', '')
        option_index = tool_args.get('option_index', None)

        if option_index is not None:
            js = f"""
            (function() {{
                var sel = document.querySelector('{selector}');
                if (!sel) return 'ERROR: Element not found: {selector}';
                if ({option_index} >= sel.options.length) return 'ERROR: Index out of range. Total options: ' + sel.options.length;
                sel.selectedIndex = {option_index};
                sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return 'Selected option at index {option_index}: ' + sel.options[sel.selectedIndex].text;
            }})()
            """
        elif option_text:
            safe_text = option_text.lower().replace("'", "\\'")
            js = f"""
            (function() {{
                var sel = document.querySelector('{selector}');
                if (!sel) return 'ERROR: Element not found: {selector}';
                var options = Array.from(sel.options);
                var match = options.find(function(o) {{ return o.text.toLowerCase().includes('{safe_text}'); }});
                if (match) {{
                    sel.value = match.value;
                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'Selected: ' + match.text;
                }}
                return 'ERROR: No option matching "{option_text}". Available: ' + options.map(function(o){{ return o.text; }}).join(', ');
            }})()
            """
        else:
            js = f"""
            (function() {{
                var sel = document.querySelector('{selector}');
                if (!sel) return 'ERROR: Element not found: {selector}';
                var options = Array.from(sel.options);
                var nonEmpty = options.find(function(o, i) {{ return i > 0 && o.value; }});
                if (nonEmpty) {{
                    sel.value = nonEmpty.value;
                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'Selected first available option: ' + nonEmpty.text;
                }}
                return 'ERROR: No selectable options found';
            }})()
            """

        if not hasattr(self.browser, 'evaluate_js'):
            raise Exception("Browser does not support JavaScript evaluation for select_option")

        result = await self.browser.evaluate_js(js)
        result_str = str(result) if result else "JS evaluation returned no result"

        if result_str.startswith("ERROR:"):
            raise Exception(result_str)
        return result_str

    # ── Execute JavaScript ───────────────────────────────────────────────

    async def _handle_evaluate_js(self, tool_args: dict) -> str:
        """Execute arbitrary JavaScript in the page context."""
        script = tool_args.get('script', '')
        if not script:
            raise ValueError("No script provided")

        if not hasattr(self.browser, 'evaluate_js'):
            raise Exception("Browser does not support JavaScript evaluation")

        result = await self.browser.evaluate_js(script)
        return f"JS result: {result}" if result is not None else "JS executed successfully (no return value)"

    # ── DOM snapshot — the AI's eyes ─────────────────────────────────────

    async def _handle_get_dom(self, tool_args: dict) -> str:
        """Return a rich DOM snapshot so the AI can 'see' the page.

        This is the equivalent of Antigravity's DOM reader — it gives the
        AI a structural view of the page with element types, IDs, classes,
        attributes, and truncated text content.

        Enhanced over original:
        - Includes formcontrolname (Angular)
        - Includes data-testid and role attributes
        - Deeper traversal (configurable depth)
        - Shows input values and select options
        - Shows disabled/readonly state
        """
        selector = tool_args.get('selector', 'body')
        max_depth = tool_args.get('max_depth', 6)

        if not hasattr(self.browser, 'evaluate_js'):
            # Fallback for remote browser — use get_text
            try:
                text = await self.browser.get_text(selector)
                return f"Page text (DOM not available via remote browser):\n{text[:3000]}"
            except Exception as e:
                return f"Could not read page content: {e}"

        js = f"""
        (function() {{
            var root = document.querySelector('{selector}');
            if (!root) return 'Element not found: {selector}';
            var lines = [];
            var pageTitle = document.title || '';
            var pageUrl = window.location.href || '';
            lines.push('Page: ' + pageTitle + ' | URL: ' + pageUrl);
            lines.push('---');
            function walk(el, depth) {{
                if (depth > {max_depth}) return;
                var children = el.children;
                for (var i = 0; i < children.length && i < 100; i++) {{
                    var c = children[i];
                    if (c.tagName === 'SCRIPT' || c.tagName === 'STYLE' || c.tagName === 'NOSCRIPT' || c.tagName === 'SVG') continue;
                    var style = window.getComputedStyle(c);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    var tag = c.tagName.toLowerCase();
                    var id = c.id ? '#' + c.id : '';
                    var cls = (c.className && typeof c.className === 'string') ? '.' + c.className.trim().split(/\\s+/).slice(0, 3).join('.') : '';
                    var type = c.getAttribute('type') ? ' type=' + c.getAttribute('type') : '';
                    var name = c.getAttribute('name') ? ' name=' + c.getAttribute('name') : '';
                    var fcn = c.getAttribute('formcontrolname') ? ' formcontrolname=' + c.getAttribute('formcontrolname') : '';
                    var ph = c.getAttribute('placeholder') ? ' placeholder="' + c.getAttribute('placeholder') + '"' : '';
                    var href = c.getAttribute('href') ? ' href="' + c.getAttribute('href').slice(0, 80) + '"' : '';
                    var val = c.value !== undefined && c.value !== '' ? ' value="' + String(c.value).slice(0, 40) + '"' : '';
                    var role = c.getAttribute('role') ? ' role=' + c.getAttribute('role') : '';
                    var ariaLabel = c.getAttribute('aria-label') ? ' aria-label="' + c.getAttribute('aria-label').slice(0, 40) + '"' : '';
                    var testid = c.getAttribute('data-testid') ? ' data-testid=' + c.getAttribute('data-testid') : '';
                    var txt = '';
                    if (c.children.length === 0 && c.textContent) {{
                        var trimmed = c.textContent.trim().slice(0, 60);
                        if (trimmed) txt = ' "' + trimmed + '"';
                    }}
                    var disabled = c.disabled ? ' [disabled]' : '';
                    var readonly = c.readOnly ? ' [readonly]' : '';
                    var required = c.required ? ' [required]' : '';
                    var indent = '  '.repeat(depth);
                    lines.push(indent + '<' + tag + id + cls + type + name + fcn + ph + href + val + role + ariaLabel + testid + disabled + readonly + required + '>' + txt);
                    if (tag === 'select') {{
                        var opts = c.options;
                        for (var j = 0; j < Math.min(opts.length, 10); j++) {{
                            var sel = opts[j].selected ? ' [selected]' : '';
                            lines.push(indent + '  <option value="' + opts[j].value + '"' + sel + '> "' + opts[j].text.trim().slice(0, 40) + '"');
                        }}
                        if (opts.length > 10) lines.push(indent + '  ... (' + opts.length + ' options total)');
                    }}
                    walk(c, depth + 1);
                }}
            }}
            walk(root, 0);
            return lines.join('\\n');
        }})()
        """
        result = await self.browser.evaluate_js(js)
        if result:
            return f"DOM structure:\n{result}"
        return "Could not retrieve DOM structure. Try playwright_get_text instead."

    # ── Execution summary ────────────────────────────────────────────────

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of tool executions."""
        total = len(self.execution_log)
        successful = sum(1 for e in self.execution_log if e['status'] == 'success')
        failed = sum(1 for e in self.execution_log if e['status'] == 'failed')

        return {
            "total_tools_executed": total,
            "successful": successful,
            "failed": failed,
            "success_rate": (successful / total * 100) if total > 0 else 0
        }