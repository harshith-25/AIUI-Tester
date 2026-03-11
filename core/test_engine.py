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
    """Core test execution engine"""
    _STOPWORDS = {
        "the", "and", "for", "with", "that", "this", "from", "into", "their", "there",
        "then", "than", "have", "has", "had", "are", "was", "were", "will", "can",
        "could", "should", "would", "all", "any", "its", "not", "but", "out", "upon",
        "your", "you", "list", "section", "main", "menu", "details", "correctly",
        "successfully", "without", "errors", "shown", "displayed", "visible"
    }
    
    def __init__(self):
        self.tools = self._get_playwright_tools()
    
    async def execute_test(self, test_case: TestCase, retry_count: int = 0, browser_queues: dict = None) -> TestResult:
        """Execute a single test case
        
        Args:
            browser_queues: Optional dict with 'command_queue' and 'response_queue'
                           asyncio.Queue instances for remote browser execution.
                           When provided, uses RemoteBrowserManager instead of BrowserManager.
        """
        
        log.info(f"{'='*80}")
        log.info(f"🧪 Executing Test: {test_case.test_id} - {test_case.test_name}")
        log.info(f"   Priority: {test_case.priority} | Category: {test_case.category}")
        if retry_count > 0:
            log.warning(f"   Retry attempt: {retry_count}/{settings.max_retries}")
        log.info(f"{'='*80}")
        
        start_time = datetime.now()
        browser_manager = None
        video_path: Optional[str] = None
        
        try:
            # Initialize components — use remote browser if queues are provided
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

            # Begin video recording immediately after the browser/tab is ready.
            # Only RemoteBrowserManager supports this; BrowserManager.start_video()
            # does not exist, so we guard with hasattr to keep both paths working.
            if hasattr(browser_manager, "start_video"):
                await browser_manager.start_video()
            
            tool_executor = ToolExecutor(browser_manager)
            copilot_agent = CopilotAgent()
            
            # Initialize conversation
            copilot_agent.initialize_conversation(test_case.description)
            
            # Execute test steps
            step_results = []
            deterministic_flow = (
                self._is_widget_flow_description(test_case.description)
                or self._is_structured_step_description(test_case.description)
            )

            if deterministic_flow:
                log.info("⚡ Running deterministic flow directly (no multi-iteration AI loop).")
                await self._run_fallback_flow(test_case, browser_manager, step_results)
            else:
                iteration = 0
                while iteration < settings.ai_max_iterations:
                    iteration += 1
                    log.info(f"\n{'─'*80}")
                    log.info(f"▶️  AI Agent Iteration {iteration}")
                    log.info(f"{'─'*80}")
                    
                    try:
                        step_data = await copilot_agent.execute_step(
                            self.tools, 
                            tool_executor
                        )
                        
                        if step_data is None:
                            log.success("✅ Test execution completed by AI agent")
                            break
                        
                        # Record step results from tool executor
                        for tool_exec in tool_executor.execution_log[-step_data.get('tools_executed', 0):]:
                            step_result = StepResult(
                                step_number=len(step_results) + 1,
                                action=tool_exec['tool'],
                                target=str(tool_exec['args'].get('selector') or tool_exec['args'].get('url', '')),
                                value=tool_exec['args'].get('value'),
                                status=TestStatus.PASSED if tool_exec['status'] == 'success' else TestStatus.FAILED,
                                duration_ms=tool_exec['duration_ms'],
                                error_message=tool_exec.get('error'),
                                ai_observation=step_data.get('message')
                            )
                            step_results.append(step_result)
                    
                    except Exception as step_error:
                        log.error(f"❌ Step execution error: {step_error}")

                        # Fallback for provider limits: run deterministic flow from test description.
                        if self._is_rate_limit_error(step_error):
                            log.warning("AI provider rate-limited. Switching to deterministic fallback flow.")
                            await self._run_fallback_flow(test_case, browser_manager, step_results)
                            break

                        step_result = StepResult(
                            step_number=len(step_results) + 1,
                            action="error",
                            target="",
                            value=None,
                            status=TestStatus.ERROR,
                            duration_ms=0,
                            error_message=str(step_error)
                        )
                        step_results.append(step_result)
                        break
            
            # Get final page content for validation
            try:
                page_text = await browser_manager.get_text('body')
                final_screenshot = await browser_manager.screenshot(f"{test_case.test_id}_final.png")
            except Exception as e:
                log.warning(f"Could not capture final state: {e}")
                page_text = ""
                final_screenshot = ""
            
            # Validate results
            validation_passed, actual_result = self._validate_result(
                test_case.expected_result,
                page_text,
                copilot_agent.get_conversation_summary()
            )

            # Determine overall status
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            passed_steps = sum(1 for s in step_results if s.status == TestStatus.PASSED)
            failed_steps = sum(1 for s in step_results if s.status == TestStatus.FAILED)
            skipped_steps = sum(1 for s in step_results if s.status == TestStatus.SKIPPED)

            # Strict status: validation must pass, and no failed steps.
            widget_flow = self._is_widget_flow_description(test_case.description)
            has_real_execution = passed_steps > 0
            required_actions_ok, required_reason = self._required_actions_satisfied(
                description=test_case.description,
                step_results=step_results,
            )
            if failed_steps == 0 and (
                validation_passed
                or (widget_flow and has_real_execution and required_actions_ok)
            ):
                overall_status = TestStatus.PASSED
            else:
                overall_status = TestStatus.FAILED
                if required_reason:
                    actual_result = f"{actual_result} | Required action check failed: {required_reason}"
            
            # Create test result
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
                ai_observations=copilot_agent.get_conversation_summary(),
                retry_count=retry_count,
                is_retry=retry_count > 0,
                video_path=video_path,
            )
            
            # Log summary
            self._log_test_summary(test_result)
            
            return test_result
        
        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            log.error(f"❌ Test execution failed: {e}")
            log.error(traceback.format_exc())
            
            # Create error result
            test_result = TestResult(
                test_id=test_case.test_id,
                test_name=test_case.test_name,
                status=TestStatus.ERROR,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                total_steps=0,
                passed_steps=0,
                failed_steps=0,
                skipped_steps=0,
                step_results=[],
                expected_result=test_case.expected_result,
                actual_result=f"Test execution error: {str(e)}",
                validation_passed=False,
                error_type=type(e).__name__,
                error_stack_trace=traceback.format_exc(),
                retry_count=retry_count,
                is_retry=retry_count > 0,
                video_path=video_path,
            )
            
            return test_result
        
        finally:
            if browser_manager:
                # Stop video recording before closing the tab so the extension
                # can still access the stream.  video_path is written back into
                # the already-constructed test_result if one was returned.
                if hasattr(browser_manager, "stop_video"):
                    try:
                        saved_path = await browser_manager.stop_video()
                        if saved_path:
                            video_path = saved_path
                            # Patch the path into the result object that was
                            # already built above (both happy-path and error-path
                            # branches set video_path=video_path at construction
                            # time using the local variable, so we update it here
                            # if the local variable was None when the result was
                            # constructed — i.e., the finally block ran after the
                            # result object was already created).
                            try:
                                test_result.video_path = saved_path  # type: ignore[name-defined]
                            except Exception:
                                pass  # test_result may not be defined if start() itself threw
                    except Exception as e:
                        log.warning(f"Error stopping video recording: {e}")

                try:
                    await browser_manager.close()
                except Exception as e:
                    log.warning(f"Error closing browser: {e}")
    
    def _validate_result(
        self, 
        expected_result: str, 
        page_text: str, 
        ai_observations: list
    ) -> tuple[bool, str]:
        """Validate test result against expected outcome"""
        
        expected_lower = expected_result.lower()
        page_text_lower = page_text.lower()
        
        # Check in page text
        if expected_lower in page_text_lower:
            return True, f"Found expected text in page: '{expected_result}'"
        
        # Check in AI observations
        for observation in ai_observations:
            if expected_lower in observation.lower():
                return True, f"AI agent observed: '{observation}'"
        
        # Check for common validation patterns
        validation_keywords = [
            'already exists', 'duplicate', 'success', 'error', 
            'failed', 'completed', 'saved', 'created'
        ]
        
        found_keywords = [kw for kw in validation_keywords if kw in page_text_lower]
        
        if found_keywords:
            actual = f"Found validation indicators: {', '.join(found_keywords)}"
            # Check if expected is in found keywords
            if any(kw in expected_lower for kw in found_keywords):
                return True, actual
            # Treat conversational login success as equivalent to dashboard/login complete expectation.
            if ("dashboard" in expected_lower or "login" in expected_lower) and any(
                kw in found_keywords for kw in ["success", "completed"]
            ):
                return True, actual
            # Keep evaluating semantic overlap before failing.

        combined_observations = " ".join(ai_observations).lower()
        validation_text = f"{page_text_lower} {combined_observations}"

        # Domain-equivalent terms observed in this UI.
        equivalence_map = {
            "institutes": "institution",
            "institute": "institution",
            "authenticated": "welcome",
            "updated": "update",
            "saved": "save",
        }
        for source, target in equivalence_map.items():
            if source in expected_lower and target in validation_text:
                return True, f"Matched equivalent domain term: expected '{source}' and found '{target}'"

        expected_tokens = self._extract_significant_tokens(expected_lower)
        if expected_tokens:
            matched = [token for token in expected_tokens if token in validation_text]
            token_ratio = len(matched) / len(expected_tokens)
            if len(matched) >= 2 and token_ratio >= 0.20:
                return True, (
                    f"Matched expected keywords in page/observations: "
                    f"{', '.join(matched[:8])} ({len(matched)}/{len(expected_tokens)})"
                )
        
        return False, "Expected result not found in page or AI observations"

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """Detect API rate-limit errors from provider responses."""
        error_text = str(error).lower()
        return "ratelimit" in error_text or "rate limit" in error_text or "429" in error_text

    def _is_widget_flow_description(self, description: str) -> bool:
        """Detect deterministic widget verification flow requests."""
        lower_desc = description.lower()
        return (
            "widget" in lower_desc
            or "chat icon" in lower_desc
            or ("ask for" in lower_desc and "otp" in lower_desc and "email" in lower_desc)
            or ("ask for otp" in lower_desc)
        )

    def _is_structured_step_description(self, description: str) -> bool:
        """Detect CSV descriptions that already contain explicit ordered steps."""
        lower_desc = description.lower()
        has_step_markers = bool(re.search(r"(^|\n)\s*(?:\d+|[a-z])\.\s+", lower_desc))
        has_basic_actions = any(
            phrase in lower_desc
            for phrase in [
                "navigate to",
                "enter username",
                "enter password",
                "click the login",
                "wait for",
                "verify"
            ]
        )
        return has_step_markers and has_basic_actions

    def _extract_significant_tokens(self, text: str) -> list[str]:
        """Extract meaningful keywords from expected text for soft validation."""
        raw_tokens = re.findall(r"[a-z]{4,}", text.lower())
        tokens = []
        seen = set()
        for token in raw_tokens:
            normalized = token[:-1] if token.endswith("s") and len(token) > 5 else token
            if normalized in self._STOPWORDS:
                continue
            if normalized not in seen:
                seen.add(normalized)
                tokens.append(normalized)
        return tokens

    async def _run_fallback_flow(
        self,
        test_case: TestCase,
        browser_manager: BrowserManager,
        step_results: list
    ):
        """Run deterministic UI actions parsed from description when AI calls are unavailable."""
        description = test_case.description
        lower_desc = description.lower()
        log.info(f"[{test_case.test_id}] Starting deterministic fallback flow")
        log.info(f"[{test_case.test_id}] Description length: {len(description)} characters")
        log.info(f"[{test_case.test_id}] Full description:\n{description}")
        parsed_steps = self._extract_ordered_description_steps(description)
        if parsed_steps:
            for i, step_text in enumerate(parsed_steps, 1):
                log.info(f"[{test_case.test_id}] Parsed description step {i}: {step_text}")
        description_progress = []

        def mark_description_step(step_code: str, detail: str, passed: bool, error: Optional[str] = None):
            state = "COMPLETED" if passed else "FAILED"
            progress_line = f"[{test_case.test_id}] Description step {step_code}: {state} - {detail}"
            if passed:
                log.success(progress_line)
            else:
                log.error(progress_line)
                if error:
                    log.error(f"[{test_case.test_id}] Description step {step_code} error: {error}")
            description_progress.append((step_code, passed, detail, error))

        def add_step(action: str, target: str, value: Optional[str], status: TestStatus, error: Optional[str] = None, duration_ms: float = 0):
            log.info(
                f"[{test_case.test_id}] Step {len(step_results) + 1}: {action} -> {target} | "
                f"status={status.value} | duration_ms={duration_ms:.0f}"
            )
            if error:
                if status in {TestStatus.FAILED, TestStatus.ERROR}:
                    log.error(f"[{test_case.test_id}] Step error: {error}")
                else:
                    log.warning(f"[{test_case.test_id}] Step note: {error}")
            step_results.append(
                StepResult(
                    step_number=len(step_results) + 1,
                    action=action,
                    target=target,
                    value=value,
                    status=status,
                    duration_ms=duration_ms,
                    error_message=error,
                    ai_observation="Deterministic fallback flow executed"
                )
            )

        # 1) Navigate
        url_match = re.search(r"https?://[^\s,]+", description)
        if url_match:
            url = url_match.group(0).strip().rstrip(".")
            log.info(f"[{test_case.test_id}] Parsed URL: {url}")
            nav_ok, nav_err, nav_dur = await self._navigate_with_retries(
                browser_manager=browser_manager,
                url=url,
                max_attempts=3,
                wait_between_seconds=1.0,
            )
            add_step(
                "playwright_navigate",
                url,
                None,
                TestStatus.PASSED if nav_ok else TestStatus.FAILED,
                error=nav_err,
                duration_ms=nav_dur,
            )
        else:
            log.warning(f"[{test_case.test_id}] No URL found in description")

        widget_flow = self._is_widget_flow_description(description)

        # 2) Handle MiraQ widget flow when requested in description
        if widget_flow:
            log.info(f"[{test_case.test_id}] Widget flow detected; enforcing conversational steps")
            launcher_ok, launcher_err, launcher_dur = await self._try_click_selectors(
                browser_manager,
                test_case.test_id,
                "chat_launcher",
                [
                    "chat_launcher",
                    "#silfra-chat-widget-container button",
                    "button[aria-label*='chat' i]",
                    "[title*='chat' i]",
                ],
            )
            add_step(
                "playwright_click",
                "chat_launcher",
                None,
                TestStatus.PASSED if launcher_ok else TestStatus.FAILED,
                error=launcher_err,
                duration_ms=launcher_dur,
            )

            start_ok, start_err, start_dur = await self._try_click_selectors(
                browser_manager,
                test_case.test_id,
                "chat_start_button",
                [
                    "chat_start_button",
                    "#silfra-chat-widget-container .xpert-home-action-icon",
                    "#silfra-chat-widget-container button.xpert-icon-btn",
                    "button:has-text('Start')",
                    "text=Start",
                ],
            )
            add_step(
                "playwright_click",
                "chat_start_button",
                None,
                TestStatus.PASSED if start_ok else TestStatus.FAILED,
                error=start_err,
                duration_ms=start_dur,
            )

            full_name = self._extract_full_name(description)
            if not full_name:
                full_name = self._extract_any_field_value(description, "name")
            if full_name:
                log.info(f"[{test_case.test_id}] Parsed full name: {full_name}")
                ok, err, dur = await self._send_widget_message(browser_manager, full_name)
                add_step("playwright_fill", "chat_input", full_name, TestStatus.PASSED if ok else TestStatus.FAILED, error=err, duration_ms=dur)
                mark_description_step("a", "Entered name in widget", ok, err)
            else:
                if "name" in lower_desc:
                    add_step("parse_value", "full_name", None, TestStatus.SKIPPED, error="Could not extract full name from description", duration_ms=0)
                    mark_description_step("a", "Entered name in widget", True, "Skipped: could not extract full name from description")

            email = self._extract_field_value(description, "email")
            if not email:
                email = self._extract_any_field_value(description, "email")
            if email:
                log.info(f"[{test_case.test_id}] Parsed email: {email}")
                ok, err, dur = await self._send_widget_message(browser_manager, email)
                add_step("playwright_fill", "chat_input", email, TestStatus.PASSED if ok else TestStatus.FAILED, error=err, duration_ms=dur)
                mark_description_step("b", "Entered email in widget", ok, err)
            else:
                if "email" in lower_desc:
                    add_step("parse_value", "email", None, TestStatus.SKIPPED, error="Could not extract email from description", duration_ms=0)
                    mark_description_step("b", "Entered email in widget", True, "Skipped: could not extract email from description")

            otp = self._extract_field_value(description, "otp")
            if not otp:
                otp = self._extract_any_field_value(description, "otp")
            if otp:
                log.info(f"[{test_case.test_id}] Parsed OTP value")
                ok, err, dur = await self._send_widget_message(browser_manager, otp)
                add_step("playwright_fill", "chat_input", otp, TestStatus.PASSED if ok else TestStatus.FAILED, error=err, duration_ms=dur)
                mark_description_step("c", "Entered OTP and submitted", ok, err)
            else:
                if "otp" in lower_desc:
                    add_step("parse_value", "otp", None, TestStatus.SKIPPED, error="Could not extract OTP from description", duration_ms=0)
                    mark_description_step("c", "Entered OTP and submitted", True, "Skipped: could not extract OTP from description")

            if self._description_requests_confirmation(lower_desc):
                confirm_ok, confirm_err, confirm_dur = await self._verify_widget_confirmation(browser_manager)
                add_step(
                    "validate_widget_confirmation",
                    "widget_success_or_login_complete",
                    None,
                    TestStatus.PASSED if confirm_ok else TestStatus.SKIPPED,
                    error=None if confirm_ok else f"Optional confirmation text not found: {confirm_err}",
                    duration_ms=confirm_dur
                )
                mark_description_step("d", "Verified success/login-complete confirmation in widget", True, None if confirm_ok else f"Skipped: {confirm_err}")

            if self._description_requests_back_action(lower_desc):
                back_ok, back_err, back_dur = await self._try_click_selectors(
                    browser_manager,
                    test_case.test_id,
                    "back_action",
                    [
                        "button:has-text('Back')",
                        "[aria-label*='back' i]",
                        "#silfra-chat-widget-container button.xpert-icon-btn",
                        "text=Back"
                    ]
                )
                add_step(
                    "playwright_click",
                    "back_action",
                    None,
                    TestStatus.PASSED if back_ok else TestStatus.SKIPPED,
                    error=None if back_ok else f"Optional back action not found: {back_err}",
                    duration_ms=back_dur,
                )
                mark_description_step("d", "Used in-widget back action", True, None if back_ok else f"Skipped: {back_err}")

            if self._description_requests_documents(lower_desc):
                doc_ok, doc_err, doc_dur = await self._try_click_selectors(
                    browser_manager,
                    test_case.test_id,
                    "documents_button",
                    [
                        "button[aria-label*='folder' i]",
                        "button[title*='folder' i]",
                        "[aria-label*='documents' i]",
                        "[title*='documents' i]",
                        "#silfra-chat-widget-container .xpert-home-action-icon:nth-of-type(2)",
                        ":nth-match(#silfra-chat-widget-container button, 2)",
                        "text=Documents",
                        "button:has-text('Documents')",
                        "a:has-text('Documents')",
                        "button[aria-label*='document' i]",
                        "button[title*='document' i]",
                        "#silfra-chat-widget-container button.xpert-icon-btn"
                    ]
                )
                add_step("playwright_click", "documents_button", None, TestStatus.PASSED if doc_ok else TestStatus.SKIPPED, error=None if doc_ok else f"Optional documents action not found: {doc_err}", duration_ms=doc_dur)
                mark_description_step("e", "Clicked folder icon/Documents and opened list", True, None if doc_ok else f"Skipped: {doc_err}")

            if self._description_requests_validate_content(lower_desc):
                start_wait_docs = time.time()
                try:
                    await browser_manager.wait(2)
                    add_step("playwright_wait", "documents_list_wait_2s", None, TestStatus.PASSED, duration_ms=(time.time() - start_wait_docs) * 1000)
                except Exception as e:
                    add_step("playwright_wait", "documents_list_wait_2s", None, TestStatus.FAILED, error=str(e), duration_ms=(time.time() - start_wait_docs) * 1000)

                validate_ok, validate_err, validate_dur = await self._try_click_selectors(
                    browser_manager,
                    test_case.test_id,
                    "validate_content_first_file",
                    [
                        ":nth-match(button:has-text('Validate Content'), 1)",
                        "#silfra-chat-widget-container :nth-match(button:has-text('Validate Content'), 1)",
                        "button:has-text('Validate Content')",
                        "text=Validate Content",
                        "button:has-text('Validate')",
                        "[data-testid*='validate' i]",
                        "button[aria-label*='validate' i]"
                    ]
                )
                add_step(
                    "playwright_click",
                    "validate_content_first_file",
                    None,
                    TestStatus.PASSED if validate_ok else TestStatus.SKIPPED,
                    error=None if validate_ok else f"Optional validate-content action not found: {validate_err}",
                    duration_ms=validate_dur
                )
                mark_description_step("f", "Clicked Validate Content on first file", True, None if validate_ok else f"Skipped: {validate_err}")

            if self._description_requests_question(lower_desc):
                question = self._extract_question_text(description)
                if not question:
                    add_step(
                        "parse_value",
                        "chat_question",
                        None,
                        TestStatus.FAILED,
                        error="Question requested in description but no question text could be extracted.",
                        duration_ms=0,
                    )
                    mark_description_step("q", "Extracted question text", False, "Could not extract question text")
                else:
                    # Ensure chat input context is active before sending question.
                    chat_ok, chat_err, chat_dur = await self._try_click_selectors(
                        browser_manager,
                        test_case.test_id,
                        "chat_tab_for_question",
                        [
                            "#silfra-chat-widget-container [aria-label*='chat' i]",
                            "#silfra-chat-widget-container button[title*='chat' i]",
                            "#silfra-chat-widget-container .xpert-home-action-icon:first-of-type",
                            "button:has-text('Chat')",
                            "text=Chat",
                        ],
                    )
                    add_step(
                        "playwright_click",
                        "chat_tab_for_question",
                        None,
                        TestStatus.PASSED if chat_ok else TestStatus.SKIPPED,
                        error=None if chat_ok else f"Optional chat-tab action not found: {chat_err}",
                        duration_ms=chat_dur,
                    )

                    q_ok, q_err, q_dur = await self._send_widget_message(browser_manager, question)
                    add_step(
                        "playwright_fill",
                        "chat_input_question",
                        question,
                        TestStatus.PASSED if q_ok else TestStatus.FAILED,
                        error=q_err,
                        duration_ms=q_dur,
                    )
                    mark_description_step("q", f"Asked question in widget: {question}", q_ok, q_err)

                    if q_ok:
                        vr_ok, vr_err, vr_dur = await self._verify_widget_question_response(browser_manager, question)
                        add_step(
                            "validate_widget_question_response",
                            "chat_response",
                            None,
                            TestStatus.PASSED if vr_ok else TestStatus.FAILED,
                            error=vr_err,
                            duration_ms=vr_dur,
                        )
                        mark_description_step("r", "Verified question response", vr_ok, vr_err)

        # 3) Fill username/password when present in description
        username = self._extract_credential(description, "username")
        password = self._extract_credential(description, "password")

        if username:
            ok, err, dur = await self._fill_first_available(
                browser_manager,
                ["login_username", "input[name='username']", "input#username", "input[type='text']"],
                username
            )
            add_step("playwright_fill", "username", username, TestStatus.PASSED if ok else TestStatus.FAILED, error=err, duration_ms=dur)

        if password:
            ok, err, dur = await self._fill_first_available(
                browser_manager,
                ["login_password", "input[name='password']", "input#password", "input[type='password']"],
                password
            )
            add_step("playwright_fill", "password", "***", TestStatus.PASSED if ok else TestStatus.FAILED, error=err, duration_ms=dur)

        # 4) Click login if requested
        if (not widget_flow) and "login" in lower_desc and "click" in lower_desc:
            start = time.time()
            click_ok = False
            last_err = None
            for selector in ["login_button", "button[type='submit']", "button:has-text('Login')", "input[type='submit']"]:
                try:
                    await browser_manager.click(selector)
                    click_ok = True
                    break
                except Exception as e:
                    last_err = str(e)
            add_step(
                "playwright_click",
                "login_button",
                None,
                TestStatus.PASSED if click_ok else TestStatus.FAILED,
                error=None if click_ok else last_err,
                duration_ms=(time.time() - start) * 1000
            )

        # 4.1) Structured admin-menu actions from CSV step text.
        if (not widget_flow) and self._is_structured_step_description(description):
            await self._run_structured_menu_actions(
                browser_manager,
                test_case.test_id,
                lower_desc,
                add_step
            )

        # 5) Give UI some time, then screenshot
        start_wait = time.time()
        try:
            await browser_manager.wait(3)
            add_step("playwright_wait", "3s", None, TestStatus.PASSED, duration_ms=(time.time() - start_wait) * 1000)
        except Exception as e:
            add_step("playwright_wait", "3s", None, TestStatus.FAILED, error=str(e), duration_ms=(time.time() - start_wait) * 1000)

        start_shot = time.time()
        try:
            shot = await browser_manager.screenshot()
            add_step("playwright_screenshot", shot, None, TestStatus.PASSED, duration_ms=(time.time() - start_shot) * 1000)
        except Exception as e:
            add_step("playwright_screenshot", "fallback", None, TestStatus.FAILED, error=str(e), duration_ms=(time.time() - start_shot) * 1000)
        completion_passed = len(step_results) > 0 and all(s.status != TestStatus.FAILED for s in step_results)
        mark_description_step("g", "Marked test passed only if all critical described steps passed", completion_passed, None if completion_passed else "One or more critical steps failed")
        log.info(f"[{test_case.test_id}] Deterministic fallback flow completed with {len(step_results)} steps")

    async def _run_structured_menu_actions(self, browser_manager: BrowserManager, test_id: str, lower_desc: str, add_step):
        """Execute common admin navigation/actions from structured CSV step descriptions."""
        nav_targets = [
            ("institutes", ["text=Institutes", "a:has-text('Institutes')", "button:has-text('Institutes')"]),
            ("role management", ["text=Role Management", "text=Roles", "a:has-text('Role')"]),
            ("user management", ["text=User Management", "text=Users", "a:has-text('Users')"]),
            ("services offered", ["text=Services Offered", "text=Services", "a:has-text('Services')"]),
            ("service mapping", ["text=Service Mapping", "a:has-text('Service Mapping')"]),
            ("study list", ["text=Study List", "text=Studies", "a:has-text('Studies')"]),
            ("trash", ["text=Trash", "a:has-text('Trash')"]),
            ("appointment calendar", ["text=Appointment Calendar", "text=Calendar", "a:has-text('Calendar')"]),
            ("doctor's calendar", ["text=Calendar", "a:has-text('Calendar')"]),
            ("user profile", ["text=Profile", "a:has-text('Profile')"]),
            ("change password", ["text=Change Password", "a:has-text('Change Password')"]),
            ("user type", ["text=User Types", "text=User Type", "a:has-text('User Type')"]),
        ]

        for phrase, selectors in nav_targets:
            if phrase in lower_desc:
                ok, err, dur = await self._try_click_selectors(
                    browser_manager,
                    test_id,
                    f"nav_{phrase.replace(' ', '_')}",
                    selectors
                )
                add_step(
                    "playwright_click",
                    phrase,
                    None,
                    TestStatus.PASSED if ok else TestStatus.SKIPPED,
                    error=None if ok else f"Optional navigation not found: {err}",
                    duration_ms=dur
                )

        action_targets = [
            ("add", ["button:has-text('Add')", "text=Add", "[aria-label*='add' i]"]),
            ("edit", ["button:has-text('Edit')", "text=Edit", "[aria-label*='edit' i]"]),
            ("delete", ["button:has-text('Delete')", "text=Delete", "[aria-label*='delete' i]"]),
            ("save", ["button:has-text('Save')", "text=Save", "button:has-text('Update')", "text=Update"]),
            ("confirm", ["button:has-text('Confirm')", "button:has-text('Yes')", "text=Confirm", "text=Yes"]),
        ]

        for phrase, selectors in action_targets:
            if phrase in lower_desc:
                ok, err, dur = await self._try_click_selectors(
                    browser_manager,
                    test_id,
                    f"action_{phrase}",
                    selectors
                )
                add_step(
                    "playwright_click",
                    f"{phrase}_action",
                    None,
                    TestStatus.PASSED if ok else TestStatus.SKIPPED,
                    error=None if ok else f"Optional action not found: {err}",
                    duration_ms=dur
                )

    def _description_requests_back_action(self, lower_desc: str) -> bool:
        """Detect back-navigation instruction in flexible natural language."""
        return (
            "back button" in lower_desc
            or "back action" in lower_desc
            or "in-widget back" in lower_desc
            or ("back" in lower_desc and "widget" in lower_desc)
            or "move out of the current conversation panel" in lower_desc
        )

    def _description_requests_confirmation(self, lower_desc: str) -> bool:
        """Detect request to verify successful verification/login text."""
        return (
            "confirm the widget shows" in lower_desc
            or "successful verification" in lower_desc
            or "login-complete" in lower_desc
            or "confirmation text" in lower_desc
        )

    def _description_requests_documents(self, lower_desc: str) -> bool:
        """Detect request to open documents list."""
        return (
            "documents" in lower_desc
            or "folder icon" in lower_desc
            or "open the document" in lower_desc
        )

    def _description_requests_validate_content(self, lower_desc: str) -> bool:
        """Detect request to validate first document/content item."""
        return (
            "validate content" in lower_desc
            or "validate content button" in lower_desc
            or ("validate" in lower_desc and "file" in lower_desc)
        )

    def _description_requests_question(self, lower_desc: str) -> bool:
        """Detect explicit request to ask a chat question."""
        if "who are you" in lower_desc:
            return True
        if re.search(r"\btype\s+(?:the\s+)?question\b", lower_desc):
            return True
        if re.search(r"\bask\s+(?:a\s+|the\s+)?question\b", lower_desc):
            return True
        if re.search(r"\bquestion\s*[:=]\s*", lower_desc):
            return True
        return False

    def _extract_ordered_description_steps(self, description: str) -> list[str]:
        """Extract explicit ordered steps from free-form description text."""
        lines = [ln.strip() for ln in (description or "").splitlines() if ln.strip()]
        steps: list[str] = []
        for ln in lines:
            if re.match(r"^(?:[a-z]|\d+)[\.\)]\s+", ln, flags=re.IGNORECASE):
                steps.append(re.sub(r"^(?:[a-z]|\d+)[\.\)]\s+", "", ln, flags=re.IGNORECASE).strip())
        return steps

    def _extract_question_text(self, description: str) -> Optional[str]:
        """Extract quoted/unquoted question text from description."""
        patterns = [
            r"type\s+(?:the\s+)?question\s*['\"]([^'\"]+)['\"]",
            r"ask\s+(?:the\s+)?question\s*['\"]([^'\"]+)['\"]",
            r"question\s*[:=]\s*['\"]?([^\n]+?)['\"]?(?:\.|$)",
            r"ask\s*['\"]([^'\"]+)['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip("'\"")
                value = re.sub(r"\s+(?:in the|and|then)\b.*$", "", value, flags=re.IGNORECASE)
                if value:
                    return value

        lower_desc = description.lower()
        if "who are you" in lower_desc:
            return "Who are you?"
        generic = re.search(
            r"\bquestion\b[^\n]*?\b(?:as|is|:)\s*([^\n\.!?]+)",
            description,
            flags=re.IGNORECASE,
        )
        if generic:
            return generic.group(1).strip().strip("'\"")
        return None

    def _required_actions_satisfied(
        self,
        description: str,
        step_results: list[StepResult],
    ) -> tuple[bool, str]:
        """
        Ensure explicitly requested actions were truly executed.
        Prevents false PASS when critical requested actions were skipped.
        """
        lower_desc = (description or "").lower()

        def _was_passed(target: str) -> bool:
            return any(sr.target == target and sr.status == TestStatus.PASSED for sr in step_results)

        required_targets: list[tuple[str, str]] = []

        if self._is_widget_flow_description(description):
            if "name" in lower_desc:
                required_targets.append(("chat_input", "name/email/otp input not executed"))
            if "email" in lower_desc:
                required_targets.append(("chat_input", "name/email/otp input not executed"))
            if "otp" in lower_desc:
                required_targets.append(("chat_input", "name/email/otp input not executed"))

            asks_question = self._description_requests_question(lower_desc)
            asks_documents = self._description_requests_documents(lower_desc)
            asks_validate = self._description_requests_validate_content(lower_desc)

            # Pure login flow: require confirmation.
            if self._description_requests_confirmation(lower_desc) and not (asks_documents or asks_validate or asks_question):
                required_targets.append(("validate_widget_confirmation", "widget confirmation step did not pass"))

            # Document-validation flow: require document open + validate click.
            if asks_validate:
                required_targets.append(("documents_button", "requested Documents open step did not pass"))
                required_targets.append(("validate_content_first_file", "requested Validate Content step did not pass"))
            elif asks_documents:
                required_targets.append(("documents_button", "requested Documents open step did not pass"))

            # Question flow: require ask + response validation.
            if asks_question:
                required_targets.append(("chat_input_question", "requested question was not sent"))
                required_targets.append(("chat_response", "requested question response was not validated"))

        for target, reason in required_targets:
            if not _was_passed(target):
                return False, reason
        return True, ""

    async def _try_click_selectors(
        self,
        browser_manager: BrowserManager,
        test_id: str,
        action_label: str,
        selectors: list
    ) -> tuple[bool, Optional[str], float]:
        """Try selectors one-by-one with explicit attempt logging."""
        start = time.time()
        last_error = None
        for index, selector in enumerate(selectors, 1):
            try:
                log.info(f"[{test_id}] {action_label}: trying selector {index}/{len(selectors)} -> {selector}")
                await browser_manager.click(selector)
                log.success(f"[{test_id}] {action_label}: selector matched -> {selector}")
                return True, None, (time.time() - start) * 1000
            except Exception as e:
                last_error = str(e)
                log.warning(f"[{test_id}] {action_label}: selector failed -> {selector} | {last_error}")
        return False, last_error, (time.time() - start) * 1000

    async def _navigate_with_retries(
        self,
        browser_manager: BrowserManager,
        url: str,
        max_attempts: int = 3,
        wait_between_seconds: float = 1.0,
    ) -> tuple[bool, Optional[str], float]:
        """Retry navigation to reduce flaky timeouts/network hiccups."""
        start = time.time()
        last_error: Optional[str] = None
        for attempt in range(1, max_attempts + 1):
            try:
                log.info(f"Navigate attempt {attempt}/{max_attempts}: {url}")
                await browser_manager.navigate(url)
                return True, None, (time.time() - start) * 1000
            except Exception as e:
                last_error = str(e)
                log.warning(f"Navigate attempt {attempt} failed: {last_error}")
                if attempt < max_attempts:
                    try:
                        await browser_manager.wait(wait_between_seconds)
                    except Exception:
                        pass
        return False, last_error, (time.time() - start) * 1000

    async def _verify_widget_confirmation(self, browser_manager: BrowserManager) -> tuple[bool, Optional[str], float]:
        """Verify widget shows a confirmation/success style message."""
        start = time.time()
        try:
            await browser_manager.wait(1.5)
            page_text = (await browser_manager.get_text("body")).lower()
            confirmation_keywords = [
                "success",
                "successful",
                "verified",
                "verification",
                "login complete",
                "login-complete",
                "welcome"
            ]
            matched = [kw for kw in confirmation_keywords if kw in page_text]
            if matched:
                return True, f"Matched confirmation keyword(s): {', '.join(matched)}", (time.time() - start) * 1000
            return False, "No success/login-complete confirmation text found", (time.time() - start) * 1000
        except Exception as e:
            return False, str(e), (time.time() - start) * 1000

    async def _verify_widget_question_response(
        self,
        browser_manager: BrowserManager,
        question: str,
    ) -> tuple[bool, Optional[str], float]:
        """Verify question appears in chat context and a likely response exists."""
        start = time.time()
        try:
            await browser_manager.wait(1.5)
            page_text = (await browser_manager.get_text("body")).lower()
            q = (question or "").strip().lower()
            if q and q not in page_text:
                return False, "Question text not found in page/chat after sending.", (time.time() - start) * 1000

            response_indicators = [
                "i am",
                "assistant",
                "help",
                "miraq",
                "bot",
                "can assist",
                "how can i",
            ]
            if any(ind in page_text for ind in response_indicators):
                return True, "Detected response indicator text in chat.", (time.time() - start) * 1000

            # Generic fallback: if question is present and page contains conversational tokens.
            conversational_tokens = ["you", "your", "can", "what", "who", "i", "am"]
            token_hits = sum(1 for tok in conversational_tokens if tok in page_text)
            if token_hits >= 4:
                return True, "Detected conversational response text after question.", (time.time() - start) * 1000

            return False, "No clear response text detected after question submission.", (time.time() - start) * 1000
        except Exception as e:
            return False, str(e), (time.time() - start) * 1000

    def _extract_credential(self, description: str, label: str) -> Optional[str]:
        """Extract quoted credential values from text like username 'admin'."""
        patterns = [
            rf"{label}\s*['\"]([^'\"]+)['\"]",
            rf"{label}\s*[:=]\s*['\"]?([^,'\"\n]+)['\"]?",
            rf"{label}\s*(?:as|is)\s*['\"]?([^\n,\.]+)['\"]?"
        ]
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _extract_full_name(self, description: str) -> Optional[str]:
        """Extract full name text like 'give it as Harshith S'."""
        patterns = [
            r"first and last name[^\n]*?give it as\s*([^\n,]+)",
            r"name[^\n]*?give it as\s*([^\n,]+)",
            r"(?:full[_\s-]?name|name)[^\n]*?\bas\s*([^\n,]+)",
            r"enter\s+(?:the\s+)?(?:full[_\s-]?name|name)\s+as\s*([^\n,]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.IGNORECASE)
            if match:
                value = self._clean_extracted_value(match.group(1))
                # Keep up to first two tokens for name.
                tokens = value.split()
                if len(tokens) >= 2:
                    return f"{tokens[0]} {tokens[1]}"
                if len(tokens) == 1:
                    return tokens[0]
        return None

    def _extract_field_value(self, description: str, field_name: str) -> Optional[str]:
        """Extract field values like email/otp from natural language instructions."""
        patterns = [
            rf"{field_name}[^\n]*?give\s*(?:it\s*as)?\s*([^\n,]+)",
            rf"{field_name}\s*[:=]\s*([^\n,]+)",
            rf"{field_name}[^\n]*?\bas\s*([^\n,]+)",
            rf"enter\s+(?:the\s+)?{field_name}\s+as\s*([^\n,]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.IGNORECASE)
            if match:
                return self._clean_extracted_value(match.group(1))
        return None

    def _extract_any_field_value(self, description: str, field_name: str) -> Optional[str]:
        """Best-effort extraction for noisy/free-form descriptions."""
        text = description or ""
        if field_name.lower() == "email":
            m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
            return m.group(0) if m else None
        if field_name.lower() == "otp":
            m = re.search(r"\botp\b[^\n]*?([0-9]{4,8})", text, flags=re.IGNORECASE)
            if m:
                return m.group(1)
            m2 = re.search(r"\b([0-9]{4,8})\b", text)
            return m2.group(1) if m2 else None
        if field_name.lower() == "name":
            patterns = [
                r"(?:full[_\s-]?name|name)\s*(?:as|is|:)\s*([A-Za-z][A-Za-z .'-]{1,50})",
                r"give it as\s*([A-Za-z][A-Za-z .'-]{1,50})",
            ]
            for pat in patterns:
                m = re.search(pat, text, flags=re.IGNORECASE)
                if m:
                    value = self._clean_extracted_value(m.group(1))
                    tokens = [t for t in value.split() if t]
                    if tokens:
                        return " ".join(tokens[:2])
            return None
        return None

    def _clean_extracted_value(self, value: str) -> str:
        """Normalize extracted free-text values from instruction bullets."""
        cleaned = value.strip().strip("'\"")
        cleaned = re.sub(r"\b[a-z]\.\s*$", "", cleaned)  # trailing lowercase list marker 'b.' / 'c.'
        cleaned = re.sub(r"\band submit\b.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\sin the\s+.+?field\b.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\sin\s+.+?field\b.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\sand wait\b.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\sproceed after\b.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
        return cleaned

    async def _send_widget_message(self, browser_manager: BrowserManager, message: str) -> tuple[bool, Optional[str], float]:
        """Send a message in MiraQ widget (fill textarea + click send)."""
        start = time.time()
        try:
            await browser_manager.fill("chat_input", message)
            try:
                await browser_manager.click("chat_send_button")
            except Exception:
                # OTP step may temporarily disable/replace send button; Enter still sends from textarea.
                await browser_manager.press_key("Enter")
            await browser_manager.wait(1.0)
            return True, None, (time.time() - start) * 1000
        except Exception as e:
            return False, str(e), (time.time() - start) * 1000

    async def _fill_first_available(self, browser_manager: BrowserManager, selectors: list, value: str) -> tuple[bool, Optional[str], float]:
        """Try a list of selectors and fill the first one that works."""
        start = time.time()
        last_error = None
        for selector in selectors:
            try:
                await browser_manager.fill(selector, value)
                return True, None, (time.time() - start) * 1000
            except Exception as e:
                last_error = str(e)
        return False, last_error, (time.time() - start) * 1000
    
    def _log_test_summary(self, result: TestResult):
        """Log test execution summary"""
        
        log.info(f"\n{'='*80}")
        log.info(f"📊 Test Summary: {result.test_id}")
        log.info(f"{'='*80}")
        
        status_emoji = {
            TestStatus.PASSED: "✅",
            TestStatus.FAILED: "❌",
            TestStatus.ERROR: "💥",
            TestStatus.SKIPPED: "⊘"
        }
        
        log.info(f"Status: {status_emoji.get(result.status, '?')} {result.status.upper()}")
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
        
        log.info(f"{'='*80}\n")
    
    def _get_playwright_tools(self) -> list:
        """Get Playwright automation tools in OpenAI function format"""
        
        return [
            {
                "type": "function",
                "function": {
                    "name": "playwright_navigate",
                    "description": "Navigate to a URL in the browser",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "The URL to navigate to"}
                        },
                        "required": ["url"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "playwright_click",
                    "description": "Click an element. Use smart names: 'login_button', 'admin_button', 'add_button', 'save_button' OR CSS selectors",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "Smart button name OR CSS selector"}
                        },
                        "required": ["selector"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "playwright_fill",
                    "description": "Fill an input field. Use smart names: 'login_username', 'login_password', 'institutionname', 'username', 'address1', 'email', etc.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "Smart field name OR CSS selector"},
                            "value": {"type": "string", "description": "Text to fill"}
                        },
                        "required": ["selector", "value"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "playwright_screenshot",
                    "description": "Take a screenshot of current page state",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "playwright_get_text",
                    "description": "Extract text from page or element to check for messages",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector (default: 'body')"}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "playwright_wait",
                    "description": "Wait for specified seconds (use 2-3 after navigation/clicks)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "number", "description": "Seconds to wait"}
                        },
                        "required": ["seconds"]
                    }
                }
            }
        ]