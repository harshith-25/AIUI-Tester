import re

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from typing import Optional
from config.settings import settings
from utils.logger import log
from datetime import datetime
from pathlib import Path


class BrowserManager:
    """Manages browser lifecycle and operations"""
    
    def __init__(self, test_id: str):
        self.test_id = test_id
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.screenshot_counter = 0
    
    async def start(self):
        """Initialize browser"""
        log.info(f"Starting browser for test {self.test_id}")
        
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=settings.browser_headless,
            slow_mo=settings.browser_slow_mo
        )
        
        # Pre-create video dir — Playwright requires the directory to exist before context creation
        video_dir = None
        if not settings.browser_headless:
            video_dir = settings.results_dir / self.test_id / "videos"
            video_dir.mkdir(parents=True, exist_ok=True)
        
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            record_video_dir=str(video_dir) if video_dir else None
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(settings.browser_timeout)
        
        # Auto-accept all dialogs to prevent blocking execution
        async def handle_dialog(dialog):
            log.info(f"Automatically accepting dialog: {dialog.type} - {dialog.message}")
            try:
                await dialog.accept()
            except Exception as e:
                log.warning(f"Failed to accept dialog: {e}")
                
        self.page.on("dialog", handle_dialog)
        
        log.success("Browser started successfully")
    
    async def navigate(self, url: str):
        """Navigate to URL"""
        log.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until='domcontentloaded')
        await self.page.wait_for_timeout(2000)
    
    async def click(self, selector: str):
        """Click element with smart selector handling"""
        log.info(f"Clicking: {selector}")
        
        # Handle smart selectors
        actual_selector = self._resolve_selector(selector)

        clicked = await self._click_first_match(actual_selector)
        if not clicked:
            await self.page.click(actual_selector, timeout=4000)
        await self.page.wait_for_timeout(1000)
    
    async def fill(self, selector: str, value: str):
        """Fill input field"""
        log.info(f"Filling '{selector}' with '{value}'")
        
        actual_selector = self._resolve_selector(selector)

        semantic_kind = self._infer_field_kind(selector)
        if semantic_kind:
            locator = await self._find_semantic_fill_target(semantic_kind)
            if locator is not None:
                await self._fill_locator(locator, value)
                await self.page.wait_for_timeout(500)
                return

        locator = await self._find_first_fillable_locator(actual_selector)
        if locator is not None:
            await self._fill_locator(locator, value)
        else:
            await self.page.fill(actual_selector, "")
            await self.page.fill(actual_selector, value)
        await self.page.wait_for_timeout(500)
    
    async def get_text(self, selector: str = 'body') -> str:
        """Extract text from page"""
        text = await self.page.inner_text(selector)
        return text
    
    async def screenshot(self, name: Optional[str] = None) -> str:
        """Take screenshot"""
        self.screenshot_counter += 1
        
        if not name:
            name = f"{self.test_id}_step_{self.screenshot_counter}_{datetime.now().strftime('%H%M%S')}.png"
        
        screenshot_path = settings.screenshots_dir / name
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        
        log.info(f"Screenshot saved: {screenshot_path}")
        return str(screenshot_path)
    
    async def wait(self, seconds: float):
        """Wait for specified seconds"""
        await self.page.wait_for_timeout(int(seconds * 1000))
    
    def _resolve_selector(self, selector: str) -> str:
        """Resolve smart selector to actual CSS selector"""
        
        # Smart selector mappings
        smart_selectors = {
            # Login
            'login_username': (
                'input[name="username"], input[name="userName"], input[id="username"], '
                'input[id*="username" i], input[id*="user" i], input[formcontrolname="username"], '
                'input[formcontrolname="userName"], input[placeholder="User Name"], '
                'input[placeholder*="user name" i], input[placeholder*="username" i], '
                'input[autocomplete="username"], input[aria-label*="user" i], '
                'input[data-testid*="user" i]'
            ),
            'login_email': (
                'input[type="email"], input[name="email"], input[id="email"], '
                'input[id*="email" i], input[formcontrolname="email"], '
                'input[placeholder*="email" i], input[autocomplete="email"], '
                'input[aria-label*="email" i], input[data-testid*="email" i]'
            ),
            'login_name': (
                'input[name="name"], input[id="name"], input[id*="fullName" i], '
                'input[id*="fullname" i], input[id*="name" i], input[formcontrolname="name"], '
                'input[placeholder="Name"], input[placeholder*="full name" i], '
                'input[placeholder*="name" i], input[aria-label*="name" i]'
            ),
            'login_password': (
                'input[name="password"], input[name="passWord"], input[id="password"], '
                'input[id*="password" i], input[id*="pass" i], input[formcontrolname="password"], '
                'input[placeholder="Password"], input[placeholder*="password" i], '
                'input[autocomplete="current-password"], input[type="password"]'
            ),
            'login_otp': (
                'input[name="otp"], input[id="otp"], input[id*="otp" i], input[name*="code" i], '
                'input[id*="code" i], input[formcontrolname*="otp" i], input[formcontrolname*="code" i], '
                'input[placeholder*="otp" i], input[placeholder*="verification" i], '
                'input[placeholder*="code" i], input[inputmode="numeric"], input[autocomplete="one-time-code"]'
            ),
            'login_button': (
                'button.loginuserbutton, button[type="submit"], input[type="submit"], '
                'button[id*="login" i], button[name*="login" i], button[class*="login" i], '
                'button[class*="signin" i], button[class*="btn-info" i], '
                'button[aria-label*="login" i], button[aria-label*="log in" i], '
                'button[aria-label*="sign in" i]'
            ),
            'chat_launcher': '#silfra-chat-widget-container button',
            'chat_start_button': '#silfra-chat-widget-container .xpert-home-action-icon',
            'chat_input': '#silfra-chat-widget-container textarea.xpert-chat-input',
            'chat_send_button': '#silfra-chat-widget-container button.xpert-send-btn',
            
            # Navigation
            'admin_button': 'button:has(img[alt="Admin"])',
            'add_button': 'p:has-text("Add")',
            'save_button': 'button:has-text("Save")',
            'submit_button': 'button:has-text("Submit")',
            'cancel_button': 'button:has-text("Cancel")',
            
            # Form fields (by ID)
            'institutionname': '#institutionname',
            'username': '#username',
            'password': '#password',
            'confirmpassword': '#confirmpassword',
            'address1': '#address1',
            'address2': '#address2',
            'city': '#city',
            'state': '#state',
            'country': '#country',
            'pincode': '#pincode',
            'email': '#email',
            'contactnumber': '#contactnumber',
            'contactperson': '#contactperson',
        }
        
        return smart_selectors.get(selector, selector)

    def _infer_field_kind(self, selector: str) -> Optional[str]:
        raw = (selector or "").lower()
        if "chat_input" in raw:
            return None
        if "password" in raw:
            return "password"
        if "otp" in raw or "one-time-code" in raw or "verification" in raw:
            return "otp"
        if "email" in raw:
            return "email"
        if re.search(r"\bname\b", raw):
            return "name"
        if "user" in raw or "login_username" in raw:
            return "username"
        return None

    def _split_selector_candidates(self, selector: str) -> list[str]:
        return [part.strip() for part in (selector or "").split(",") if part.strip()]

    async def _click_first_match(self, selector: str) -> bool:
        for candidate in self._split_selector_candidates(selector):
            try:
                locators = self.page.locator(candidate)
                if await locators.count() == 0:
                    continue
                locator = locators.first
                await locator.scroll_into_view_if_needed()
                if not await locator.is_visible():
                    continue
                await locator.click(timeout=4000)
                return True
            except Exception:
                continue
        return False

    async def _find_first_fillable_locator(self, selector: str):
        for candidate in self._split_selector_candidates(selector):
            try:
                locators = self.page.locator(candidate)
                count = min(await locators.count(), 5)
                for index in range(count):
                    locator = locators.nth(index)
                    if await self._is_fillable(locator):
                        return locator
            except Exception:
                continue
        return None

    async def _is_fillable(self, locator) -> bool:
        try:
            return await locator.evaluate(
                """(el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const tag = (el.tagName || '').toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const visible = style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                    const editableTag = tag === 'input' || tag === 'textarea' || el.isContentEditable;
                    return visible && editableTag && !el.disabled && !el.readOnly && type !== 'hidden';
                }"""
            )
        except Exception:
            return False

    async def _find_semantic_fill_target(self, field_kind: str):
        locator = self.page.locator("input, textarea, [contenteditable='true'], [role='textbox']")
        try:
            count = min(await locator.count(), 20)
        except Exception:
            return None

        best_locator = None
        best_score = float("-inf")
        for index in range(count):
            candidate = locator.nth(index)
            if not await self._is_fillable(candidate):
                continue
            try:
                meta = await candidate.evaluate(
                    """(el) => {
                        const attr = (name) => (el.getAttribute(name) || '').trim();
                        const ownText = `${attr('name')} ${attr('id')} ${attr('placeholder')} ${attr('aria-label')} ${attr('data-testid')} ${attr('formcontrolname')} ${attr('autocomplete')}`.toLowerCase();
                        const labelText = [];
                        if (el.labels) {
                            for (const label of el.labels) {
                                labelText.push((label.innerText || label.textContent || '').trim());
                            }
                        }
                        const parentLabel = el.closest('label');
                        if (parentLabel) {
                            labelText.push((parentLabel.innerText || parentLabel.textContent || '').trim());
                        }
                        const previous = el.previousElementSibling;
                        if (previous) {
                            labelText.push((previous.innerText || previous.textContent || '').trim());
                        }
                        return {
                            tag: (el.tagName || '').toLowerCase(),
                            type: attr('type').toLowerCase(),
                            text: `${ownText} ${labelText.join(' ')}`.toLowerCase(),
                        };
                    }"""
                )
            except Exception:
                continue

            score = self._score_fill_candidate(field_kind, meta or {})
            if score > best_score:
                best_score = score
                best_locator = candidate

        return best_locator if best_score >= 2 else None

    def _score_fill_candidate(self, field_kind: str, meta: dict) -> int:
        text = str(meta.get("text") or "")
        input_type = str(meta.get("type") or "")
        score = 0

        if field_kind == "username":
            positives = ["username", "user name", "user id", "userid", "login id", "account"]
            negatives = ["password", "otp", "code", "search", "filter", "email", "name"]
            if input_type in {"text"}:
                score += 1
        elif field_kind == "email":
            positives = ["email", "e-mail", "mail"]
            negatives = ["password", "otp", "code", "search", "filter", "username", "name"]
            if input_type == "email":
                score += 4
        elif field_kind == "password":
            positives = ["password", "passcode", "pin"]
            negatives = ["confirm", "search", "filter", "otp", "email", "username"]
            if input_type == "password":
                score += 5
        elif field_kind == "otp":
            positives = ["otp", "one time", "one-time", "verification", "verify", "code"]
            negatives = ["password", "postal code", "zip code", "search", "filter"]
            if input_type in {"number", "tel"}:
                score += 3
        elif field_kind == "name":
            positives = ["full name", "fullname", "name", "display name"]
            negatives = ["username", "email", "password", "otp", "search", "filter"]
            if input_type in {"text"}:
                score += 1
        else:
            positives = []
            negatives = []

        score += sum(3 for token in positives if token in text)
        score -= sum(4 for token in negatives if token in text)
        return score

    async def _fill_locator(self, locator, value: str):
        await locator.scroll_into_view_if_needed()
        await locator.click(timeout=4000)
        try:
            await locator.fill("")
            await locator.fill(value)
            return
        except Exception:
            pass

        try:
            await locator.press("Control+A")
            await locator.type(value, delay=20)
            return
        except Exception:
            pass

        await locator.evaluate(
            """(el, nextValue) => {
                if (el.isContentEditable) {
                    el.textContent = '';
                    el.focus();
                    document.execCommand('insertText', false, nextValue);
                } else {
                    el.value = nextValue;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
    
    async def press_key(self, key: str):
        """Press a keyboard key"""
        log.info(f"Pressing key: {key}")
        await self.page.keyboard.press(key)

    async def close(self):
        """Close browser"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        
        log.info("Browser closed")
