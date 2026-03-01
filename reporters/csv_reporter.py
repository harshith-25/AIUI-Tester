import csv
from pathlib import Path
from datetime import datetime
from typing import List
import pandas as pd

from models.test_result import TestResult, TestSuiteResult, StepResult, TestStatus
from utils.logger import log
from config.settings import settings


class CSVReporter:
    """Generate CSV reports from test results"""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or settings.results_dir
        self.output_dir.mkdir(exist_ok=True)

    def _report_suffix(self) -> str:
        """Return unique suffix that always ends with DDMMYYYY date."""
        return datetime.now().strftime('%H%M%S_%d%m%Y')
    
    def generate_report(self, suite_result: TestSuiteResult) -> Path:
        """Generate comprehensive CSV report"""
        
        suffix = self._report_suffix()
        report_path = self.output_dir / f"test_results_{suffix}.csv"
        
        log.info(f"📄 Generating CSV report: {report_path}")
        
        # Prepare data rows
        rows = []
        
        for test_result in suite_result.test_results:
            # If test has steps, create one row per step
            if test_result.step_results:
                for step in test_result.step_results:
                    row = self._create_step_row(test_result, step)
                    rows.append(row)
            else:
                # Test with no steps (e.g., error before execution)
                row = self._create_summary_row(test_result)
                rows.append(row)
        
        # Write to CSV
        if rows:
            df = pd.DataFrame(rows)
            
            # Reorder columns for better readability
            column_order = [
                'test_id', 'test_name', 'test_priority', 'test_category',
                'step_number', 'step_action', 'step_target', 'step_value',
                'step_status', 'step_result', 'step_duration_ms',
                'step_error', 'step_screenshot', 'ai_observation',
                'overall_test_status', 'test_duration_seconds',
                'total_steps', 'passed_steps', 'failed_steps',
                'expected_result', 'actual_result', 'validation_passed',
                'retry_count', 'test_start_time', 'test_end_time',
                'error_type', 'tags'
            ]
            
            # Only include columns that exist
            column_order = [col for col in column_order if col in df.columns]
            df = df[column_order]
            
            df.to_csv(report_path, index=False)
            
            log.success(f"✅ CSV report generated: {report_path}")
            log.info(f"   Total rows: {len(rows)}")
            log.info(f"   File size: {report_path.stat().st_size / 1024:.2f} KB")
        
        return report_path
    
    def generate_summary_report(self, suite_result: TestSuiteResult) -> Path:
        """Generate summary-only CSV report (one row per test)"""
        
        suffix = self._report_suffix()
        report_path = self.output_dir / f"test_summary_{suffix}.csv"
        
        log.info(f"📊 Generating summary CSV report: {report_path}")
        
        rows = []
        
        for test_result in suite_result.test_results:
            row = {
                'test_id': test_result.test_id,
                'test_name': test_result.test_name,
                'status': test_result.status.value,
                'result': '✅ PASS' if test_result.status == TestStatus.PASSED else '❌ FAIL',
                'duration_seconds': round(test_result.duration_seconds, 2),
                'total_steps': test_result.total_steps,
                'passed_steps': test_result.passed_steps,
                'failed_steps': test_result.failed_steps,
                'success_rate': round(test_result.success_rate, 1),
                'expected_result': test_result.expected_result,
                'actual_result': test_result.actual_result,
                'validation_passed': 'Yes' if test_result.validation_passed else 'No',
                'retry_count': test_result.retry_count,
                'error_type': test_result.error_type or '',
                'start_time': test_result.start_time.strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': test_result.end_time.strftime('%Y-%m-%d %H:%M:%S'),
                'screenshots_count': len(test_result.screenshots)
            }
            rows.append(row)
        
        df = pd.DataFrame(rows)
        df.to_csv(report_path, index=False)
        
        log.success(f"✅ Summary CSV report generated: {report_path}")
        
        return report_path
    
    def generate_statistics_report(self, suite_result: TestSuiteResult, statistics: dict) -> Path:
        """Generate statistics CSV report"""
        
        suffix = self._report_suffix()
        report_path = self.output_dir / f"test_statistics_{suffix}.csv"
        
        log.info(f"📈 Generating statistics CSV report: {report_path}")
        
        # Flatten statistics dict
        rows = []
        
        def flatten_dict(d, prefix=''):
            for key, value in d.items():
                if isinstance(value, dict):
                    flatten_dict(value, f"{prefix}{key}_")
                else:
                    rows.append({
                        'metric': f"{prefix}{key}",
                        'value': value
                    })
        
        # Add suite-level metrics
        rows.append({'metric': 'suite_name', 'value': suite_result.suite_name})
        rows.append({'metric': 'total_tests', 'value': suite_result.total_tests})
        rows.append({'metric': 'passed_tests', 'value': suite_result.passed_tests})
        rows.append({'metric': 'failed_tests', 'value': suite_result.failed_tests})
        rows.append({'metric': 'error_tests', 'value': suite_result.error_tests})
        rows.append({'metric': 'pass_rate_percent', 'value': round(suite_result.pass_rate, 2)})
        rows.append({'metric': 'total_duration_seconds', 'value': round(suite_result.duration_seconds, 2)})
        
        # Add detailed statistics
        flatten_dict(statistics)
        
        df = pd.DataFrame(rows)
        df.to_csv(report_path, index=False)
        
        log.success(f"✅ Statistics CSV report generated: {report_path}")
        
        return report_path
    
    def _create_step_row(self, test_result: TestResult, step: StepResult) -> dict:
        """Create a CSV row for a test step"""
        
        return {
            'test_id': test_result.test_id,
            'test_name': test_result.test_name,
            'test_priority': '',  # Can be added if stored in result
            'test_category': '',  # Can be added if stored in result
            'step_number': step.step_number,
            'step_action': step.action,
            'step_target': step.target or '',
            'step_value': step.value or '',
            'step_status': step.status.value,
            'step_result': '✅ PASS' if step.status == TestStatus.PASSED else '❌ FAIL' if step.status == TestStatus.FAILED else '⊘ SKIP',
            'step_duration_ms': round(step.duration_ms, 2),
            'step_error': step.error_message or '',
            'step_screenshot': step.screenshot_path or '',
            'ai_observation': step.ai_observation or '',
            'overall_test_status': test_result.status.value.upper(),
            'test_duration_seconds': round(test_result.duration_seconds, 2) if step.step_number == 1 else '',
            'total_steps': test_result.total_steps if step.step_number == 1 else '',
            'passed_steps': test_result.passed_steps if step.step_number == 1 else '',
            'failed_steps': test_result.failed_steps if step.step_number == 1 else '',
            'expected_result': test_result.expected_result if step.step_number == 1 else '',
            'actual_result': test_result.actual_result if step.step_number == 1 else '',
            'validation_passed': 'Yes' if test_result.validation_passed else 'No' if step.step_number == 1 else '',
            'retry_count': test_result.retry_count if step.step_number == 1 else '',
            'test_start_time': test_result.start_time.strftime('%Y-%m-%d %H:%M:%S') if step.step_number == 1 else '',
            'test_end_time': test_result.end_time.strftime('%Y-%m-%d %H:%M:%S') if step.step_number == 1 else '',
            'error_type': test_result.error_type or '',
            'tags': ''  # Can be added if stored in result
        }
    
    def _create_summary_row(self, test_result: TestResult) -> dict:
        """Create a CSV row for test without steps"""
        
        return {
            'test_id': test_result.test_id,
            'test_name': test_result.test_name,
            'test_priority': '',
            'test_category': '',
            'step_number': 0,
            'step_action': 'ERROR',
            'step_target': '',
            'step_value': '',
            'step_status': test_result.status.value,
            'step_result': '❌ FAIL',
            'step_duration_ms': 0,
            'step_error': test_result.actual_result,
            'step_screenshot': '',
            'ai_observation': '',
            'overall_test_status': test_result.status.value.upper(),
            'test_duration_seconds': round(test_result.duration_seconds, 2),
            'total_steps': 0,
            'passed_steps': 0,
            'failed_steps': 0,
            'expected_result': test_result.expected_result,
            'actual_result': test_result.actual_result,
            'validation_passed': 'No',
            'retry_count': test_result.retry_count,
            'test_start_time': test_result.start_time.strftime('%Y-%m-%d %H:%M:%S'),
            'test_end_time': test_result.end_time.strftime('%Y-%m-%d %H:%M:%S'),
            'error_type': test_result.error_type or 'Test execution error',
            'tags': ''
        }
    
    def generate_failure_report(self, suite_result: TestSuiteResult) -> Path:
        """Generate CSV report for failed tests only"""
        
        suffix = self._report_suffix()
        report_path = self.output_dir / f"test_failures_{suffix}.csv"
        
        failed_tests = [
            t for t in suite_result.test_results 
            if t.status in [TestStatus.FAILED, TestStatus.ERROR]
        ]
        
        if not failed_tests:
            log.info("No failed tests to report")
            return None
        
        log.info(f"📋 Generating failure report: {report_path}")
        
        rows = []
        for test_result in failed_tests:
            # Get first failed step
            failed_step = next(
                (s for s in test_result.step_results if s.status == TestStatus.FAILED),
                None
            )
            
            row = {
                'test_id': test_result.test_id,
                'test_name': test_result.test_name,
                'status': test_result.status.value,
                'error_type': test_result.error_type or '',
                'failed_at_step': failed_step.step_number if failed_step else 0,
                'failed_action': failed_step.action if failed_step else '',
                'error_message': failed_step.error_message if failed_step else test_result.actual_result,
                'expected_result': test_result.expected_result,
                'retry_count': test_result.retry_count,
                'duration_seconds': round(test_result.duration_seconds, 2),
                'timestamp': test_result.end_time.strftime('%Y-%m-%d %H:%M:%S')
            }
            rows.append(row)
        
        df = pd.DataFrame(rows)
        df.to_csv(report_path, index=False)
        
        log.success(f"✅ Failure report generated: {report_path}")
        log.info(f"   Failed tests: {len(failed_tests)}")
        
        return report_path
