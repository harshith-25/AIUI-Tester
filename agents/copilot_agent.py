"""
CopilotAgent — AI brain that drives browser automation like Antigravity.

Key design principles:
1. DOM-first: Always inspect the page before acting.
2. Adaptive: If a selector fails, inspect DOM again and try alternatives.
3. Sliding-window: Keep conversation history bounded to avoid token overflow.
4. Verify after every action: Read page text to confirm success.
"""

from openai import OpenAI
from typing import Dict, Any, List, Optional
from config.settings import settings
from utils.logger import log
from models.test_result import StepResult, TestStatus
from datetime import datetime
import json
import time


# ── System prompt — this is the core intelligence ─────────────────────────
SYSTEM_PROMPT = """You are an expert web automation agent driving a real browser. You execute UI test cases step by step with perfect accuracy.

## Your Execution Flow (FOLLOW THIS EXACTLY)

For EVERY action you take:
1. **INSPECT first** — Call `playwright_get_dom` to see the current page structure. This is your eyes.
2. **PLAN** — Based on the DOM, identify the exact selector for the element you need.
3. **ACT** — Execute the action (click, fill, select, etc.) using the most specific selector.
4. **WAIT** — Call `playwright_wait` for 2-3 seconds after actions that trigger navigation, modals, or AJAX.
5. **VERIFY** — Call `playwright_get_text` on `body` to confirm the action succeeded.

## Selector Strategy (use in this order of preference)

1. `#elementId` — by ID (most reliable)
2. `input[name="fieldname"]` / `input[formcontrolname="fieldname"]` — by attribute
3. `input[type="password"]` / `select.classname` — by type/class
4. `[aria-label*="text" i]` / `[placeholder*="text" i]` — by accessibility attributes
5. `button:has-text('Save')` / `text=Login` — by visible text (last resort)

## Handling Specific UI Patterns

### Login forms
- Look for input fields in the DOM. Match them by `name`, `id`, `formcontrolname`, `placeholder`, or `type`.
- For username: look for `input[name="username"]`, `input[name="userName"]`, etc.
- For password: look for `input[type="password"]` — this is always reliable.
- For the login button: look for `button[type="submit"]` or `button:has-text('Login')`.
- After clicking login, ALWAYS wait 3 seconds, then inspect DOM to verify you reached the next page.

### Login forms with 3 fields (name + username + password)
- Some apps have a "name" field before username. Check the DOM for 3 input fields.
- Fill them in order: name first, then username, then password.
- Look for `input[name="name"]` or the first visible text input.

### Native <select> dropdowns
NEVER click options inside a `<select>`. Use `playwright_select_option` instead:
- By text: `{"selector": "select.myclass", "option_text": "Doctor"}`
- By index: `{"selector": "select.myclass", "option_index": 1}`
- First available: `{"selector": "select.myclass"}`

### Custom dropdowns (Angular Material, PrimeNG, etc.)
- Click the dropdown trigger element first.
- Wait 1 second for the overlay to appear.
- Inspect DOM to find the option elements (usually `mat-option`, `p-dropdownitem`, `li.option`, etc.).
- Click the desired option.

### Modifying existing input values (dates, numbers, etc.)
1. Call `playwright_clear` with the field selector to erase current value.
2. Call `playwright_fill` with the new value.
3. Both steps are required — `playwright_fill` alone will NOT clear existing text.

### Calendar / date interactions
- Inspect DOM to find calendar navigation buttons (Next, Previous, arrows).
- Click date cells by their visible text or by specific selectors.
- For date inputs: clear first, then fill with new value.

### Dialogs and confirmations
- After a delete/submit action, a confirmation dialog may appear.
- Inspect DOM first — look for modal overlays, `.modal`, `[role="dialog"]`, etc.
- Click the confirm/OK/Yes button inside the dialog.

### Tables and lists
- To select an item, inspect the table/list DOM structure first.
- Look for `table tbody tr`, `.list-group-item`, `mat-row`, etc.
- Click the first row or the specific row matching the test requirement.

### Form filling (create/edit flows)
- Inspect DOM to find all input fields on the form.
- Fill each field with appropriate test data based on field type:
  - Email fields: use a valid email format (e.g., `test@example.com`)
  - Phone fields: use digits (e.g., `9876543210`)
  - Name fields: use readable names (e.g., `Test User`)
  - Password fields: use `Test@1234`
  - Generic text: use descriptive values (e.g., `Test Institution`, `Test Service`)

## Tools Available
- `playwright_navigate` — Go to a URL
- `playwright_click` — Click an element (CSS selector or text-based)
- `playwright_fill` — Type text into an input/textarea
- `playwright_clear` — Clear an existing input field (use before fill to modify values)
- `playwright_select_option` — Select from a native <select> dropdown
- `playwright_get_text` — Read text from the page or an element
- `playwright_get_dom` — Get simplified DOM structure (tags, IDs, classes, text). THIS IS YOUR EYES.
- `playwright_evaluate_js` — Execute JavaScript in the page
- `playwright_press_key` — Press a keyboard key (Tab, Enter, Escape, etc.)
- `playwright_screenshot` — Take a screenshot
- `playwright_wait` — Wait N seconds

## CRITICAL Rules
1. ALWAYS call `playwright_get_dom` before your first action on any new page or after any navigation.
2. NEVER guess selectors — find them in the DOM first.
3. When a selector fails, call `playwright_get_dom` again to find alternatives. Try at least 3 different selectors.
4. After login, ALWAYS wait 3+ seconds for the dashboard to load, then inspect DOM.
5. After form submission (Save/Submit), wait 2 seconds and verify success by reading page text.
6. Complete each described step fully before moving to the next.
7. When ALL steps from the test description are done AND verified, include "TEST EXECUTION COMPLETE" in your response.
8. If you encounter an error you cannot recover from after 3 attempts, include "TEST EXECUTION FAILED: <reason>" in your response.
9. Be precise and methodical. Quality over speed."""


