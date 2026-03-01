#!/bin/bash
# AI UI Tester Quick Start Script

set -e

echo "🚀 AI UI Tester - Quick Start"
echo "=============================="
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.8 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "✓ Python $PYTHON_VERSION found"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "✓ Virtual environment created"
fi

# Activate virtual environment
source venv/bin/activate
echo "✓ Virtual environment activated"

# Install dependencies if needed
if [ ! -f "venv/.dependencies_installed" ]; then
    echo "Installing dependencies..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    playwright install chromium
    touch venv/.dependencies_installed
    echo "✓ Dependencies installed"
fi

# Check if .env exists
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  .env file not found!"
    echo "Creating .env file..."
    cat > .env << 'EOF'
# GitHub Token (for Copilot AI)
GITHUB_TOKEN=your_github_token_here

# Browser Settings
BROWSER_HEADLESS=false
BROWSER_TIMEOUT=30000

# AI Model Settings
AI_MODEL=gpt-4o
AI_TEMPERATURE=0.1
AI_MAX_ITERATIONS=35

# Test Execution Settings
MAX_PARALLEL_TESTS=3
RETRY_FAILED_TESTS=true
MAX_RETRIES=2

# Reporting
GENERATE_HTML_REPORT=true
GENERATE_CSV_REPORT=true
GENERATE_JUNIT_REPORT=true

# Logging
LOG_LEVEL=INFO
LOG_TO_FILE=true
EOF
    echo "✓ .env file created"
    echo ""
    echo "⚠️  Please edit .env and add your GITHUB_TOKEN"
    echo ""
    exit 1
fi

# Check if test_cases.csv exists
if [ ! -f "test_cases.csv" ]; then
    echo "Generating sample test_cases.csv..."
    python3 main.py --generate-sample
    echo ""
    echo "✓ Sample test cases generated"
    echo ""
    echo "📝 Next steps:"
    echo "1. Edit test_cases.csv with your test cases"
    echo "2. Run: ./run.sh"
    exit 0
fi

# Run tests
echo ""
echo "🧪 Starting test execution..."
echo ""

python3 main.py "$@"

EXIT_CODE=$?

# Deactivate virtual environment
deactivate

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ All tests completed successfully!"
else
    echo "⚠️  Some tests failed. Check the reports for details."
fi

exit $EXIT_CODE