from typing import Dict, List, Any
from utils.logger import log
from browser.browser_manager import BrowserManager
from datetime import datetime
import json
import time

class ToolExecutor:
    """Executes browser automation tools requested by AI agent"""
    
    def __init__(self, browser_manager: BrowserManager):
        self.browser = browser_manager
        self.execution_log = []
    
    async def execute_tools(self, tool_calls) -> List[Dict]:
        """Execute multiple tool calls and return results"""
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
                
                # Log execution
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
        """Execute a single tool"""
        
        if tool_name == 'playwright_navigate':
            url = tool_args.get('url', '')
            await self.browser.navigate(url)
            return f"Successfully navigated to {url}"
        
        elif tool_name == 'playwright_click':
            selector = tool_args.get('selector', '')
            await self.browser.click(selector)
            return f"Successfully clicked '{selector}'"
        
        elif tool_name == 'playwright_fill':
            selector = tool_args.get('selector', '')
            value = tool_args.get('value', '')
            await self.browser.fill(selector, value)
            return f"Successfully filled '{selector}'"
        
        elif tool_name == 'playwright_screenshot':
            path = await self.browser.screenshot()
            return f"Screenshot saved: {path}"
        
        elif tool_name == 'playwright_get_text':
            selector = tool_args.get('selector', 'body')
            text = await self.browser.get_text(selector)
            return f"Extracted text: {text[:1000]}"
        
        elif tool_name == 'playwright_wait':
            seconds = tool_args.get('seconds', 2)
            await self.browser.wait(seconds)
            return f"Waited {seconds} seconds"
        
        else:
            raise ValueError(f"Unknown tool: {tool_name}")
    
    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of tool executions"""
        total = len(self.execution_log)
        successful = sum(1 for e in self.execution_log if e['status'] == 'success')
        failed = sum(1 for e in self.execution_log if e['status'] == 'failed')
        
        return {
            "total_tools_executed": total,
            "successful": successful,
            "failed": failed,
            "success_rate": (successful / total * 100) if total > 0 else 0
        }