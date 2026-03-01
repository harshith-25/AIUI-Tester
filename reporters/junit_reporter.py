from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

from config.settings import settings
from models.test_result import TestSuiteResult, TestStatus
from utils.logger import log


class JUnitReporter:
    """Generate JUnit XML reports for CI integration."""

    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or settings.results_dir
        self.output_dir.mkdir(exist_ok=True)

    def _report_suffix(self) -> str:
        """Return unique suffix that always ends with DDMMYYYY date."""
        return datetime.now().strftime("%H%M%S_%d%m%Y")

    def generate_report(self, suite_result: TestSuiteResult) -> Path:
        """Generate a JUnit XML report from suite results."""
        suffix = self._report_suffix()
        report_path = self.output_dir / f"junit_report_{suffix}.xml"

        log.info(f"🧪 Generating JUnit report: {report_path}")

        testsuite = ET.Element(
            "testsuite",
            name=suite_result.suite_name,
            tests=str(suite_result.total_tests),
            failures=str(suite_result.failed_tests),
            errors=str(suite_result.error_tests),
            skipped=str(suite_result.skipped_tests),
            time=f"{suite_result.duration_seconds:.3f}",
            timestamp=suite_result.start_time.isoformat()
        )

        properties = ET.SubElement(testsuite, "properties")
        ET.SubElement(properties, "property", name="pass_rate", value=f"{suite_result.pass_rate:.2f}")

        for test in suite_result.test_results:
            testcase = ET.SubElement(
                testsuite,
                "testcase",
                classname="ui_tests",
                name=f"{test.test_id} - {test.test_name}",
                time=f"{test.duration_seconds:.3f}"
            )

            if test.status == TestStatus.FAILED:
                failure = ET.SubElement(testcase, "failure", message="Validation failed", type="AssertionError")
                failure.text = test.actual_result
            elif test.status == TestStatus.ERROR:
                error = ET.SubElement(testcase, "error", message="Execution error", type=test.error_type or "ExecutionError")
                error.text = test.error_stack_trace or test.actual_result
            elif test.status == TestStatus.SKIPPED:
                ET.SubElement(testcase, "skipped")

            system_out = ET.SubElement(testcase, "system-out")
            system_out.text = "\n".join(test.ai_observations[-5:])

        tree = ET.ElementTree(testsuite)
        ET.indent(tree, space="  ")
        tree.write(report_path, encoding="utf-8", xml_declaration=True)

        log.success(f"✅ JUnit report generated: {report_path}")
        return report_path
