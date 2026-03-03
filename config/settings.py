from pydantic_settings import BaseSettings
from typing import Optional, List
from pathlib import Path
import os

class Settings(BaseSettings):
    """Application configuration with environment variable support"""
    
    # API Keys
    github_token: str
    
    # Browser settings
    browser_headless: bool = False
    browser_timeout: int = 30000
    browser_slow_mo: int = 0
    
    # AI Model settings
    ai_model: str = "gpt-4o"
    ai_temperature: float = 0.1
    ai_max_tokens: int = 2000
    ai_max_iterations: int = 10
    
    # Test execution settings
    max_parallel_tests: int = 3
    retry_failed_tests: bool = True
    max_retries: int = 2
    retry_delay: int = 5
    
    # Paths
    test_cases_path: Path = Path("test_cases.csv")
    results_dir: Path = Path("test_results")
    screenshots_dir: Path = Path("screenshots")
    logs_dir: Path = Path("logs")
    
    # Reporting
    generate_html_report: bool = True
    generate_junit_report: bool = True
    generate_csv_report: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_to_file: bool = True
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    def setup_directories(self):
        """Create necessary directories"""
        self.results_dir.mkdir(exist_ok=True)
        self.screenshots_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

# Global settings instance
settings = Settings()