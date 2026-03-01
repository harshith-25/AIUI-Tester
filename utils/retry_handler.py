import asyncio
from typing import Callable, Any, Optional
from utils.logger import log
from config.settings import settings


class RetryHandler:
    """Handles retry logic with exponential backoff"""
    
    @staticmethod
    async def retry_async(
        func: Callable,
        *args,
        max_retries: int = None,
        delay: int = None,
        backoff_factor: float = 2.0,
        exceptions: tuple = (Exception,),
        **kwargs
    ) -> Any:
        """
        Retry an async function with exponential backoff
        
        Args:
            func: Async function to retry
            max_retries: Maximum number of retry attempts
            delay: Initial delay in seconds
            backoff_factor: Multiplier for delay after each retry
            exceptions: Tuple of exceptions to catch and retry
        """
        
        max_retries = max_retries or settings.max_retries
        delay = delay or settings.retry_delay
        
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            
            except exceptions as e:
                last_exception = e
                
                if attempt < max_retries:
                    wait_time = delay * (backoff_factor ** attempt)
                    log.warning(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                        f"Retrying in {wait_time:.1f}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    log.error(f"All {max_retries + 1} attempts failed")
                    raise last_exception
        
        raise last_exception
    
    @staticmethod
    def should_retry(exception: Exception) -> bool:
        """Determine if an exception should trigger a retry"""
        
        # Don't retry validation failures
        if "validation" in str(exception).lower():
            return False
        
        # Retry timeouts and connection errors
        retryable_keywords = [
            "timeout", "connection", "network", 
            "unreachable", "refused", "reset"
        ]
        
        error_str = str(exception).lower()
        return any(keyword in error_str for keyword in retryable_keywords)