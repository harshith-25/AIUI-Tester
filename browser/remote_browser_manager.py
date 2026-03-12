"""
RemoteBrowserManager — drop-in replacement for BrowserManager.

Instead of driving a local Playwright browser, every action is serialized
as a JSON command and placed on an asyncio queue.  A WebSocket handler
reads the queue and relays the command to the frontend, which forwards it
to the Chrome Extension.  The extension executes the action and the
response travels back through the same path.
"""

import asyncio
import base64
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import settings
from utils.logger import log


class RemoteBrowserManager:
    """Sends browser commands via async queues (bridged by WebSocket)."""

    def __init__(self, test_id: str, command_queue: asyncio.Queue, response_queue: asyncio.Queue):
        self.test_id = test_id
        self.command_queue = command_queue
        self.response_queue = response_queue
        self.screenshot_counter = 0
        self._timeout = 30  # seconds to wait for each response
        # Extended timeout for video stop — encoding can take a few seconds longer
        self._video_stop_timeout = 60

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_command(self, action: str, data: dict | None = None, timeout: float | None = None) -> dict:
        """Put a command on the queue and wait for the response."""
        cmd = {"action": action, "data": data or {}}
        log.info(f"[RemoteBrowser] Sending command: {action} {data or ''}")
        await self.command_queue.put(cmd)
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            response = await asyncio.wait_for(
                self.response_queue.get(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Remote browser did not respond to '{action}' within {effective_timeout}s"
            )
        if response.get("status") != "success":
            raise Exception(response.get("message", f"{action} failed on remote browser"))
        return response

    # ------------------------------------------------------------------
    # Public API — mirrors BrowserManager exactly
    # ------------------------------------------------------------------

    async def start(self):
        """Ask the extension to open a new test tab."""
        log.info(f"Starting remote browser for test {self.test_id}")
        await self._send_command("start_browser", timeout=20)
        log.success("Remote browser started successfully")

    async def navigate(self, url: str):
        """Navigate to URL."""
        log.info(f"Navigating to {url}")
        await self._send_command("navigate", {"url": url})

    async def click(self, selector: str):
        """Click element."""
        log.info(f"Clicking: {selector}")
        actual_selector = self._resolve_selector(selector)
        await self._send_command("click", {"selector": actual_selector})

    async def fill(self, selector: str, value: str):
        """Fill input field."""
        log.info(f"Filling '{selector}' with '{value}'")
        actual_selector = self._resolve_selector(selector)
        await self._send_command("fill", {"selector": actual_selector, "value": value})

    async def get_text(self, selector: str = "body") -> str:
        """Extract text from page."""
        response = await self._send_command("get_text", {"selector": selector})
        return response.get("page_text", "")

    async def screenshot(self, name: Optional[str] = None) -> str:
        """Take screenshot — extension returns base64, we save to disk."""
        self.screenshot_counter += 1
        if not name:
            name = f"{self.test_id}_step_{self.screenshot_counter}_{datetime.now().strftime('%H%M%S')}.png"

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
        """Wait for specified seconds."""
        await self._send_command("wait", {"seconds": seconds})

    async def press_key(self, key: str):
        """Press a keyboard key."""
        log.info(f"Pressing key: {key}")
        await self._send_command("press_key", {"key": key})

    async def close(self):
        """Close remote browser tab."""
        try:
            await self._send_command("close_browser")
        except Exception as e:
            log.warning(f"Error closing remote browser: {e}")
        log.info("Remote browser closed")

    # ------------------------------------------------------------------
    # Video recording
    # ------------------------------------------------------------------

    async def start_video(self) -> None:
        """
        Tell the Chrome Extension to begin recording the active tab.
        Recording is best-effort — failure does not abort the test run.
        """
        log.info(f"[RemoteBrowser] Starting video recording for test {self.test_id}")
        try:
            await self._send_command("start_video", timeout=40)
            log.success("[RemoteBrowser] Video recording started")
        except Exception as e:
            log.warning(f"[RemoteBrowser] Could not start video recording (non-fatal): {e}")

    async def stop_video(self) -> Optional[str]:
        """
        Tell the Chrome Extension to stop recording.
        The extension returns the WebM as base64 in response["video"].
        We decode and save it alongside screenshots.

        Returns:
            Absolute path to the saved .webm file, or None on failure.
        """
        log.info(f"[RemoteBrowser] Stopping video recording for test {self.test_id}")
        try:
            response = await self._send_command(
                "stop_video",
                timeout=self._video_stop_timeout,
            )
        except Exception as e:
            log.warning(f"[RemoteBrowser] Could not stop video recording (non-fatal): {e}")
            return None

        video_b64: str = response.get("video", "")
        if not video_b64:
            log.warning("[RemoteBrowser] Extension returned no video data")
            return None

        # Save to videos_dir (falls back to screenshots_dir if not configured)
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
    # Selector resolution
    # ------------------------------------------------------------------

    def _resolve_selector(self, selector: str) -> str:
        """Resolve smart selector to actual CSS selector."""
        smart_selectors = {
            "login_username": 'input[type="text"]:first-of-type',
            "login_password": 'input[type="password"]:first-of-type',
            "login_button": "button.loginuserbutton",
            "chat_launcher": "#silfra-chat-widget-container button",
            "chat_start_button": "#silfra-chat-widget-container .xpert-home-action-icon",
            "chat_input": "#silfra-chat-widget-container textarea.xpert-chat-input",
            "chat_send_button": "#silfra-chat-widget-container button.xpert-send-btn",
            "admin_button": 'button:has(img[alt="Admin"])',
            "add_button": 'p:has-text("Add")',
            "save_button": 'button:has-text("Save")',
            "submit_button": 'button:has-text("Submit")',
            "cancel_button": 'button:has-text("Cancel")',
            "institutionname": "#institutionname",
            "username": "#username",
            "password": "#password",
            "confirmpassword": "#confirmpassword",
            "address1": "#address1",
            "address2": "#address2",
            "city": "#city",
            "state": "#state",
            "country": "#country",
            "pincode": "#pincode",
            "email": "#email",
            "contactnumber": "#contactnumber",
            "contactperson": "#contactperson",
        }
        return smart_selectors.get(selector, selector)