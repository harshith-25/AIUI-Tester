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
        
        await self.page.click(actual_selector, timeout=4000)
        await self.page.wait_for_timeout(1000)
    
    async def fill(self, selector: str, value: str):
        """Fill input field"""
        log.info(f"Filling '{selector}' with '{value}'")
        
        actual_selector = self._resolve_selector(selector)
        
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
            'login_username': 'input[type="text"]:first-of-type',
            'login_password': 'input[type="password"]:first-of-type',
            'login_button': 'button.loginuserbutton',
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