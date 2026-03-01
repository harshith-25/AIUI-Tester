from pathlib import Path
from typing import Dict, Any

from reporters.csv_reporter import CSVReporter
from reporters.html_reporter import HTMLReporter
from models.test_result import TestSuiteResult
from utils.logger import log
from config.settings import settings


class ReporterFactory:
    """Factory for creating and managing reporters"""
    
    @staticmethod
    def generate_all_reports(
        suite_result: TestSuiteResult,
        statistics: Dict[str, Any] = None,
        failure_analysis: Dict[str, Any] = None
    ) -> Dict[str, Path]:
        """Generate all enabled reports"""
        
        log.info("📊 Generating test reports...")
        
        reports = {}
        
        # CSV Reports
        if settings.generate_csv_report:
            csv_reporter = CSVReporter()
            detailed_csv = csv_reporter.generate_report(suite_result)
            reports['csv'] = detailed_csv
        
        # HTML Report
        if settings.generate_html_report:
            html_reporter = HTMLReporter()
            html_report = html_reporter.generate_report(
                suite_result,
                statistics,
                failure_analysis
            )
            reports['html'] = html_report
        
        # JUnit Report
        if settings.generate_junit_report:
            from reporters.junit_reporter import JUnitReporter
            junit_reporter = JUnitReporter()
            junit_report = junit_reporter.generate_report(suite_result)
            reports['junit'] = junit_report
        
        log.success(f"✅ Generated {len(reports)} report(s)")
        
        return reports
