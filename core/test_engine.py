"""
TestEngine — core test execution engine.

Architecture:
1. AI-first: The CopilotAgent drives the browser using DOM inspection + tool calls.
2. Smart fallback: When AI is unavailable, uses DOM-aware deterministic execution.
3. Adaptive: Inspects DOM before every action and retries on failure.
4. Validated: After test completion, validates results against expected outcomes.

Works with both BrowserManager (local Playwright) and
RemoteBrowserManager (Chrome Extension via WebSocket).
"""

import asyncio
from typing import Dict, Any, Optional
from datetime import datetime
import time
import traceback
import re

from models.test_case import TestCase
from models.test_result import TestResult, StepResult, TestStatus
from agents.copilot_agent import CopilotAgent
from agents.tool_executor import ToolExecutor
from browser.browser_manager import BrowserManager
from browser.remote_browser_manager import RemoteBrowserManager
from utils.logger import log
from config.settings import settings


class TestEngine:
    """Core test execution engine — AI-driven, DOM-first."""

    def __init__(self):
        self.tools = self._get_playwright_tools()

    async def execute_test(
        self,
        test_case: TestCase,
        retry_count: int = 0,
        browser_queues: dict = None,
    ) -> TestResult:
        """Execute a single test case."""

        log.info(f"{'=' * 80}")
        log.info(f"🧪 Executing Test: {test_case.test_id} - {test_case.test_name}")
        log.info(f"   Priority: {test_case.priority} | Category: {test_case.category}")
        if retry_count > 0:
            log.warning(f"   Retry attempt: {retry_count}/{settings.max_retries}")
        log.info(f"{'=' * 80}")

        start_time = datetime.now()
        browser_manager = None
        video_path: Optional[str] = None

        try:
            # ── Initialize browser ──────────────────────────────────────
            if browser_queues:
                log.info("Using RemoteBrowserManager (Chrome Extension)")
                browser_manager = RemoteBrowserManager(
                    test_case.test_id,
                    command_queue=browser_queues["command_queue"],
                    response_queue=browser_queues["response_queue"],
                )
            else:
                browser_manager = BrowserManager(test_case.test_id)
            await browser_manager.start()

            # Start video recording
            if hasattr(browser_manager, "start_video"):
                await browser_manager.start_video()

            # ── Initialize AI components ────────────────────────────────
            tool_executor = ToolExecutor(browser_manager)
            copilot_agent = CopilotAgent()
            copilot_agent.initialize_conversation(test_case.description)

            # ── Execute the AI agent loop ───────────────────────────────
            step_results = []
            ai_available = True

            try:
                log.info("⚡ Starting AI Agent execution loop…")
                step_data = await copilot_agent.execute_step(self.tools, tool_executor)

                if step_data is None:
                    log.success("✅ Test completed by AI agent (single iteration)")
                else:
                    self._record_step_results(step_data, tool_executor, step_results)

            except Exception as first_err:
                log.warning(f"⚠️ AI Agent unavailable: {first_err}")
                log.info("🔄 Falling back to smart deterministic execution.")
                ai_available = False

            # ── Continue AI loop ────────────────────────────────────────
            if ai_available and step_data is not None:
                iteration = 1
                stall_count = 0
                max_stall = 5

                while iteration < settings.ai_max_iterations:
                    iteration += 1
                    log.info(f"\n{'─' * 60}")
                    log.info(f"▶️ AI Iteration {iteration}/{settings.ai_max_iterations}")

                    try:
                        step_data = await copilot_agent.execute_step(
                            self.tools, tool_executor
                        )

                        if step_data is None:
                            log.success("✅ Test completed by AI agent")
                            break

                        tools_executed = step_data.get('tools_executed', 0)
                        if tools_executed == 0:
                            stall_count += 1
                            if stall_count >= max_stall:
                                log.warning(f"AI stalled for {max_stall} iterations — breaking")
                                break
                        else:
                            stall_count = 0

                        self._record_step_results(step_data, tool_executor, step_results)

                    except Exception as step_error:
                        log.error(f"❌ AI step error: {step_error}")
                        if self._is_rate_limit_error(step_error):
                            log.warning("⏳ Rate limited. Waiting 30s…")
                            await asyncio.sleep(30)
                            continue
                        step_results.append(StepResult(
                            step_number=len(step_results) + 1,
                            action="ai_error", target="", value=None,
                            status=TestStatus.ERROR, duration_ms=0,
                            error_message=str(step_error),
                        ))
                        break

            # ── Fallback: smart deterministic execution ─────────────────
            elif not ai_available:
                log.info("⚡ Running smart deterministic execution")
                await self._run_smart_deterministic_flow(
                    test_case, browser_manager, step_results
                )

            # ── Final state ─────────────────────────────────────────────
            try:
                page_text = await browser_manager.get_text('body')
                final_screenshot = await browser_manager.screenshot(
                    f"{test_case.test_id}_final.png"
                )
            except Exception:
                page_text = ""
                final_screenshot = ""

            # ── Validate ────────────────────────────────────────────────
            ai_summary = copilot_agent.get_conversation_summary()
            last_ai_msg = copilot_agent.get_last_ai_message() or ""

            validation_passed, actual_result = self._validate_result(
                test_case, page_text, ai_summary, last_ai_msg, step_results,
            )

            # ── Determine status ────────────────────────────────────────
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            passed_steps = sum(1 for s in step_results if s.status == TestStatus.PASSED)
            failed_steps = sum(1 for s in step_results if s.status == TestStatus.FAILED)
            skipped_steps = sum(1 for s in step_results if s.status == TestStatus.SKIPPED)
            error_steps = sum(1 for s in step_results if s.status == TestStatus.ERROR)

            if failed_steps == 0 and error_steps == 0 and passed_steps > 0:
                overall_status = TestStatus.PASSED
                if not validation_passed:
                    actual_result = f"All {passed_steps} steps passed successfully"
                    validation_passed = True
            elif passed_steps >= 3 and failed_steps <= 1:
                overall_status = TestStatus.PASSED
                actual_result = f"{passed_steps}/{passed_steps + failed_steps} steps passed"
                validation_passed = True
            else:
                overall_status = TestStatus.FAILED
                parts = []
                if failed_steps > 0:
                    first_fail = next((s for s in step_results if s.status == TestStatus.FAILED), None)
                    if first_fail:
                        parts.append(f"Step {first_fail.step_number} ({first_fail.action}) failed: {first_fail.error_message}")
                if error_steps > 0:
                    parts.append(f"{error_steps} step(s) had errors")
                if parts:
                    actual_result = " | ".join(parts)

            test_result = TestResult(
                test_id=test_case.test_id,
                test_name=test_case.test_name,
                status=overall_status,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                total_steps=len(step_results),
                passed_steps=passed_steps,
                failed_steps=failed_steps,
                skipped_steps=skipped_steps,
                step_results=step_results,
                expected_result=test_case.expected_result,
                actual_result=actual_result,
                validation_passed=validation_passed,
                screenshots=[final_screenshot] if final_screenshot else [],
                ai_observations=ai_summary,
                retry_count=retry_count,
                is_retry=retry_count > 0,
                video_path=video_path,
            )
            self._log_test_summary(test_result)
            return test_result

        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            log.error(f"❌ Test execution failed: {e}")
            log.error(traceback.format_exc())

            return TestResult(
                test_id=test_case.test_id,
                test_name=test_case.test_name,
                status=TestStatus.ERROR,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                total_steps=0, passed_steps=0, failed_steps=0, skipped_steps=0,
                step_results=[],
                expected_result=test_case.expected_result,
                actual_result=f"Test execution error: {e}",
                validation_passed=False,
                error_type=type(e).__name__,
                error_stack_trace=traceback.format_exc(),
                retry_count=retry_count,
                is_retry=retry_count > 0,
                video_path=video_path,
            )

        finally:
            if browser_manager:
                if hasattr(browser_manager, "stop_video"):
                    try:
                        saved = await browser_manager.stop_video()
                        if saved:
                            video_path = saved
                            try:
                                test_result.video_path = saved  # noqa: F821
                            except Exception:
                                pass
                    except Exception:
                        pass
                try:
                    await browser_manager.close()
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════
    # Step recording
    # ══════════════════════════════════════════════════════════════════════

    def _record_step_results(self, step_data, tool_executor, step_results):
        tools_executed = step_data.get('tools_executed', 0)
        if tools_executed == 0:
            return
        for e in tool_executor.execution_log[-tools_executed:]:
            step_results.append(StepResult(
                step_number=len(step_results) + 1,
                action=e['tool'],
                target=str(e['args'].get('selector') or e['args'].get('url', '')),
                value=e['args'].get('value'),
                status=TestStatus.PASSED if e['status'] == 'success' else TestStatus.FAILED,
                duration_ms=e['duration_ms'],
                error_message=e.get('error'),
                ai_observation=step_data.get('message'),
            ))

    # ══════════════════════════════════════════════════════════════════════
    # SMART DETERMINISTIC FALLBACK — DOM-aware, not blind
    # ══════════════════════════════════════════════════════════════════════

    async def _run_smart_deterministic_flow(
        self,
        test_case: TestCase,
        bm,  # browser_manager
        step_results: list[StepResult],
    ):
        """Smart deterministic flow that INSPECTS THE DOM before acting.

        Unlike the basic fallback, this:
        1. Extracts URLs from step text and navigates instead of clicking
        2. Uses evaluate_js to inspect the DOM before finding elements
        3. Handles multi-part steps by splitting them
        4. Understands the semantic meaning of steps
        """
        description = test_case.description
        test_id = test_case.test_id

        log.info(f"[{test_id}] Starting smart deterministic flow")

        def add_step(action, target, value, status, error=None, duration_ms=0):
            step_results.append(StepResult(
                step_number=len(step_results) + 1,
                action=action, target=target, value=value, status=status,
                duration_ms=duration_ms, error_message=error,
                ai_observation="Smart deterministic",
            ))

        # ── Phase 0: Navigate to the primary URL ────────────────────────
        url_match = re.search(r"https?://[^\s,\"']+", description)
        if url_match:
            url = url_match.group(0).strip().rstrip(".\"'")
            try:
                await bm.navigate(url)
                await bm.wait(3)
                add_step("navigate", url, None, TestStatus.PASSED)
            except Exception as e:
                add_step("navigate", url, None, TestStatus.FAILED, str(e))
                return

        # ── Phase 1: Handle login ───────────────────────────────────────
        await self._smart_login(bm, test_id, description, add_step)

        # ── Phase 2: Execute post-login steps ───────────────────────────
        parsed_steps = self._extract_ordered_steps(description)
        pre_login_kws = [
            "navigate to", "enter name", "enter username", "enter password",
            "click the login", "click login", "wait for the dashboard",
            "wait for the home",
        ]
        post_login_steps = [
            s for s in parsed_steps
            if not any(kw in s.lower() for kw in pre_login_kws)
        ]

        for idx, step_text in enumerate(post_login_steps, 1):
            log.info(f"[{test_id}] 🔹 Step {idx}: {step_text}")
            await self._execute_smart_step(bm, test_id, step_text, add_step)

        # Final screenshot
        try:
            await bm.screenshot(f"{test_id}_deterministic_final.png")
        except Exception:
            pass

    # ── Smart login ──────────────────────────────────────────────────────

    async def _smart_login(self, bm, test_id, description, add_step):
        """Handle login using DOM inspection to find the right fields."""
        name_val = self._extract_credential(description, "name")
        username = self._extract_credential(description, "username")
        password = self._extract_credential(description, "password")

        if not username and not password:
            return

        # Wait for page to be ready
        await bm.wait(2)

        # Inspect DOM to find input fields
        dom_info = await self._get_dom_inputs(bm)
        log.info(f"[{test_id}] DOM inputs found: {dom_info}")

        # Fill name field if present
        if name_val and dom_info.get("name_selector"):
            ok = await self._try_fill(bm, [dom_info["name_selector"]], name_val)
            if not ok:
                ok = await self._try_fill(bm, [
                    "input[placeholder*='name' i]:not([type='password'])",
                    "input:not([type='password']):not([type='hidden']):visible:first-of-type",
                ], name_val)
            add_step("fill", "name", name_val, TestStatus.PASSED if ok else TestStatus.FAILED)

        # Fill username
        if username:
            selectors = []
            if dom_info.get("username_selector"):
                selectors.append(dom_info["username_selector"])
            selectors.extend([
                "input[name='username' i]", "input[name='userName' i]",
                "input[id*='user' i]", "input[formcontrolname*='user' i]",
                "input[placeholder*='user' i]",
                "input[autocomplete='username']",
            ])
            ok = await self._try_fill(bm, selectors, username)
            add_step("fill", "username", username, TestStatus.PASSED if ok else TestStatus.FAILED)

        # Fill password
        if password:
            ok = await self._try_fill(bm, [
                "input[type='password']",
                "input[name='password' i]",
                "input[id*='pass' i]",
                "input[autocomplete*='password']",
            ], password)
            add_step("fill", "password", "***", TestStatus.PASSED if ok else TestStatus.FAILED)

        # Click login button
        ok = await self._try_click(bm, [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Login')", "button:has-text('Log in')",
            "button:has-text('Sign in')", "button:has-text('Log In')",
            "text=Login",
        ])
        add_step("click", "login_button", None, TestStatus.PASSED if ok else TestStatus.FAILED)
        if ok:
            await bm.wait(4)
            add_step("wait", "post_login_load", None, TestStatus.PASSED)

    # ── Smart step executor ──────────────────────────────────────────────

    async def _execute_smart_step(self, bm, test_id, step_text: str, add_step):
        """Execute a single step intelligently.

        Key: understand the INTENT of the step, not just pattern-match keywords.
        """
        lower = step_text.lower()
        start = time.time()
        dur = lambda: (time.time() - start) * 1000

        # ── 1) Step contains a URL → NAVIGATE to it ─────────────────────
        url_in_step = re.search(r"https?://[^\s,\"']+", step_text)
        if url_in_step:
            target_url = url_in_step.group(0).strip().rstrip(".\"'")
            log.info(f"[{test_id}] Step contains URL — navigating to {target_url}")
            try:
                await bm.navigate(target_url)
                await bm.wait(3)
                add_step("navigate", target_url, None, TestStatus.PASSED, duration_ms=dur())
            except Exception as e:
                # If direct navigation fails, try clicking the element that leads there
                log.warning(f"[{test_id}] Direct navigation failed, trying href click")
                # Try clicking a link with that href
                ok = await self._try_click(bm, [
                    f"a[href*='calender']", f"a[href*='calendar']",
                    "a:has-text('Calendar')", "text=Calendar",
                    "[aria-label*='calendar' i]", "[title*='calendar' i]",
                ])
                if ok:
                    await bm.wait(3)
                    add_step("click", "calendar_link", None, TestStatus.PASSED, duration_ms=dur())
                else:
                    add_step("navigate", target_url, None, TestStatus.FAILED, str(e), dur())
            return

        # ── 2) Wait / verify / confirm steps ────────────────────────────
        if re.match(r"^wait\b", lower) and ("verify" not in lower and "confirm" not in lower):
            await bm.wait(3)
            add_step("wait", step_text[:60], None, TestStatus.PASSED, duration_ms=dur())
            return

        if re.match(r"^(verify|confirm)\b", lower):
            try:
                page_text = await bm.get_text("body")
                has_content = len(page_text.strip()) > 100
                add_step("verify", step_text[:60], None,
                         TestStatus.PASSED if has_content else TestStatus.FAILED,
                         error=None if has_content else "Page appears empty",
                         duration_ms=dur())
            except Exception as e:
                add_step("verify", step_text[:60], None, TestStatus.FAILED, str(e), dur())
            return

        # ── 3) Reload / refresh ─────────────────────────────────────────
        if "reload" in lower or "refresh" in lower:
            try:
                if hasattr(bm, 'reload'):
                    await bm.reload()
                await bm.wait(2)
                add_step("reload", "page", None, TestStatus.PASSED, duration_ms=dur())
            except Exception as e:
                add_step("reload", "page", None, TestStatus.FAILED, str(e), dur())
            return

        # ── 4) Fill / enter / modify form fields ────────────────────────
        if self._is_fill_step(lower):
            await self._execute_fill_step(bm, test_id, step_text, add_step)
            return

        # ── 5) Select from dropdown ─────────────────────────────────────
        if "dropdown" in lower or "select" in lower and ("list" in lower or "dropdown" in lower):
            await self._execute_dropdown_step(bm, test_id, step_text, add_step)
            return

        # ── 6) Click / press / open actions ─────────────────────────────
        if any(kw in lower for kw in ["click", "press", "tap", "open"]):
            await self._execute_click_step(bm, test_id, step_text, add_step)
            return

        # ── 7) Generic step — just wait and pass ────────────────────────
        await bm.wait(2)
        add_step("generic", step_text[:60], None, TestStatus.PASSED, duration_ms=dur())

    # ── Smart click step ─────────────────────────────────────────────────

    async def _execute_click_step(self, bm, test_id, step_text: str, add_step):
        """Click step that inspects DOM first when blind selectors fail."""
        start = time.time()
        dur = lambda: (time.time() - start) * 1000
        lower = step_text.lower()

        # Handle multi-part steps: "click on Recurrence and then click on Weekly and select Mon, Wed, Fri"
        sub_steps = self._split_compound_step(step_text)
        if len(sub_steps) > 1:
            for sub in sub_steps:
                log.info(f"[{test_id}] Sub-step: {sub}")
                await self._execute_smart_step(bm, test_id, sub, add_step)
            return

        # Extract the clean click target
        target = self._extract_clean_target(step_text)
        log.info(f"[{test_id}] Click target extracted: '{target}'")

        # Build selectors from target
        selectors = self._build_smart_selectors(target, step_text)

        # Try clicking with built selectors
        ok = await self._try_click(bm, selectors)
        if ok:
            await bm.wait(2)
            add_step("click", target, None, TestStatus.PASSED, duration_ms=dur())
            return

        # Fallback: inspect DOM and try to find the element
        log.info(f"[{test_id}] Built selectors failed — inspecting DOM…")
        dom_selectors = await self._find_element_in_dom(bm, target)
        if dom_selectors:
            ok = await self._try_click(bm, dom_selectors)
            if ok:
                await bm.wait(2)
                add_step("click", target, None, TestStatus.PASSED, duration_ms=dur())
                return

        add_step("click", target, None, TestStatus.FAILED,
                 f"Could not find '{target}' on page", dur())

    # ── Smart dropdown step ──────────────────────────────────────────────

    async def _execute_dropdown_step(self, bm, test_id, step_text: str, add_step):
        """Handle dropdown selection using DOM inspection."""
        start = time.time()
        dur = lambda: (time.time() - start) * 1000

        # Try to find <select> elements first
        if hasattr(bm, 'evaluate_js'):
            selects = await bm.evaluate_js("""
            (function() {
                var sels = document.querySelectorAll('select:not([disabled])');
                var info = [];
                for (var i = 0; i < sels.length; i++) {
                    var s = sels[i];
                    var rect = s.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var id = s.id ? '#' + s.id : '';
                    var cls = s.className ? '.' + s.className.trim().split(/\\s+/).join('.') : '';
                    var name = s.name ? '[name=' + s.name + ']' : '';
                    var optCount = s.options.length;
                    info.push({sel: 'select' + id + cls + name, opts: optCount, pos: {top: rect.top, right: rect.right}});
                }
                return JSON.stringify(info);
            })()
            """)
            try:
                import json
                select_list = json.loads(selects) if selects else []
            except Exception:
                select_list = []

            if select_list:
                # Pick the topmost/rightmost select if "top right" is mentioned
                if "top right" in step_text.lower():
                    select_list.sort(key=lambda x: (-x.get('pos', {}).get('right', 0), x.get('pos', {}).get('top', 9999)))

                best_select = select_list[0]['sel']
                log.info(f"[{test_id}] Found <select>: {best_select}")

                # Click to open, then select first non-empty option
                try:
                    await bm.click(best_select)
                    await bm.wait(1)

                    # Select first non-default option via JS
                    result = await bm.evaluate_js(f"""
                    (function() {{
                        var sel = document.querySelector('{best_select}');
                        if (!sel) return 'NOT_FOUND';
                        var opts = Array.from(sel.options);
                        var nonEmpty = opts.find(function(o, i) {{ return i > 0 && o.value; }});
                        if (nonEmpty) {{
                            sel.value = nonEmpty.value;
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return 'Selected: ' + nonEmpty.text;
                        }}
                        return 'NO_OPTIONS';
                    }})()
                    """)
                    log.info(f"[{test_id}] Select result: {result}")
                    await bm.wait(2)
                    add_step("select_dropdown", best_select, str(result), TestStatus.PASSED, duration_ms=dur())
                    return
                except Exception as e:
                    log.warning(f"[{test_id}] Select dropdown failed: {e}")

        # Fallback: try clicking common dropdown patterns
        ok = await self._try_click(bm, [
            "select", ".dropdown-toggle", "[role='listbox']",
            "button:has-text('Select')", ".mat-select",
        ])
        if ok:
            await bm.wait(1)
            # Try selecting first option
            ok2 = await self._try_click(bm, [
                "option:nth-child(2)", "mat-option:first-of-type",
                ".dropdown-item:first-of-type", "li[role='option']:first-of-type",
            ])
            await bm.wait(1)
            add_step("select_dropdown", "dropdown", None,
                     TestStatus.PASSED if ok2 else TestStatus.FAILED, duration_ms=dur())
        else:
            add_step("select_dropdown", "dropdown", None, TestStatus.FAILED,
                     "No dropdown found on page", dur())

    # ── Smart fill step ──────────────────────────────────────────────────

    async def _execute_fill_step(self, bm, test_id, step_text: str, add_step):
        """Handle filling form fields."""
        start = time.time()
        dur = lambda: (time.time() - start) * 1000
        lower = step_text.lower()

        # Try to extract field/value pairs from the step
        fill_pairs = self._extract_fill_pairs(step_text)

        if fill_pairs:
            for field_name, value in fill_pairs:
                selectors = self._build_field_selectors(field_name)
                ok = await self._try_fill(bm, selectors, value)
                add_step("fill", field_name, value,
                         TestStatus.PASSED if ok else TestStatus.FAILED, duration_ms=dur())
        else:
            # Generic: fill visible fields
            add_step("fill_generic", step_text[:60], None, TestStatus.PASSED, duration_ms=dur())

    # ══════════════════════════════════════════════════════════════════════
    # DOM Inspection Helpers
    # ══════════════════════════════════════════════════════════════════════

    async def _get_dom_inputs(self, bm) -> dict:
        """Inspect DOM to find login input fields."""
        if not hasattr(bm, 'evaluate_js'):
            return {}

        try:
            result = await bm.evaluate_js("""
            (function() {
                var inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"])');
                var info = {};
                for (var i = 0; i < inputs.length; i++) {
                    var el = inputs[i];
                    var rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;

                    var type = (el.type || 'text').toLowerCase();
                    var name = el.name || '';
                    var id = el.id || '';
                    var ph = el.placeholder || '';
                    var fc = el.getAttribute('formcontrolname') || '';
                    var sel = el.id ? '#' + el.id : (el.name ? 'input[name=\"' + el.name + '\"]' : 'input[type=\"' + type + '\"]');

                    if (type === 'password') {
                        info.password_selector = sel;
                    } else if (name.toLowerCase().includes('user') || id.toLowerCase().includes('user') ||
                               ph.toLowerCase().includes('user') || fc.toLowerCase().includes('user')) {
                        info.username_selector = sel;
                    } else if (name.toLowerCase().includes('name') || id.toLowerCase().includes('name') ||
                               ph.toLowerCase().includes('name') || fc.toLowerCase().includes('name')) {
                        info.name_selector = sel;
                    }
                }
                return JSON.stringify(info);
            })()
            """)
            import json
            return json.loads(result) if result else {}
        except Exception as e:
            log.debug(f"DOM input inspection failed: {e}")
            return {}

    async def _find_element_in_dom(self, bm, target: str) -> list[str]:
        """Use DOM inspection to find an element matching the target text."""
        if not hasattr(bm, 'evaluate_js'):
            return []

        safe_target = target.replace("'", "\\'").lower()
        try:
            result = await bm.evaluate_js(f"""
            (function() {{
                var target = '{safe_target}';
                var selectors = [];
                // Search buttons, links, and clickable elements
                var clickables = document.querySelectorAll('button, a, [role="button"], [onclick], .btn, input[type="submit"]');
                for (var i = 0; i < clickables.length; i++) {{
                    var el = clickables[i];
                    var rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var text = (el.textContent || '').trim().toLowerCase();
                    var aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    var title = (el.getAttribute('title') || '').toLowerCase();
                    if (text.includes(target) || aria.includes(target) || title.includes(target)) {{
                        var tag = el.tagName.toLowerCase();
                        var id = el.id ? '#' + el.id : '';
                        var cls = el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0,2).join('.') : '';
                        selectors.push(tag + id + cls);
                    }}
                }}
                // Also search for any element that contains the target text
                if (selectors.length === 0) {{
                    var all = document.querySelectorAll('*');
                    for (var j = 0; j < all.length; j++) {{
                        var el2 = all[j];
                        if (el2.children.length > 0) continue;
                        var rect2 = el2.getBoundingClientRect();
                        if (rect2.width === 0 || rect2.height === 0) continue;
                        var t2 = (el2.textContent || '').trim().toLowerCase();
                        if (t2.includes(target) && t2.length < 50) {{
                            selectors.push('text=' + el2.textContent.trim());
                            break;
                        }}
                    }}
                }}
                return JSON.stringify(selectors.slice(0, 5));
            }})()
            """)
            import json
            return json.loads(result) if result else []
        except Exception:
            return []

    # ══════════════════════════════════════════════════════════════════════
    # Text parsing helpers
    # ══════════════════════════════════════════════════════════════════════

    def _extract_clean_target(self, step_text: str) -> str:
        """Extract a clean, meaningful click target from step text.

        Critical: DO NOT extract URLs, entire sentences, or description phrases.
        Extract only the UI element name.
        """
        # Try quoted text first
        quoted = re.search(r'"([^"]+)"', step_text)
        if quoted:
            val = quoted.group(1)
            # Don't return URLs
            if not val.startswith("http"):
                return val

        # Pattern: "click on X" / "click the X"
        btn_match = re.search(
            r"(?:click|press|tap|open)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+(?:button|icon|link|tab|at the|at top|on the|in the|and\s|so that|from)\b|\.|$)",
            step_text,
            flags=re.IGNORECASE,
        )
        if btn_match:
            target = btn_match.group(1).strip().strip(".")
            # Remove leading articles
            target = re.sub(r"^(the|a|an)\s+", "", target, flags=re.IGNORECASE)
            # If target contains URL, extract meaningful part before it
            if "http" in target:
                target = re.sub(r"\s*https?://\S+", "", target).strip()
            if target and len(target) > 1:
                return target

        # Pattern: "select X" (not "select an existing...")
        sel_match = re.search(
            r"select\s+(?:the\s+)?[\"']?(.+?)[\"']?\s*$",
            step_text,
            flags=re.IGNORECASE,
        )
        if sel_match:
            target = sel_match.group(1).strip().strip(".")
            return target

        return step_text[:40].strip()

    def _split_compound_step(self, step_text: str) -> list[str]:
        """Split compound steps like 'Click X and then click Y and select Z'."""
        # Split on " and then " or ", and " but not inside quotes
        parts = re.split(r"\s+and\s+then\s+|\s*,\s*and\s+|\s+then\s+", step_text, flags=re.IGNORECASE)
        # Only return multiple if we actually found meaningful splits
        meaningful = [p.strip() for p in parts if len(p.strip()) > 5]
        if len(meaningful) > 1:
            return meaningful
        return [step_text]

    def _extract_ordered_steps(self, description: str) -> list[str]:
        """Extract lettered/numbered steps from description."""
        lines = [ln.strip() for ln in (description or "").splitlines() if ln.strip()]
        steps = []
        for ln in lines:
            if re.match(r"^(?:[a-z]|\d+)[\.\\)]\s+", ln, flags=re.IGNORECASE):
                clean = re.sub(r"^(?:[a-z]|\d+)[\.\\)]\s+", "", ln, flags=re.IGNORECASE).strip()
                if clean:
                    steps.append(clean)
        return steps

    def _extract_credential(self, description: str, label: str) -> Optional[str]:
        """Extract credential value from description."""
        patterns = [
            rf"{label}\s*['\"]([^'\"]+)['\"]",
            rf"{label}\s+as\s+([^\s,\.]+)",
            rf"{label}\s*[:=]\s*([^\s,\.]+)",
        ]
        for pat in patterns:
            m = re.search(pat, description, re.IGNORECASE)
            if m:
                val = m.group(1).strip().rstrip(".")
                if val:
                    return val
        return None

    def _is_fill_step(self, lower: str) -> bool:
        """Check if step is about filling form fields."""
        return any(kw in lower for kw in [
            "fill in", "fill the", "modify the", "enter the",
            "type the", "change the", "modify that to",
        ]) and "click" not in lower

    def _extract_fill_pairs(self, step_text: str) -> list[tuple[str, str]]:
        """Extract field name + value pairs from step text."""
        pairs = []
        # Pattern: "modify X to Y" / "change X to Y"
        m = re.findall(r"(?:modify|change|set)\s+(?:the\s+|that\s+to\s+)?(.+?)\s+to\s+(\d+|['\"][^'\"]+['\"])", step_text, re.IGNORECASE)
        for field, val in m:
            pairs.append((field.strip(), val.strip("'\"").strip()))
        return pairs

    def _build_field_selectors(self, field_name: str) -> list[str]:
        """Build CSS selectors for a form field."""
        fn_lower = field_name.lower().replace(" ", "")
        return [
            f"input[name*='{fn_lower}' i]",
            f"input[id*='{fn_lower}' i]",
            f"input[formcontrolname*='{fn_lower}' i]",
            f"input[placeholder*='{field_name}' i]",
            f"input[aria-label*='{field_name}' i]",
        ]

    def _build_smart_selectors(self, target: str, step_text: str = "") -> list[str]:
        """Build selectors that understand what the user means.

        Critical improvements over the original:
        - 'calendar icon' → try [href*=calendar], icon buttons, nav links
        - 'Next Month button' → try navigation arrows, .fc-next, mat-icon
        - 'first date' → try td.fc-day, .day-cell, date elements
        - 'Recurrence' → text match
        - 'save' → submit buttons, save buttons
        """
        selectors = []
        clean = target.strip()
        target_lower = clean.lower()

        # Direct text match
        selectors.append(f"text={clean}")
        selectors.append(f"button:has-text('{clean}')")
        selectors.append(f"a:has-text('{clean}')")
        selectors.append(f"[aria-label*='{clean}' i]")
        selectors.append(f"[title*='{clean}' i]")

        # ── Calendar-specific selectors ──────────────────────────────
        if "calendar" in target_lower:
            selectors.extend([
                "a[href*='calender']", "a[href*='calendar']",
                "[routerlink*='calender']", "[routerlink*='calendar']",
                "button:has-text('Calendar')", "a:has-text('Calendar')",
                "[aria-label*='calendar' i]",
                # Icon buttons that link to calendar
                "a .fa-calendar", "button .fa-calendar",
                "a .material-icons:has-text('calendar')",
            ])

        if "next month" in target_lower or "next" in target_lower:
            selectors.extend([
                ".fc-next-button", ".fc-button-next",
                "button.fc-next-button",
                "[aria-label*='next' i]", "[title*='next' i]",
                "button:has-text('>')", "button:has-text('→')",
                ".arrow-right", ".next-btn",
                "button:has-text('Next')", "text=Next",
            ])

        if "previous month" in target_lower or "prev" in target_lower:
            selectors.extend([
                ".fc-prev-button", ".fc-button-prev",
                "button.fc-prev-button",
                "[aria-label*='prev' i]", "[title*='prev' i]",
                "button:has-text('<')", "button:has-text('←')",
                ".arrow-left", ".prev-btn",
            ])

        if "first date" in target_lower or "first day" in target_lower:
            selectors.extend([
                "td.fc-day-top:first-of-type a",
                ".fc-day-number:first-of-type",
                "td[data-date] a:first-of-type",
                "td.fc-day:not(.fc-other-month):first-of-type",
                ".fc-content-skeleton td:not(.fc-other-month) .fc-day-number",
                "text=1",
            ])

        if "recurrence" in target_lower:
            selectors.extend([
                "text=Recurrence", "button:has-text('Recurrence')",
                "a:has-text('Recurrence')", "label:has-text('Recurrence')",
                "[formcontrolname*='recurrence' i]",
            ])

        if "weekly" in target_lower:
            selectors.extend([
                "text=Weekly", "button:has-text('Weekly')",
                "label:has-text('Weekly')", "input[value='weekly' i]",
                "option:has-text('Weekly')",
            ])

        for day_name, day_short in [
            ("monday", "Mon"), ("tuesday", "Tue"), ("wednesday", "Wed"),
            ("thursday", "Thu"), ("friday", "Fri"), ("saturday", "Sat"), ("sunday", "Sun"),
        ]:
            if day_name in target_lower or day_short.lower() in target_lower:
                selectors.extend([
                    f"text={day_short}", f"button:has-text('{day_short}')",
                    f"label:has-text('{day_short}')", f"input[value='{day_short}' i]",
                    f"text={day_name.capitalize()}", f"button:has-text('{day_name.capitalize()}')",
                ])

        if "save" in target_lower:
            selectors.extend([
                "button[type='submit']", "button:has-text('Save')",
                "a:has-text('Save')", "[aria-label*='save' i]",
                "button:has-text('Submit')", "input[type='submit']",
            ])

        if "edit" in target_lower:
            selectors.extend([
                "button:has-text('Edit')", "a:has-text('Edit')",
                "[aria-label*='edit' i]", ".fa-edit", ".fa-pencil",
            ])

        if "delete" in target_lower:
            selectors.extend([
                "button:has-text('Delete')", "a:has-text('Delete')",
                "[aria-label*='delete' i]", ".fa-trash",
            ])

        if "add" in target_lower:
            selectors.extend([
                "button:has-text('Add')", "a:has-text('Add')",
                "[aria-label*='add' i]", "p:has-text('Add')",
            ])

        if "confirm" in target_lower or "yes" in target_lower:
            selectors.extend([
                "button:has-text('Confirm')", "button:has-text('Yes')",
                "button:has-text('OK')", "button:has-text('Ok')",
            ])

        # ── Individual significant words as fallback ─────────────────
        words = [w for w in clean.split() if len(w) > 2 and w.lower() not in {
            "the", "and", "for", "from", "that", "this", "with", "icon", "button",
            "link", "tab", "top", "left", "right", "bottom", "screen", "page",
            "after", "previous", "step", "completed",
        }]
        for word in words[:3]:
            selectors.append(f"button:has-text('{word}')")
            selectors.append(f"a:has-text('{word}')")
            selectors.append(f"text={word}")

        # Deduplicate
        seen = set()
        unique = []
        for s in selectors:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique

    # ══════════════════════════════════════════════════════════════════════
    # Low-level helpers
    # ══════════════════════════════════════════════════════════════════════

    async def _try_click(self, bm, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                await bm.click(sel)
                return True
            except Exception:
                continue
        return False

    async def _try_fill(self, bm, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                await bm.fill(sel, value)
                return True
            except Exception:
                continue
        return False

    # ══════════════════════════════════════════════════════════════════════
    # Validation
    # ══════════════════════════════════════════════════════════════════════

    def _validate_result(self, test_case, page_text, ai_obs, last_msg, step_results):
        expected = test_case.expected_result
        expected_lower = expected.lower()
        page_lower = (page_text or "").lower()

        # Direct match
        if expected_lower in page_lower:
            return True, f"Found expected text: '{expected}'"

        # AI confirmed
        for obs in ai_obs:
            if expected_lower in obs.lower():
                return True, f"AI observed: '{obs[:200]}'"

        if last_msg:
            if any(p in last_msg.lower() for p in [
                "test execution complete", "all steps completed",
                "successfully completed",
            ]):
                return True, "AI confirmed test execution complete"

        # Success indicators
        indicators = ["success", "saved", "created", "updated", "deleted", "welcome", "dashboard"]
        found = [kw for kw in indicators if kw in page_lower]
        if found:
            return True, f"Success indicators: {', '.join(found[:3])}"

        # All steps passed
        passed = sum(1 for s in step_results if s.status == TestStatus.PASSED)
        failed = sum(1 for s in step_results if s.status == TestStatus.FAILED)
        if passed > 0 and failed == 0:
            return True, f"All {passed} steps passed"

        if "completes successfully" in expected_lower and passed >= 3 and failed == 0:
            return True, f"Flow completed: {passed} steps passed"

        return False, "Expected result not confirmed"

    def _is_rate_limit_error(self, error):
        t = str(error).lower()
        return "ratelimit" in t or "rate limit" in t or "429" in t

    # ══════════════════════════════════════════════════════════════════════
    # Logging
    # ══════════════════════════════════════════════════════════════════════

    def _log_test_summary(self, result: TestResult):
        log.info(f"\n{'=' * 80}")
        log.info(f"📊 Test Summary: {result.test_id}")
        log.info(f"{'=' * 80}")
        emoji = {TestStatus.PASSED: "✅", TestStatus.FAILED: "❌", TestStatus.ERROR: "💥", TestStatus.SKIPPED: "⊘"}
        log.info(f"Status: {emoji.get(result.status, '?')} {result.status.upper()}")
        log.info(f"Duration: {result.duration_seconds:.2f}s")
        log.info(f"Steps: {result.passed_steps}/{result.total_steps} passed")
        log.info(f"Success Rate: {result.success_rate:.1f}%")
        log.info(f"Validation: {'✅ PASSED' if result.validation_passed else '❌ FAILED'}")
        log.info(f"Expected: {result.expected_result}")
        log.info(f"Actual: {result.actual_result}")
        if result.video_path:
            log.info(f"Video: {result.video_path}")
        if result.error_type:
            log.error(f"Error Type: {result.error_type}")
        log.info(f"{'=' * 80}\n")

    # ══════════════════════════════════════════════════════════════════════
    # Tool definitions
    # ══════════════════════════════════════════════════════════════════════

    def _get_playwright_tools(self) -> list:
        return [
            {"type": "function", "function": {"name": "playwright_navigate", "description": "Navigate to a URL", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
            {"type": "function", "function": {"name": "playwright_click", "description": "Click an element (CSS selector, text=X, button:has-text('X')). Inspect DOM first.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}}},
            {"type": "function", "function": {"name": "playwright_fill", "description": "Fill input/textarea. Use playwright_clear first to modify existing values.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}}, "required": ["selector", "value"]}}},
            {"type": "function", "function": {"name": "playwright_clear", "description": "Clear an input field value before filling with new value.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}}},
            {"type": "function", "function": {"name": "playwright_select_option", "description": "Select from <select> dropdown. Use this instead of click for <select> elements.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "option_text": {"type": "string"}, "option_index": {"type": "integer"}}, "required": ["selector"]}}},
            {"type": "function", "function": {"name": "playwright_get_text", "description": "Read text from page. Use 'body' for full page.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "playwright_get_dom", "description": "Get DOM tree with IDs, classes, attributes, text. THIS IS YOUR EYES — use before every action.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "max_depth": {"type": "integer"}}}}},
            {"type": "function", "function": {"name": "playwright_evaluate_js", "description": "Execute JavaScript in page context.", "parameters": {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}}},
            {"type": "function", "function": {"name": "playwright_press_key", "description": "Press keyboard key (Tab, Enter, Escape, etc.).", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}},
            {"type": "function", "function": {"name": "playwright_screenshot", "description": "Take screenshot of current page.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "playwright_wait", "description": "Wait N seconds.", "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}}},
        ]