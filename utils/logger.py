from loguru import logger
from config.settings import settings
import sys
from pathlib import Path
from datetime import datetime

def setup_logger():
    """Configure structured logging"""
    
    # Remove default handler
    logger.remove()
    
    # Console handler with colors
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=settings.log_level,
        colorize=True
    )
    
    # File handler
    if settings.log_to_file:
        log_file = settings.logs_dir / f"test_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        logger.add(
            log_file,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level="DEBUG",
            rotation="100 MB",
            retention="30 days",
            compression="zip"
        )
    
    return logger

# Global logger instance
log = setup_logger()