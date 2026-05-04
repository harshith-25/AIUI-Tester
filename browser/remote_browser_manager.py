"""
RemoteBrowserManager — drop-in replacement for BrowserManager.

Instead of driving a local Playwright browser, every action is serialized
as a JSON command and placed on an asyncio queue.  A WebSocket handler
reads the queue and relays the command to the frontend, which forwards it
to the Chrome Extension.  The extension executes the action and the
response travels back through the same path.

All selectors are application-agnostic — no hardcoded product/brand names.
Human-like timing is injected automatically for fill and click actions.

FIX SUMMARY (vs previous version):
  1. evaluate_js() no longer tries unsupported action names ("evaluate",
     "execute_script", "eval") that the extension rejects with "Unknown action".
     It now uses only "run_script" — the single action name the extension
     actually supports — with a direct fallback to returning None when that
     too is unavailable. The javascript: URL fallback has been removed entirely;
     Chrome Extension APIs block javascript: navigations and it was generating
     a noisy, pointless error on every call.
  2. _send_command() has a simple retry (up to MAX_RETRIES attempts with
     exponential back-off) for transient queue/timeout failures so one
     slow response doesn't immediately abort the test.
  3. Minor: reload() fallback now calls _current_url directly without a
     second await get_page_url() to avoid an extra round-trip when the URL
     is already cached.
"""

import asyncio
import base64
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import settings
from utils.logger import log


