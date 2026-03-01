#!/usr/bin/env python3
"""
AI UI Tester - Intelligent Web Application Testing Framework
Powered by GitHub Copilot and Playwright
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime
import argparse
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

from config.settings import settings
from utils.logger import log
from utils.csv_reader import CSVTestCaseReader, generate_sample_csv
from core.test_runner import TestRunner
from core.result_aggregator import ResultAggregator
from reporters import ReporterFactory

# Ensure Unicode output works on Windows terminals with legacy code pages.
for stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


class AIUITester:
    """Main application class"""
    
    def __init__(self):
        self.test_runner = TestRunner()
        self.start_time = None
        self.end_time = None
    
    async def run(self, args):
        """Main execution flow"""
        
        try:
            # Setup
            settings.setup_directories()
            self._print_banner()
            
            # Validate and load test cases
            test_cases = self._load_test_cases(args.input)
            
            if not test_cases:
                log.error("No test cases to execute")
                return 1
            
            # Display test plan
            self._display_test_plan(test_cases)
            
            if args.dry_run:
                log.info("Dry run mode - exiting without execution")
                return 0
            
            # Confirm execution
            if not args.yes and not self._confirm_execution():
                log.info("Execution cancelled by user")
                return 0
            
            # Execute tests
            self.start_time = datetime.now()
            suite_result = await self.test_runner.run_test_suite(test_cases)
            self.end_time = datetime.now()
            
            # Analyze results
            statistics = ResultAggregator.get_statistics(suite_result)
            failure_analysis = ResultAggregator.get_failure_analysis(suite_result)
            
            # Display results
            self._display_results(suite_result, statistics, failure_analysis)
            
            # Generate reports
            reports = ReporterFactory.generate_all_reports(
                suite_result,
                statistics,
                failure_analysis
            )
            
            self._display_report_paths(reports)
            
            # Return exit code based on results
            return 0 if suite_result.failed_tests == 0 else 1
        
        except KeyboardInterrupt:
            log.warning("\n⚠️  Execution interrupted by user")
            return 130
        
        except Exception as e:
            log.error(f"Fatal error: {e}")
            import traceback
            log.error(traceback.format_exc())
            return 1
    
    def _load_test_cases(self, input_path: str):
        """Load test cases from CSV"""
        
        csv_path = Path(input_path) if input_path else settings.test_cases_path
        
        log.info(f"Loading test cases from: {csv_path}")
        
        # Validate CSV format
        is_valid, errors = CSVTestCaseReader.validate_csv_format(csv_path)
        
        if not is_valid:
            log.error("CSV validation failed:")
            for error in errors:
                log.error(f"  - {error}")
            return None
        
        # Read test cases
        reader = CSVTestCaseReader(csv_path)
        test_cases = reader.read_test_cases()
        
        return test_cases
    
    def _display_test_plan(self, test_cases):
        """Display test execution plan"""
        
        console.print("\n")
        console.print(Panel.fit(
            "[bold cyan]Test Execution Plan[/bold cyan]",
            border_style="cyan"
        ))
        
        # Create table
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=4)
        table.add_column("Test ID", style="cyan")
        table.add_column("Test Name", style="white")
        table.add_column("Priority", style="yellow")
        table.add_column("Category", style="green")
        table.add_column("Retry", justify="center")
        
        for i, test in enumerate(test_cases, 1):
            priority_color = {
                "Critical": "red bold",
                "High": "red",
                "Medium": "yellow",
                "Low": "green"
            }.get(test.priority.value, "white")
            
            table.add_row(
                str(i),
                test.test_id,
                test.test_name[:40] + "..." if len(test.test_name) > 40 else test.test_name,
                f"[{priority_color}]{test.priority.value}[/{priority_color}]",
                test.category.value,
                "✓" if test.retry_on_failure else "✗"
            )
        
        console.print(table)
        console.print(f"\n[bold]Total Tests:[/bold] {len(test_cases)}")
        console.print(f"[bold]Parallel Execution:[/bold] {settings.max_parallel_tests} concurrent")
        console.print(f"[bold]Retry Enabled:[/bold] {settings.retry_failed_tests}")
        console.print("")
    
    def _confirm_execution(self) -> bool:
        """Ask user to confirm execution"""
        
        response = console.input("[yellow]Proceed with test execution? [y/N]:[/yellow] ")
        return response.lower() in ['y', 'yes']
    
    def _display_results(self, suite_result, statistics, failure_analysis):
        """Display comprehensive test results"""
        
        console.print("\n")
        console.print("=" * 80)
        console.print("[bold cyan]TEST EXECUTION SUMMARY[/bold cyan]")
        console.print("=" * 80)
        
        # Status indicator
        if suite_result.pass_rate == 100:
            status = "[bold green]✓ ALL TESTS PASSED[/bold green]"
        elif suite_result.pass_rate >= 80:
            status = "[bold yellow]⚠ MOSTLY PASSED[/bold yellow]"
        else:
            status = "[bold red]✗ TESTS FAILED[/bold red]"
        
        console.print(f"\nStatus: {status}")
        
        # Create results table
        results_table = Table(show_header=False, box=None)
        results_table.add_column("Metric", style="bold")
        results_table.add_column("Value", justify="right")
        
        results_table.add_row("Total Tests", str(suite_result.total_tests))
        results_table.add_row(
            "[green]✓ Passed[/green]",
            f"[green]{suite_result.passed_tests}[/green]"
        )
        results_table.add_row(
            "[red]✗ Failed[/red]",
            f"[red]{suite_result.failed_tests}[/red]"
        )
        results_table.add_row(
            "[yellow]⚠ Errors[/yellow]",
            f"[yellow]{suite_result.error_tests}[/yellow]"
        )
        results_table.add_row(
            "[dim]⊘ Skipped[/dim]",
            f"[dim]{suite_result.skipped_tests}[/dim]"
        )
        results_table.add_row("", "")
        results_table.add_row(
            "[bold]Pass Rate[/bold]",
            f"[bold]{suite_result.pass_rate:.1f}%[/bold]"
        )
        results_table.add_row(
            "[bold]Duration[/bold]",
            f"[bold]{suite_result.duration_seconds:.2f}s[/bold]"
        )
        
        console.print(results_table)
        
        # Statistics
        if statistics:
            console.print("\n[bold]Execution Statistics:[/bold]")
            console.print(f"  Average Test Duration: {statistics['execution']['average_test_duration']:.2f}s")
            console.print(f"  Total Steps Executed: {statistics['steps']['total_steps']}")
            console.print(f"  Tests Retried: {statistics['retries']['tests_retried']}")
        
        # Failure analysis
        if failure_analysis and failure_analysis.get('total_failures', 0) > 0:
            console.print("\n[bold red]Failure Analysis:[/bold red]")
            console.print(f"  Failed Tests: {', '.join(failure_analysis['failed_test_ids'])}")
            
            if failure_analysis.get('common_failure_steps'):
                console.print(f"  Common Failure Points:")
                for step, count in list(failure_analysis['common_failure_steps'].items())[:3]:
                    console.print(f"    • {step}: {count} times")
        
        console.print("=" * 80)
    
    def _display_report_paths(self, reports):
        """Display generated report paths"""
        
        console.print("\n[bold cyan]Generated Reports:[/bold cyan]")
        
        for report_type, report_path in reports.items():
            icon = "📄" if "csv" in report_type else "🌐" if "html" in report_type else "📋"
            console.print(f"  {icon} {report_type:20s} → {report_path}")
        
        # Open HTML in browser if available
        html_report = reports.get('html')
        if html_report:
            console.print(f"\n[bold green]Open HTML report:[/bold green] file://{html_report.absolute()}")
    
    def _print_banner(self):
        """Print application banner"""
        
        banner = """
