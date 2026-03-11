from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime

class TestPriority(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"

class TestCategory(str, Enum):
    SMOKE = "Smoke Test"
    REGRESSION = "Regression"
    FUNCTIONALITY = "Functionality Test"
    FUNCTIONAL = "Functional"
    INTEGRATION = "Integration"
    E2E = "End-to-End"
    SECURITY = "Security"
    UTILITY = "Utility"

class TestCase(BaseModel):
    """Test case data model"""
    
    test_id: str = Field(..., description="Unique test identifier")
    test_name: str = Field(..., description="Human-readable test name")
    description: str = Field(..., description="Detailed test description")
    priority: TestPriority = Field(default=TestPriority.MEDIUM)
    category: TestCategory = Field(default=TestCategory.FUNCTIONAL)
    expected_result: str = Field(..., description="Expected outcome")
    tags: List[str] = Field(default_factory=list)
    retry_on_failure: bool = Field(default=True)
    timeout: Optional[int] = Field(default=None, description="Test timeout in seconds")
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    created_by: Optional[str] = None
    
    @field_validator('category', mode='before')
    @classmethod
    def validate_category(cls, v):
        if not v:
            return TestCategory.FUNCTIONAL
        
        # Exact match check
        if isinstance(v, TestCategory):
            return v
            
        s = str(v).strip()
        # Case-insensitive mapping
        mapping = {c.value.lower(): c for c in TestCategory}
        if s.lower() in mapping:
            return mapping[s.lower()]
            
        # Fallback to Functional
        return TestCategory.FUNCTIONAL

    @field_validator('test_id')
    @classmethod
    def validate_test_id(cls, v):
        if not v or not v.strip():
            raise ValueError("Test ID cannot be empty")
        return v.strip()
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        if len(v) < 20:
            raise ValueError("Test description must be at least 20 characters")
        return v