# ---------------------------------------------------------------------------
# Universal selector library
# ---------------------------------------------------------------------------
UNIVERSAL_SELECTORS: dict[str, str] = {
    # ── Login inputs ─────────────────────────────────────────────────────────
    "login_username": (
        'input[autocomplete="username"], '
        'input[name="username" i], '
        'input[id="username" i], '
        'input[id*="username" i], '
        'input[id*="user" i], '
        'input[formcontrolname*="username" i], '
        'input[formcontrolname*="user" i], '
        'input[placeholder*="user name" i], '
        'input[placeholder*="username" i], '
        'input[aria-label*="user" i], '
        'input[data-testid*="user" i]'
    ),
    "login_email": (
        'input[type="email"], '
        'input[autocomplete="email"], '
        'input[name="email" i], '
        'input[id="email" i], '
        'input[id*="email" i], '
        'input[formcontrolname*="email" i], '
        'input[placeholder*="email" i], '
        'input[aria-label*="email" i], '
        'input[data-testid*="email" i]'
    ),
    "login_name": (
        'input[autocomplete="name"], '
        'input[autocomplete="given-name"], '
        'input[name="name" i], '
        'input[id="name" i], '
        'input[id*="fullname" i], '
        'input[id*="full_name" i], '
        'input[id*="name" i], '
        'input[formcontrolname*="name" i], '
        'input[placeholder*="full name" i], '
        'input[placeholder*="your name" i], '
        'input[placeholder="Name"], '
        'input[aria-label*="name" i], '
        'input[data-testid*="name" i]'
    ),
    "login_password": (
        'input[type="password"], '
        'input[autocomplete="current-password"], '
        'input[autocomplete="new-password"], '
        'input[name="password" i], '
        'input[id="password" i], '
        'input[id*="password" i], '
        'input[id*="pass" i], '
        'input[formcontrolname*="password" i], '
        'input[placeholder*="password" i], '
        'input[aria-label*="password" i]'
    ),
    "login_otp": (
        'input[autocomplete="one-time-code"], '
        'input[inputmode="numeric"][maxlength], '
        'input[name="otp" i], '
        'input[id="otp" i], '
        'input[id*="otp" i], '
        'input[name*="code" i], '
        'input[id*="code" i], '
        'input[formcontrolname*="otp" i], '
        'input[formcontrolname*="code" i], '
        'input[placeholder*="otp" i], '
        'input[placeholder*="one-time" i], '
        'input[placeholder*="verification" i], '
        'input[placeholder*="code" i], '
        'input[aria-label*="otp" i], '
        'input[aria-label*="verification" i]'
    ),

    # ── Auth buttons ──────────────────────────────────────────────────────────
    "login_button": (
        'button[type="submit"]:not([disabled]), '
        'input[type="submit"]:not([disabled]), '
        'button[id*="login" i], '
        'button[name*="login" i], '
        'button[class*="login" i], '
        'button[class*="signin" i], '
        'button[aria-label*="log in" i], '
        'button[aria-label*="sign in" i], '
        '[role="button"][aria-label*="login" i]'
    ),

    # ── Chat / conversational widget ──────────────────────────────────────────
    "chat_launcher": (
        '[aria-label*="chat" i][role="button"], '
        '[title*="chat" i][role="button"], '
        'button[aria-label*="chat" i], '
        'button[title*="chat" i], '
        '[data-testid*="chat-launcher"], '
        '[data-testid*="chat_launcher"], '
        'button[class*="chat-launcher"], '
        'button[class*="chat_launcher"], '
        '[class*="chat-widget"] button:first-of-type, '
        '[id*="chat-widget"] button:first-of-type, '
        '[class*="chatwidget"] button:first-of-type, '
        '[id*="chatwidget"] button:first-of-type'
    ),
    "chat_start_button": (
        '[aria-label*="start" i][role="button"], '
        'button[aria-label*="start chat" i], '
        'button[aria-label*="new chat" i], '
        '[data-testid*="start-chat"], '
        '[class*="chat"] button[class*="start"], '
        '[id*="chat"] button[class*="start"], '
        'button:has-text("Start"), '
        'button:has-text("New Chat")'
    ),
    "chat_input": (
        '[aria-label*="message" i] textarea, '
        '[aria-label*="chat" i] textarea, '
        'textarea[placeholder*="message" i], '
        'textarea[placeholder*="type" i], '
        'textarea[placeholder*="ask" i], '
        'textarea[placeholder*="enter" i], '
        '[data-testid*="chat-input"] textarea, '
        '[data-testid*="message-input"] textarea, '
        '[class*="chat"] textarea, '
        '[id*="chat"] textarea, '
        'form textarea:last-of-type'
    ),
    "chat_send_button": (
        'button[type="submit"][aria-label*="send" i], '
        'button[aria-label*="send" i], '
        'button[title*="send" i], '
        '[data-testid*="send-button"], '
        '[data-testid*="send_button"], '
        'button[class*="send"]:not([disabled]), '
        'button:has-text("Send")'
    ),

    # ── Generic CRUD actions ──────────────────────────────────────────────────
    "admin_button": (
        '[aria-label*="admin" i][role="button"], '
        'button[aria-label*="admin" i], '
        'a[href*="admin" i]:not([class*="disabled"]), '
        '[data-testid*="admin"]'
    ),
    "add_button": (
        'button[aria-label*="add" i]:not([disabled]), '
        'a[aria-label*="add" i], '
        '[data-testid*="add-button"], '
        'button:has-text("Add"), '
        'a:has-text("Add"), '
        'p:has-text("Add")'
    ),
    "save_button": (
        'button[type="submit"]:not([disabled]), '
        'button[aria-label*="save" i]:not([disabled]), '
        'a[aria-label*="save" i], '
        '[data-testid*="save-button"], '
        'button:has-text("Save"), '
        'a:has-text("Save"), '
        'button:has-text("Update"), '
        'a:has-text("Update")'
    ),
    "submit_button": (
        'button[type="submit"]:not([disabled]), '
        'input[type="submit"]:not([disabled]), '
        'button[aria-label*="submit" i]:not([disabled]), '
        '[data-testid*="submit-button"], '
        'button:has-text("Submit"), '
        'a:has-text("Submit")'
    ),
    "cancel_button": (
        'button[aria-label*="cancel" i], '
        'a[aria-label*="cancel" i], '
        '[data-testid*="cancel-button"], '
        'button:has-text("Cancel"), '
        'a:has-text("Cancel")'
    ),

    # ── Form fields by semantic name ──────────────────────────────────────────
    "institutionname": (
        'input[id="institutionname"], input[name="institutionname"], '
        'input[id*="institution" i], input[name*="institution" i], '
        'input[placeholder*="institution" i], input[aria-label*="institution" i]'
    ),
    "username":        'input[id="username"], input[name="username"]',
    "password":        'input[id="password"], input[name="password"], input[type="password"]',
    "confirmpassword": (
        'input[id="confirmpassword"], input[name="confirmpassword"], '
        'input[id*="confirm" i][type="password"], input[autocomplete="new-password"]'
    ),
    "address1": (
        'input[id="address1"], input[name="address1"], '
        'input[autocomplete="address-line1"], input[placeholder*="address line 1" i]'
    ),
    "address2": (
        'input[id="address2"], input[name="address2"], '
        'input[autocomplete="address-line2"], input[placeholder*="address line 2" i]'
    ),
    "city": (
        'input[id="city"], input[name="city"], '
        'input[autocomplete="address-level2"], input[placeholder*="city" i]'
    ),
    "state": (
        'input[id="state"], input[name="state"], select[id="state"], select[name="state"], '
        'input[autocomplete="address-level1"], input[placeholder*="state" i]'
    ),
    "country": (
        'input[id="country"], input[name="country"], select[id="country"], select[name="country"], '
        'input[autocomplete="country"], input[autocomplete="country-name"]'
    ),
    "pincode": (
        'input[id="pincode"], input[name="pincode"], input[id="zipcode"], '
        'input[autocomplete="postal-code"], input[inputmode="numeric"][maxlength="6"], '
        'input[placeholder*="pin" i], input[placeholder*="zip" i], input[placeholder*="postal" i]'
    ),
    "email":         'input[id="email"], input[name="email"], input[type="email"]',
    "contactnumber": (
        'input[id="contactnumber"], input[name="contactnumber"], '
        'input[type="tel"], input[autocomplete="tel"], '
        'input[placeholder*="phone" i], input[placeholder*="mobile" i], '
        'input[placeholder*="contact" i], input[aria-label*="phone" i]'
    ),
    "contactperson": (
        'input[id="contactperson"], input[name="contactperson"], '
        'input[placeholder*="contact person" i], input[aria-label*="contact person" i]'
    ),
}