╔═══════════════════════════════════════════════════════════════════════════╗
║                                                                           ║
║   █████╗ ██╗    ██╗   ██╗██╗    ████████╗███████╗███████╗████████╗      ║
║  ██╔══██╗██║    ██║   ██║██║    ╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝      ║
║  ███████║██║    ██║   ██║██║       ██║   █████╗  ███████╗   ██║         ║
║  ██╔══██║██║    ██║   ██║██║       ██║   ██╔══╝  ╚════██║   ██║         ║
║  ██║  ██║██║    ╚██████╔╝██║       ██║   ███████╗███████║   ██║         ║
║  ╚═╝  ╚═╝╚═╝     ╚═════╝ ╚═╝       ╚═╝   ╚══════╝╚══════╝   ╚═╝         ║
║                                                                           ║
║              Intelligent Web Testing with GitHub Copilot                 ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
        """
        
        console.print(banner, style="bold cyan")
        console.print(f"[dim]Version 1.0.0 | Powered by GitHub Copilot & Playwright[/dim]\n")


def parse_arguments():
    """Parse command line arguments"""
    
    parser = argparse.ArgumentParser(
        description="AI-powered UI testing framework with GitHub Copilot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all tests from default test_cases.csv
  python main.py
  
  # Run tests from custom CSV file
  python main.py -i my_tests.csv
  
  # Generate sample test cases file
  python main.py --generate-sample
  
  # Dry run (validate without executing)
  python main.py --dry-run
  
  # Run with specific settings
  python main.py --parallel 5 --no-retry
  
  # Headless mode (no browser UI)
  python main.py --headless
  
  # Generate only HTML report
  python main.py --html-only
        """
    )
    
    # Input/Output
    parser.add_argument(
        '-i', '--input',
        help='Path to test cases CSV file (default: test_cases.csv)',
        default=None
    )
    
    parser.add_argument(
        '-o', '--output',
        help='Output directory for results (default: test_results/)',
        default=None
    )
    
    # Execution options
    parser.add_argument(
        '--parallel',
        type=int,
        help=f'Number of parallel tests (default: {settings.max_parallel_tests})',
        default=None
    )
    
    parser.add_argument(
        '--no-retry',
        action='store_true',
        help='Disable automatic retry of failed tests'
    )
    
    parser.add_argument(
        '--max-retries',
        type=int,
        help=f'Maximum retry attempts (default: {settings.max_retries})',
        default=None
    )
    
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run browser in headless mode (no UI)'
    )
    
    parser.add_argument(
        '--timeout',
        type=int,
        help='Browser timeout in milliseconds (default: 30000)',
        default=None
    )
    
    # Report options
    parser.add_argument(
        '--html-only',
        action='store_true',
        help='Generate only HTML report'
    )
    
    parser.add_argument(
        '--csv-only',
        action='store_true',
        help='Generate only CSV reports'
    )
    
    parser.add_argument(
        '--no-reports',
        action='store_true',
        help='Skip report generation'
    )
    
    # Utility commands
    parser.add_argument(
        '--generate-sample',
        action='store_true',
        help='Generate sample test_cases.csv file and exit'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate test cases without executing'
    )
    
    parser.add_argument(
        '-y', '--yes',
        action='store_true',
        help='Skip confirmation prompt'
    )
    
    # Logging
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Set logging level',
        default=None
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    
    # Version
    parser.add_argument(
        '--version',
        action='version',
        version='AI UI Tester v1.0.0'
    )
    
    return parser.parse_args()


