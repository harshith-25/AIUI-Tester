@echo off
REM AI UI Tester Quick Start Script for Windows

echo ========================================
echo AI UI Tester - Quick Start
echo ========================================
echo.

REM Check Python installation
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8 or higher.
    exit /b 1
)

echo Python found: 
python --version

REM Create virtual environment if not exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    echo Virtual environment created
)

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Install dependencies if needed
if not exist "venv\.dependencies_installed" (
    echo Installing dependencies...
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    playwright install chromium
    type nul > venv\.dependencies_installed
    echo Dependencies installed
)

REM Check .env file
if not exist ".env" (
    echo.
    echo WARNING: .env file not found!
    echo Creating .env file...
    (
        echo # GitHub Token (for Copilot AI^)
        echo GITHUB_TOKEN=your_github_token_here
        echo.
        echo # Browser Settings
        echo BROWSER_HEADLESS=false
        echo BROWSER_TIMEOUT=30000
        echo.
        echo # AI Model Settings
        echo AI_MODEL=gpt-4o
        echo AI_TEMPERATURE=0.1
        echo.
        echo # Test Execution Settings
        echo MAX_PARALLEL_TESTS=3
        echo RETRY_FAILED_TESTS=true
        echo.
        echo # Reporting
        echo GENERATE_HTML_REPORT=true
        echo GENERATE_CSV_REPORT=true
    ) > .env
    echo .env file created
    echo.
    echo Please edit .env and add your GITHUB_TOKEN
    pause
    exit /b 1
)

REM Check test_cases.csv
if not exist "test_cases.csv" (
    echo Generating sample test_cases.csv...
    python main.py --generate-sample
    echo.
    echo Sample test cases generated
    echo.
    echo Next steps:
    echo 1. Edit test_cases.csv with your test cases
    echo 2. Run: run.bat
    pause
    exit /b 0
)

REM Run tests
echo.
echo Starting test execution...
echo.

python main.py %*

set EXIT_CODE=%ERRORLEVEL%

REM Deactivate virtual environment
call venv\Scripts\deactivate.bat

echo.
if %EXIT_CODE%==0 (
    echo All tests completed successfully!
) else (
    echo Some tests failed. Check the reports for details.
)

pause
exit /b %EXIT_CODE%