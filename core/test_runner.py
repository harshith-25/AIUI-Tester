import asyncio
from typing import List
from datetime import datetime

from models.test_case import TestCase
from models.test_result import TestResult, TestSuiteResult, TestStatus
from core.test_engine import TestEngine
from utils.logger import log
from config.settings import settings
from utils.retry_handler import RetryHandler


# Auth-failure messages that should NOT be retried — retrying them wastes time
# and risks locking accounts.
_AUTH_FAILURE_PATTERNS = [
    "invalid credentials",
    "invalid username",
    "invalid password",
    "incorrect password",
    "authentication failed",
    "unauthorized",
    "access denied",
    "login failed",
    "wrong password",
    "account locked",
    "too many attempts",
    # Environment/runtime failures that retries cannot fix.
    "notimplementederror",
    "_make_subprocess_transport",
    "asyncio.create_subprocess_exec",
]


class TestRunner:
    """Manages parallel test execution and orchestration"""

    def __init__(self):
        self.test_engine = TestEngine()
        self.retry_handler = RetryHandler()

    async def run_test_suite(
        self, test_cases: List[TestCase], browser_queues: dict = None
    ) -> TestSuiteResult:
        """Run complete test suite with parallel execution"""

        log.info("=" * 80)
        log.info("Starting Test Suite Execution")
        log.info("=" * 80)
        log.info(f"Total Tests   : {len(test_cases)}")
        log.info(f"Max Parallel  : {settings.max_parallel_tests}")
        log.info(f"Retry Enabled : {settings.retry_failed_tests}")
        if browser_queues:
            log.info("Mode          : Remote Browser (Chrome Extension)")
        log.info("=" * 80)

        start_time = datetime.now()

        # Remote browser and single-parallel mode both require sequential execution
        if browser_queues or settings.max_parallel_tests <= 1:
            test_results = await self._run_sequential(test_cases, browser_queues=browser_queues)
        else:
            test_results = await self._run_parallel(test_cases)

        # Retry failed tests if enabled
        if settings.retry_failed_tests:
            test_results = await self._retry_failed_tests(
                test_cases, test_results, browser_queues=browser_queues
            )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        suite_result = self._aggregate_results(
            test_cases, test_results, start_time, end_time, duration
        )

        self._log_suite_summary(suite_result)
        return suite_result

    # -------------------------------------------------------------------------
    # Sequential execution
    # -------------------------------------------------------------------------

    async def _run_sequential(
        self, test_cases: List[TestCase], browser_queues: dict = None
    ) -> List[TestResult]:
        log.info("Running tests sequentially...")

        results = []
        for i, test_case in enumerate(test_cases, 1):
            log.info(f"\n{'#' * 80}")
            log.info(f"Test {i}/{len(test_cases)}: {test_case.test_id} — {test_case.test_name}")
            log.info(f"{'#' * 80}")

            result = await self.test_engine.execute_test(
                test_case, browser_queues=browser_queues
            )
            results.append(result)

            # Give the app time to fully reset between tests
            await asyncio.sleep(settings.delay_between_tests if hasattr(settings, "delay_between_tests") else 2)

        return results

    # -------------------------------------------------------------------------
    # Parallel execution
    # -------------------------------------------------------------------------

    async def _run_parallel(self, test_cases: List[TestCase]) -> List[TestResult]:
        log.info(
            f"Running tests in parallel (max {settings.max_parallel_tests} concurrent)..."
        )

        semaphore = asyncio.Semaphore(settings.max_parallel_tests)

        async def run_with_semaphore(test_case: TestCase, index: int):
            async with semaphore:
                log.info(
                    f"\n{'#' * 80}\nStarting Test {index}/{len(test_cases)}: "
                    f"{test_case.test_id}\n{'#' * 80}"
                )
                result = await self.test_engine.execute_test(test_case)
                log.info(f"Completed Test {index}/{len(test_cases)}: {test_case.test_id}")
                return result

        tasks = [
            run_with_semaphore(tc, i + 1) for i, tc in enumerate(test_cases)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for i, result in enumerate(raw_results):
            if isinstance(result, Exception):
                log.error(f"Test {i + 1} raised exception: {result}")
                tc = test_cases[i]
                # FIX: use actual start/end times so duration is non-zero
                now = datetime.now()
                final_results.append(
                    TestResult(
                        test_id=tc.test_id,
                        test_name=tc.test_name,
                        status=TestStatus.ERROR,
                        start_time=now,
                        end_time=now,
                        duration_seconds=0,
                        total_steps=0,
                        passed_steps=0,
                        failed_steps=0,
                        skipped_steps=0,
                        step_results=[],
                        expected_result=tc.expected_result,
                        actual_result=f"Exception: {str(result)}",
                        validation_passed=False,
                        error_type=type(result).__name__,
                    )
                )
            else:
                final_results.append(result)

        return final_results

    # -------------------------------------------------------------------------
    # Smart retry — skip auth failures, respect per-test retry_on_failure flag
    # -------------------------------------------------------------------------

    async def _retry_failed_tests(
        self,
        test_cases: List[TestCase],
        initial_results: List[TestResult],
        browser_queues: dict = None,
    ) -> List[TestResult]:

        failed_pairs = [
            (test_cases[i], result)
            for i, result in enumerate(initial_results)
            if result.status in {TestStatus.FAILED, TestStatus.ERROR}
            and test_cases[i].retry_on_failure
            and not self._is_auth_failure(result)  # FIX: don't retry credential failures
        ]

        if not failed_pairs:
            return initial_results

        log.warning(f"\n{'=' * 80}")
        log.warning(f"Retrying {len(failed_pairs)} failed test(s)")
        log.warning(f"{'=' * 80}")

        final_results = list(initial_results)

        for test_case, original_result in failed_pairs:
            retry_count = 1

            # If MAX_RETRIES=1, the user wants 1 total attempt (0 retries).
            # So we only retry if retry_count < settings.max_retries.
            while retry_count < settings.max_retries:
                log.info(
                    f"\nRetrying {test_case.test_id} "
                    f"(attempt {retry_count + 1}/{settings.max_retries})"
                )

                await asyncio.sleep(settings.retry_delay)

                retry_result = await self.test_engine.execute_test(
                    test_case, retry_count=retry_count, browser_queues=browser_queues
                )

                if retry_result.status == TestStatus.PASSED:
                    log.info(f"Retry succeeded for {test_case.test_id}")
                    index = next(
                        i
                        for i, r in enumerate(final_results)
                        if r.test_id == test_case.test_id
                    )
                    final_results[index] = retry_result
                    break
                else:
                    # Stop retrying if this attempt also looks like an auth failure
                    if self._is_auth_failure(retry_result):
                        log.error(
                            f"Auth failure detected on retry for {test_case.test_id} — "
                            "stopping retries to avoid account lockout"
                        )
                        break
                    retry_count += 1
                    if retry_count >= settings.max_retries:
                        log.error(f"All retries exhausted for {test_case.test_id}")

        return final_results

    def _is_auth_failure(self, result: TestResult) -> bool:
        """
        FIX: Detect credential / auth failures so we don't retry them.
        Checks actual_result, error messages, and step errors.
        """
        text_to_check = " ".join(
            filter(
                None,
                [
                    (result.actual_result or "").lower(),
                    " ".join(
                        s.error_message or ""
                        for s in result.step_results
                        if s.error_message
                    ).lower(),
                ],
            )
        )
        return any(pattern in text_to_check for pattern in _AUTH_FAILURE_PATTERNS)

    # -------------------------------------------------------------------------
    # Aggregation
    # -------------------------------------------------------------------------

    def _aggregate_results(
        self,
        test_cases: List[TestCase],
        test_results: List[TestResult],
        start_time: datetime,
        end_time: datetime,
        duration: float,
    ) -> TestSuiteResult:

        passed = sum(1 for r in test_results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in test_results if r.status == TestStatus.FAILED)
        error = sum(1 for r in test_results if r.status == TestStatus.ERROR)
        skipped = sum(1 for r in test_results if r.status == TestStatus.SKIPPED)

        return TestSuiteResult(
            suite_name=f"Test Suite — {start_time.strftime('%Y%m%d_%H%M%S')}",
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration,
            total_tests=len(test_results),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            error_tests=error,
            test_results=test_results,
        )

    # -------------------------------------------------------------------------
    # Summary logging
    # -------------------------------------------------------------------------

    def _log_suite_summary(self, suite_result: TestSuiteResult):
        log.info("\n" + "=" * 80)
        log.info("TEST SUITE SUMMARY")
        log.info("=" * 80)
        log.info(f"Suite    : {suite_result.suite_name}")
        log.info(f"Duration : {suite_result.duration_seconds:.2f}s")
        log.info("-" * 80)
        log.info(f"Total    : {suite_result.total_tests}")
        log.info(f"Passed   : {suite_result.passed_tests}  ({suite_result.pass_rate:.1f}%)")
        log.info(f"Failed   : {suite_result.failed_tests}")
        log.info(f"Errors   : {suite_result.error_tests}")
        log.info(f"Skipped  : {suite_result.skipped_tests}")
        log.info("=" * 80)

        log.info("\nIndividual Results:")
        log.info("-" * 80)

        status_label = {
            TestStatus.PASSED: "PASSED ",
            TestStatus.FAILED: "FAILED ",
            TestStatus.ERROR:  "ERROR  ",
            TestStatus.SKIPPED: "SKIPPED",
        }

        for result in suite_result.test_results:
            label = status_label.get(result.status, "UNKNOWN")
            log.info(
                f"  {label} | {result.test_id:20s} | "
                f"{result.duration_seconds:6.2f}s | "
                f"Steps: {result.passed_steps}/{result.total_steps}"
            )

        log.info("=" * 80 + "\n")