def apply_arguments(args):
    """Apply command line arguments to settings"""
    
    if args.output:
        settings.results_dir = Path(args.output)
    
    if args.parallel:
        settings.max_parallel_tests = args.parallel
    
    if args.no_retry:
        settings.retry_failed_tests = False
    
    if args.max_retries:
        settings.max_retries = args.max_retries
    
    if args.headless:
        settings.browser_headless = True
    
    if args.timeout:
        settings.browser_timeout = args.timeout
    
    if args.html_only:
        settings.generate_csv_report = False
        settings.generate_junit_report = False
        settings.generate_html_report = True
    
    if args.csv_only:
        settings.generate_html_report = False
        settings.generate_junit_report = False
        settings.generate_csv_report = True
    
    if args.no_reports:
        settings.generate_html_report = False
        settings.generate_csv_report = False
        settings.generate_junit_report = False
    
    if args.log_level:
        settings.log_level = args.log_level
    
    if args.verbose:
        settings.log_level = 'DEBUG'


def main():
    """Main entry point"""
    
    args = parse_arguments()
    
    # Handle utility commands
    if args.generate_sample:
        try:
            generate_sample_csv(settings.test_cases_path)
            console.print(f"\n[green]✓[/green] Sample test cases generated: {settings.test_cases_path}")
            console.print("\n[bold]Next steps:[/bold]")
            console.print("1. Edit test_cases.csv with your test cases")
            console.print("2. Run: python main.py")
            console.print("3. View results in test_results/ directory\n")
            return 0
        except Exception as e:
            console.print(f"[red]✗[/red] Error generating sample: {e}")
            return 1
    
    # Apply settings
    apply_arguments(args)
    
    # Run tests
    app = AIUITester()
    
    try:
        exit_code = asyncio.run(app.run(args))
        return exit_code
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Interrupted by user[/yellow]")
        return 130
    except Exception as e:
        console.print(f"\n[red]✗ Fatal error: {e}[/red]")
        if args.verbose:
            import traceback
            console.print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