class CopilotAgent:
    """AI agent for test execution — thinks like Antigravity.

    This agent drives the browser exactly the way the Antigravity browser
    subagent does: inspect the DOM first, pick precise selectors, use JS
    for complex interactions, and verify outcomes by reading page content.
    """

    def __init__(self):
        self.client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=settings.github_token
        )
        self.model = settings.ai_model
        self.conversation_history: list[dict] = []
        self.step_results: list = []
        self._total_messages = 0

    def initialize_conversation(self, test_description: str):
        """Initialize conversation with system prompt and test description."""

        system_msg = {
            "role": "system",
            "content": SYSTEM_PROMPT
        }

        user_msg = {
            "role": "user",
            "content": f"""Execute this test case step by step. Follow EVERY instruction precisely.

            IMPORTANT: Before your first action, call `playwright_get_dom` to see the page structure.
            After login and page transitions, always call `playwright_get_dom` again.

            TEST CASE:
            {test_description}

            Start now. Navigate to the URL first, then proceed with each step in order.
            After completing ALL steps, say "TEST EXECUTION COMPLETE" with a summary of what was verified."""
        }

        self.conversation_history = [system_msg, user_msg]
        self._total_messages = 2
        log.info("Initialized AI conversation for test")

    async def execute_step(self, tools: list[dict], tool_executor) -> Optional[dict]:
        """Execute one iteration of the AI test loop.

        Returns:
            dict with execution info, or None if test is complete.
        """

        start_time = time.time()

        # ── Sliding window: trim conversation if too long ──
        self._trim_conversation()

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                tools=tools,
                tool_choice="auto",
                temperature=settings.ai_temperature,
                max_tokens=settings.ai_max_tokens
            )

            message = response.choices[0].message

            # ── Process AI reasoning ──
            if message.content:
                log.info(f"🤖 AI Agent: {message.content[:500]}")

                if self._is_completion_message(message.content):
                    return None  # Test complete

                if self._is_failure_message(message.content):
                    return None  # Test failed — let the engine handle status

            # ── Process tool calls ──
            if hasattr(message, 'tool_calls') and message.tool_calls:
                # Add assistant message to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        } for tc in message.tool_calls
                    ]
                })
                self._total_messages += 1

                # Execute tools
                tool_results = await tool_executor.execute_tools(message.tool_calls)

                # Add tool results to history
                for tool_result in tool_results:
                    self.conversation_history.append(tool_result)
                    self._total_messages += 1

                    # If a tool failed, prompt the AI to retry with DOM inspection
                    if "Error:" in tool_result.get("content", ""):
                        self._inject_retry_guidance(tool_result["content"])

                return {
                    "message": message.content,
                    "tools_executed": len(message.tool_calls),
                    "duration_ms": (time.time() - start_time) * 1000
                }
            else:
                # No tool calls — AI is thinking or needs a nudge
                self.conversation_history.append({
                    "role": "assistant",
                    "content": message.content
                })
                self._total_messages += 1

                # Nudge to continue
                self.conversation_history.append({
                    "role": "user",
                    "content": "Continue with the next step. Use `playwright_get_dom` to inspect the page, then take action with the appropriate tool."
                })
                self._total_messages += 1

                return {
                    "message": message.content,
                    "tools_executed": 0,
                    "duration_ms": (time.time() - start_time) * 1000
                }

        except Exception as e:
            log.error(f"Error in AI step: {e}")
            raise

    # ── Conversation management ──────────────────────────────────────────

    def _trim_conversation(self):
        """Sliding window: keep system prompt + last N messages.

        This prevents token overflow while preserving the AI's ability
        to understand the current context.
        """
        max_msgs = settings.ai_conversation_max_messages

        if len(self.conversation_history) <= max_msgs:
            return

        # Always keep: system prompt (index 0) + original task (index 1)
        preserved_start = self.conversation_history[:2]

        # Keep the most recent messages
        recent_count = max_msgs - 2  # minus the 2 preserved messages
        recent = self.conversation_history[-recent_count:]

        # Add a context bridge summarizing what was trimmed
        trimmed_count = len(self.conversation_history) - len(preserved_start) - len(recent)
        bridge = {
            "role": "user",
            "content": (
                f"[Context note: {trimmed_count} earlier messages were trimmed. "
                f"You are continuing test execution. Check the DOM with playwright_get_dom "
                f"to understand the current page state before acting.]"
            )
        }

        self.conversation_history = preserved_start + [bridge] + recent
        log.info(f"Trimmed conversation: removed {trimmed_count} old messages, kept {len(self.conversation_history)}")

    def _inject_retry_guidance(self, error_content: str):
        """When a tool fails, guide the AI to inspect DOM and retry."""
        guidance = {
            "role": "user",
            "content": (
                "The previous action failed. Follow this recovery procedure:\n"
                "1. Call `playwright_get_dom` to inspect the current page structure.\n"
                "2. Find the correct element using the DOM output.\n"
                "3. Try an alternative selector.\n"
                "Do NOT give up — try at least 2 more alternative selectors."
            )
        }
        self.conversation_history.append(guidance)
        self._total_messages += 1

    # ── Completion detection ─────────────────────────────────────────────

    def _is_completion_message(self, content: str) -> bool:
        """Check if the AI is signaling test completion."""
        if not content:
            return False
        content_lower = content.lower()

        completion_phrases = [
            'test execution complete',
            'test completed successfully',
            'all steps completed',
            'test case completed',
            'test has been completed',
            'execution is complete',
            'test done',
            'test finished',
            'verification complete',
            'all steps have been executed',
            'test execution finished',
        ]
        return any(phrase in content_lower for phrase in completion_phrases)

    def _is_failure_message(self, content: str) -> bool:
        """Check if the AI is signaling unrecoverable failure."""
        if not content:
            return False
        content_lower = content.lower()
        return 'test execution failed:' in content_lower

    # ── Conversation summary ─────────────────────────────────────────────

    def get_conversation_summary(self) -> list[str]:
        """Get summary of AI observations."""
        observations = []
        for msg in self.conversation_history:
            if msg.get("role") == "assistant" and msg.get("content"):
                observations.append(msg["content"])
        return observations

    def get_last_ai_message(self) -> Optional[str]:
        """Get the most recent AI assistant message."""
        for msg in reversed(self.conversation_history):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"]
        return None