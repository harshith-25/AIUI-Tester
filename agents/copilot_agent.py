from openai import OpenAI
from typing import Dict, Any, List, Optional
from config.settings import settings
from utils.logger import log
from models.test_result import StepResult, TestStatus
from datetime import datetime
import json
import time

class CopilotAgent:
    """GitHub Copilot AI agent for test execution"""
    
    def __init__(self):
        self.client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=settings.github_token
        )
        self.model = settings.ai_model
        self.conversation_history = []
        self.step_results = []
    
    def initialize_conversation(self, test_description: str):
        """Initialize conversation with system prompt"""
        
        system_prompt = {
            "role": "system",
            "content": """You are an expert web automation testing agent powered by GitHub Copilot.

Core Capabilities:
- Navigate web pages using browser automation tools
- Intelligently locate elements using smart field names, text, or CSS selectors
- Fill forms and interact with web applications
- Capture screenshots for validation
- Extract and analyze page content
- Adapt to dynamic page layouts

Smart Field Naming Convention:
- Login: "login_username", "login_password", "login_button"
- Navigation: "admin_button", "add_button", "save_button", "submit_button"
- Form fields: "institutionname", "username", "address1", "email", etc.

Execution Guidelines:
1. Break down tests into clear, atomic steps
2. Use smart field names for common elements
3. Wait 2-3 seconds after navigation and clicks
4. Take screenshots before validations
5. Extract page text to verify messages
6. Report observations clearly after each action
7. If an action fails, try alternative selectors
8. Validate expected results thoroughly

Tools Available:
- playwright_navigate: Navigate to URL
- playwright_click: Click elements
- playwright_fill: Fill input fields
- playwright_screenshot: Capture screenshots
- playwright_get_text: Extract page text
- playwright_wait: Pause execution

Execute systematically and provide detailed feedback."""
        }
        
        user_prompt = {
            "role": "user",
            "content": f"""Execute this test:

{test_description}

Start with step 1 and proceed methodically. After each action, report what you observe."""
        }
        
        self.conversation_history = [system_prompt, user_prompt]
        log.info(f"Initialized conversation for test")
    
    async def execute_step(self, tools: List[Dict], tool_executor) -> Optional[Dict]:
        """Execute one step of the test using AI reasoning"""
        
        start_time = time.time()
        
        try:
            # Call Copilot
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                tools=tools,
                tool_choice="auto",
                temperature=settings.ai_temperature,
                max_tokens=settings.ai_max_tokens
            )
            
            message = response.choices[0].message
            
            # Process AI observation
            if message.content:
                log.info(f"🤖 Copilot: {message.content}")
                
                # Check for completion
                if self._is_completion_message(message.content):
                    return None  # Signal completion
            
            # Process tool calls
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
                
                # Execute tools
                tool_results = await tool_executor.execute_tools(message.tool_calls)
                
                # Add results to history
                for tool_result in tool_results:
                    self.conversation_history.append(tool_result)
                
                return {
                    "message": message.content,
                    "tools_executed": len(message.tool_calls),
                    "duration_ms": (time.time() - start_time) * 1000
                }
            else:
                # No tool calls
                self.conversation_history.append({
                    "role": "assistant",
                    "content": message.content
                })
                
                # Prompt to continue
                self.conversation_history.append({
                    "role": "user",
                    "content": "Continue with the next step or confirm completion."
                })
                
                return {
                    "message": message.content,
                    "tools_executed": 0,
                    "duration_ms": (time.time() - start_time) * 1000
                }
        
        except Exception as e:
            log.error(f"Error in Copilot step: {e}")
            raise
    
    def _is_completion_message(self, content: str) -> bool:
        """Check if message indicates test completion"""
        if not content:
            return False
        
        completion_phrases = [
            'test completed', 'test finished', 'test execution complete',
            'verification complete', 'successfully verified', 'test done',
            'test is complete', 'no further steps to execute', 'execution is complete'
        ]
        
        content_lower = content.lower()
        return any(phrase in content_lower for phrase in completion_phrases)
    
    def get_conversation_summary(self) -> List[str]:
        """Get summary of AI observations"""
        observations = []
        for msg in self.conversation_history:
            if msg.get("role") == "assistant" and msg.get("content"):
                observations.append(msg["content"])
        return observations