# ---------------------------------------------------------------------------
# Human-like timing helpers
# ---------------------------------------------------------------------------

def _human_delay_ms() -> float:
    """Return a realistic inter-keystroke delay in milliseconds (50–180 ms)."""
    return random.uniform(50, 180)


def _human_pause_before_action() -> float:
    """Short pause (0.3–0.9 s) before clicking or submitting."""
    return random.uniform(0.3, 0.9)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The single action name our Chrome Extension actually supports for JS execution.
# "evaluate", "execute_script", "eval" are NOT supported and must not be tried.
_JS_ACTION = "run_script"

# Retry settings for transient failures in _send_command
_MAX_RETRIES    = 2
_RETRY_BASE_SEC = 1.0   # first retry after ~1 s, second after ~2 s


class RemoteBrowserManager:
    """Sends browser commands via async queues (bridged by WebSocket)."""

    def __init__(
        self,
        test_id: str,
        command_queue: asyncio.Queue,
        response_queue: asyncio.Queue,
    ):
        self.test_id        = test_id
        self.command_queue  = command_queue
        self.response_queue = response_queue
        self.screenshot_counter = 0
        self._timeout            = 30
        self._video_stop_timeout = 60
        self._current_url: str   = ""
        self._errors: list[dict] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _flush_stale_responses(self) -> None:
        """Drain leftover responses from a previous test."""
        drained = 0
        while True:
            try:
                self.response_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            log.warning(
                f"[RemoteBrowser] Flushed {drained} stale response(s) from previous test."
            )

    async def _send_command(
        self,
        action: str,
        data: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Put a command on the queue and wait for the response.

        Retries up to _MAX_RETRIES times on TimeoutError with exponential
        back-off.  Non-timeout failures (i.e. the extension returned an error
        status) are NOT retried — retrying would just repeat the same failure.
        """
        cmd              = {"action": action, "data": data or {}}
        effective_timeout = timeout if timeout is not None else self._timeout

        log.info(f"[RemoteBrowser] → {action} {data or ''}")

        last_error: Exception | None = None
        for attempt in range(1 + _MAX_RETRIES):
            await self.command_queue.put(cmd)
            try:
                response = await asyncio.wait_for(
                    self.response_queue.get(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                err_msg = (
                    f"Remote browser did not respond to '{action}' "
                    f"within {effective_timeout}s (attempt {attempt + 1})"
                )
                last_error = TimeoutError(err_msg)
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BASE_SEC * (2 ** attempt)
                    log.warning(f"[RemoteBrowser] Timeout — retrying in {wait:.1f}s…")
                    await asyncio.sleep(wait)
                continue

            # Got a response — check status
            if response.get("status") != "success":
                err_msg = response.get("message", f"{action} failed on remote browser")
                self._record_error(action, err_msg)
                raise Exception(err_msg)

            return response

        # All retries exhausted
        self._record_error(action, str(last_error))
        raise last_error

    def _record_error(self, action: str, message: str) -> None:
        entry = {
            "action":    action,
            "message":   message,
            "timestamp": datetime.now().isoformat(),
        }
        self._errors.append(entry)
        log.error(f"[RemoteBrowser] ✗ {action}: {message}")

    def get_all_errors(self) -> list[dict]:
        return list(self._errors)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        log.info(f"Starting remote browser for test {self.test_id}")
        await self._flush_stale_responses()
        await self._send_command("start_browser", timeout=20)
        log.success("Remote browser started successfully")

    async def navigate(self, url: str):
        log.info(f"Navigating to {url}")
        await self._send_command("navigate", {"url": url})
        self._current_url = url

    async def click(self, selector: str):
        actual_selector = self._resolve_selector(selector)
        pause = _human_pause_before_action()
        log.info(f"Clicking: {selector!r} (pause {pause:.2f}s)")
        await asyncio.sleep(pause)
        await self._send_command("click", {"selector": actual_selector})

    async def fill(self, selector: str, value: str):
        actual_selector = self._resolve_selector(selector)
        log.info(f"Filling '{selector}' with value (length={len(value)})")
        await self._send_command(
            "fill",
            {
                "selector":     actual_selector,
                "value":        value,
                "human_typing": True,
                "delay_ms":     _human_delay_ms(),
            },
        )

    async def get_text(self, selector: str = "body") -> str:
        response = await self._send_command("get_text", {"selector": selector})
        return response.get("page_text", "")

    async def screenshot(self, name: Optional[str] = None) -> str:
        self.screenshot_counter += 1
        if not name:
            name = (
                f"{self.test_id}_step_{self.screenshot_counter}"
                f"_{datetime.now().strftime('%H%M%S')}.png"
            )

        response        = await self._send_command("screenshot")
        screenshot_b64  = response.get("screenshot", "")
        screenshot_path = settings.screenshots_dir / name
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)

        if screenshot_b64:
            screenshot_path.write_bytes(base64.b64decode(screenshot_b64))
            log.info(f"Screenshot saved: {screenshot_path}")
        else:
            log.warning("Remote browser returned empty screenshot")

        return str(screenshot_path)

    async def wait(self, seconds: float):
        await self._send_command("wait", {"seconds": seconds})

    async def press_key(self, key: str):
        log.info(f"Pressing key: {key}")
        await self._send_command("press_key", {"key": key})

    async def reload(self):
        log.info("Reloading page")
        try:
            await self._send_command("reload")
        except Exception as e:
            err_msg = str(e).lower()
            if "unknown action" in err_msg or "reload" in err_msg:
                # Use cached URL — avoids an extra round-trip
                fallback_url = self._current_url
                if fallback_url:
                    log.info(
                        f"[RemoteBrowser] 'reload' not supported; "
                        f"falling back to navigate({fallback_url})"
                    )
                    await self._send_command("navigate", {"url": fallback_url})
                    await self._send_command("wait", {"seconds": 2})
                else:
                    raise
            else:
                raise

    async def accept_dialog(self) -> bool:
        try:
            await self._send_command("accept_dialog", timeout=5)
            log.info("[RemoteBrowser] Accepted browser dialog")
            return True
        except Exception as e:
            log.debug(f"[RemoteBrowser] accept_dialog not available or no dialog: {e}")
            return False

    async def get_page_url(self) -> str:
        try:
            response = await self._send_command("get_url", timeout=5)
            url = response.get("url", "")
            if url:
                self._current_url = url
            return url
        except Exception:
            return self._current_url

    async def evaluate_js(self, script: str) -> Any:
        """Execute JavaScript in the active tab via the Chrome Extension.

        FIX: The extension only supports the "run_script" action.
        Previously this method tried "evaluate", "execute_script", "eval"
        (all unsupported → "Unknown action" errors) and then fell back to a
        javascript: URL navigation which Chrome Extension APIs block outright.
        Now we try only "run_script" and return None gracefully if unavailable.
        """
        try:
            response = await self._send_command(
                _JS_ACTION, {"script": script}, timeout=10
            )
            return response.get("result", None)
        except Exception as e:
            err_msg = str(e).lower()
            if "unknown action" in err_msg or "unsupported" in err_msg:
                log.debug(
                    f"[RemoteBrowser] '{_JS_ACTION}' not supported by extension; "
                    "evaluate_js returning None"
                )
            else:
                log.debug(f"[RemoteBrowser] evaluate_js failed: {e}")
            return None

    async def close(self):
        try:
            await self._send_command("close_browser")
        except Exception as e:
            log.warning(f"Error closing remote browser: {e}")
        log.info("Remote browser closed")

    # ------------------------------------------------------------------
    # Video recording
    # ------------------------------------------------------------------

    async def start_video(self) -> None:
        log.info(f"[RemoteBrowser] Starting video recording for test {self.test_id}")
        try:
            await self._send_command("start_video", timeout=40)
            log.success("[RemoteBrowser] Video recording started")
        except Exception as e:
            log.warning(f"[RemoteBrowser] Could not start video (non-fatal): {e}")

    async def stop_video(self) -> Optional[str]:
        log.info(f"[RemoteBrowser] Stopping video recording for test {self.test_id}")
        try:
            response = await self._send_command(
                "stop_video", timeout=self._video_stop_timeout
            )
        except Exception as e:
            log.warning(f"[RemoteBrowser] Could not stop video (non-fatal): {e}")
            return None

        video_b64: str = response.get("video", "")
        if not video_b64:
            log.warning("[RemoteBrowser] Extension returned no video data")
            return None

        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        video_name = f"{self.test_id}_{timestamp}.webm"
        base_dir: Path = getattr(settings, "videos_dir", None) or settings.screenshots_dir
        video_path = base_dir / video_name
        video_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            video_path.write_bytes(base64.b64decode(video_b64))
            log.success(f"[RemoteBrowser] Video saved: {video_path}")
            return str(video_path)
        except Exception as e:
            log.error(f"[RemoteBrowser] Failed to write video file: {e}")
            return None

    # ------------------------------------------------------------------
    # Universal selector resolution
    # ------------------------------------------------------------------

    def _resolve_selector(self, selector: str) -> str:
        return UNIVERSAL_SELECTORS.get(selector, selector)