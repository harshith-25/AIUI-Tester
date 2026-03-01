from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime

class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"

class StepResult(BaseModel):
    """Individual step result"""
    step_number: int
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    status: TestStatus
    duration_ms: float
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    ai_observation: Optional[str] = None

class TestResult(BaseModel):
    """Complete test result"""
    
    # Test identification
    test_id: str
    test_name: str
    
    # Execution details
    status: TestStatus
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    
    # Steps
    total_steps: int
    passed_steps: int
    failed_steps: int
    skipped_steps: int
    step_results: List[StepResult]
    
    # Validation
    expected_result: str
    actual_result: str
    validation_passed: bool
    
    # Additional data
    screenshots: List[str] = Field(default_factory=list)
    logs: List[str] = Field(default_factory=list)
    ai_observations: List[str] = Field(default_factory=list)
    
    # Retry information
    retry_count: int = 0
    is_retry: bool = False
    
    # Error details
    error_type: Optional[str] = None
    error_stack_trace: Optional[str] = None
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate of steps"""
        if self.total_steps == 0:
            return 0.0
        return (self.passed_steps / self.total_steps) * 100

class TestSuiteResult(BaseModel):
    """Aggregated test suite results"""
    
    suite_name: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    
    total_tests: int
    passed_tests: int
    failed_tests: int
    skipped_tests: int
    error_tests: int
    
    test_results: List[TestResult]
    
    @property
    def pass_rate(self) -> float:
        """Calculate pass rate"""
        if self.total_tests == 0:
            return 0.0
        return (self.passed_tests / self.total_tests) * 100