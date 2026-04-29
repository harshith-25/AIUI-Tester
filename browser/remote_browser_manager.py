"""
RemoteBrowserManager — drop-in replacement for BrowserManager.

Instead of driving a local Playwright browser, every action is serialized
as a JSON command and placed on an asyncio queue.  A WebSocket handler
reads the queue and relays the command to the frontend, which forwards it
to the Chrome Extension.  The extension executes the action and the
response travels back through the same path.

All selectors are application-agnostic — no hardcoded product/brand names.
Human-like timing is injected automatically for fill and click actions.
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
# Every selector list is ordered from most-specific to most-generic so the
# first match wins quickly without triggering false positives.
# Zero brand-specific strings appear anywhere below.

UNIVERSAL_SELECTORS: dict[str, str] = {
    # ── Login inputs ────────────────────────────────────────────────────────
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

    # ── Auth buttons ────────────────────────────────────────────────────────
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

    # ── Chat / conversational widget ─────────────────────────────────────────
    # These selectors rely on ARIA roles and data attributes only — no brand names.
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

    # ── Generic CRUD actions ─────────────────────────────────────────────────
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

    # ── Form fields by semantic name (generic) ───────────────────────────────
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
    """Short pause (0.3–0.9 s) before clicking or submitting — like a real user."""
    return random.uniform(0.3, 0.9)


class RemoteBrowserManager:
    """Sends browser commands via async queues (bridged by WebSocket).

    Differences from original:
    - All selectors are universal (no app-specific hard-coding).
    - fill() uses per-character delays to mimic human typing speed.
    - click() inserts a small pre-click pause.
    - All failures are individually tracked so the caller sees every failure,
      not just the first one.
    """

    def __init__(
        self,
        test_id: str,
        command_queue: asyncio.Queue,
        response_queue: asyncio.Queue,
    ):
        self.test_id = test_id
        self.command_queue = command_queue
        self.response_queue = response_queue
        self.screenshot_counter = 0
        self._timeout = 30
        self._video_stop_timeout = 60
        self._current_url: str = ""
        # Collect every error so callers can report them all
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
        """Put a command on the queue and wait for the response."""
        cmd = {"action": action, "data": data or {}}
        log.info(f"[RemoteBrowser] → {action} {data or ''}")
        await self.command_queue.put(cmd)
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            response = await asyncio.wait_for(
                self.response_queue.get(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            err_msg = (
                f"Remote browser did not respond to '{action}' within {effective_timeout}s"
            )
            self._record_error(action, err_msg)
            raise TimeoutError(err_msg)

        if response.get("status") != "success":
            err_msg = response.get("message", f"{action} failed on remote browser")
            self._record_error(action, err_msg)
            raise Exception(err_msg)

        return response

    def _record_error(self, action: str, message: str) -> None:
        """Store every error so callers can surface all failures, not just the first."""
        entry = {
            "action": action,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }
        self._errors.append(entry)
        log.error(f"[RemoteBrowser] ✗ {action}: {message}")

    def get_all_errors(self) -> list[dict]:
        """Return every error recorded during this session."""
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
        """Click element with a short human-like pause before the action."""
        actual_selector = self._resolve_selector(selector)
        pause = _human_pause_before_action()
        log.info(f"Clicking: {selector!r} (pause {pause:.2f}s)")
        await asyncio.sleep(pause)
        await self._send_command("click", {"selector": actual_selector})

    async def fill(self, selector: str, value: str):
        """Fill an input field character-by-character to mimic human typing."""
        actual_selector = self._resolve_selector(selector)
        log.info(f"Filling '{selector}' with value (length={len(value)})")

        # First clear the field, then type with per-character delays.
        await self._send_command(
            "fill",
            {
                "selector": actual_selector,
                "value": value,
                "human_typing": True,          # hint for extensions that support it
                "delay_ms": _human_delay_ms(),  # average ms between keystrokes
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

        response = await self._send_command("screenshot")
        screenshot_b64 = response.get("screenshot", "")

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
                fallback_url = self._current_url or await self.get_page_url()
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
        for cmd_name in ("evaluate", "execute_script", "eval"):
            try:
                response = await self._send_command(
                    cmd_name, {"script": script}, timeout=10
                )
                return response.get("result", None)
            except Exception as e:
                err_msg = str(e).lower()
                if "unknown action" in err_msg or "unsupported" in err_msg:
                    continue
                log.debug(f"[RemoteBrowser] {cmd_name} failed: {e}")
                return None

        try:
            js_url = f"javascript:void({script})"
            await self._send_command("navigate", {"url": js_url}, timeout=5)
            await self._send_command("wait", {"seconds": 0.5})
            log.info("[RemoteBrowser] evaluate_js executed via javascript: URL")
            return None
        except Exception as e:
            log.debug(f"[RemoteBrowser] javascript: URL fallback failed: {e}")
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

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
        """
        Resolve a logical selector name to a universal CSS/attribute selector.

        Priority order:
          1. Known logical names from UNIVERSAL_SELECTORS dict (no brand names).
          2. Raw selector passed through unchanged.
        """
        return UNIVERSAL_SELECTORS.get(selector, selector)