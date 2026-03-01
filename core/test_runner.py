import asyncio
from typing import List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from models.test_case import TestCase
from models.test_result import TestResult, TestSuiteResult, TestStatus
from core.test_engine import TestEngine
from utils.logger import log
from config.settings import settings
from utils.retry_handler import RetryHandler


class TestRunner:
    """Manages parallel test execution and orchestration"""
    
    def __init__(self):
        self.test_engine = TestEngine()
        self.retry_handler = RetryHandler()
    
    async def run_test_suite(self, test_cases: List[TestCase]) -> TestSuiteResult:
        """Run complete test suite with parallel execution"""
        
        log.info("="*80)
        log.info("🚀 Starting Test Suite Execution")
        log.info("="*80)
        log.info(f"Total Tests: {len(test_cases)}")
        log.info(f"Max Parallel: {settings.max_parallel_tests}")
        log.info(f"Retry Enabled: {settings.retry_failed_tests}")
        log.info("="*80)
        
        start_time = datetime.now()
        
        # Execute tests with parallelization
        if settings.max_parallel_tests > 1:
            test_results = await self._run_parallel(test_cases)
        else:
            test_results = await self._run_sequential(test_cases)
        
        # Retry failed tests if enabled
        if settings.retry_failed_tests:
            test_results = await self._retry_failed_tests(test_cases, test_results)
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        # Aggregate results
        suite_result = self._aggregate_results(
            test_cases,
            test_results,
            start_time,
            end_time,
            duration
        )
        
        # Log final summary
        self._log_suite_summary(suite_result)
        
        return suite_result
    
    async def _run_sequential(self, test_cases: List[TestCase]) -> List[TestResult]:
        """Run tests sequentially"""
        log.info("Running tests sequentially...")
        
        results = []
        for i, test_case in enumerate(test_cases, 1):
            log.info(f"\n{'#'*80}")
            log.info(f"Test {i}/{len(test_cases)}")
            log.info(f"{'#'*80}")
            
            result = await self.test_engine.execute_test(test_case)
            results.append(result)
            
            # Small delay between tests
            await asyncio.sleep(2)
        
        return results
    
    async def _run_parallel(self, test_cases: List[TestCase]) -> List[TestResult]:
        """Run tests in parallel with semaphore"""
        log.info(f"Running tests in parallel (max {settings.max_parallel_tests} concurrent)...")
        
        semaphore = asyncio.Semaphore(settings.max_parallel_tests)
        
        async def run_with_semaphore(test_case: TestCase, index: int):
            async with semaphore:
                log.info(f"\n{'#'*80}")
                log.info(f"Starting Test {index}/{len(test_cases)}: {test_case.test_id}")
                log.info(f"{'#'*80}")
                
                result = await self.test_engine.execute_test(test_case)
                
                log.info(f"Completed Test {index}/{len(test_cases)}: {test_case.test_id}")
                return result
        
        tasks = [
            run_with_semaphore(test_case, i+1) 
            for i, test_case in enumerate(test_cases)
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle any exceptions
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error(f"Test {i+1} raised exception: {result}")
                # Create error result
                test_case = test_cases[i]
                error_result = TestResult(
                    test_id=test_case.test_id,
                    test_name=test_case.test_name,
                    status=TestStatus.ERROR,
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                    duration_seconds=0,
                    total_steps=0,
                    passed_steps=0,
                    failed_steps=0,
                    skipped_steps=0,
                    step_results=[],
                    expected_result=test_case.expected_result,
                    actual_result=f"Exception: {str(result)}",
                    validation_passed=False,
                    error_type=type(result).__name__
                )
                final_results.append(error_result)
            else:
                final_results.append(result)
        
        return final_results
    
    async def _retry_failed_tests(
        self, 
        test_cases: List[TestCase], 
        initial_results: List[TestResult]
    ) -> List[TestResult]:
        """Retry failed tests"""
        
        failed_tests = [
            (test_cases[i], result) 
            for i, result in enumerate(initial_results)
            if result.status in [TestStatus.FAILED, TestStatus.ERROR] 
            and test_cases[i].retry_on_failure
            and not self._is_widget_flow_description(test_cases[i].description)
        ]
        
        if not failed_tests:
            return initial_results
        
        log.warning(f"\n{'='*80}")
        log.warning(f"🔄 Retrying {len(failed_tests)} failed tests")
        log.warning(f"{'='*80}")
        
        final_results = list(initial_results)
        
        for test_case, original_result in failed_tests:
            retry_count = 1
            
            while retry_count <= settings.max_retries:
                log.info(f"\nRetrying {test_case.test_id} (attempt {retry_count}/{settings.max_retries})")
                
                # Wait before retry
                await asyncio.sleep(settings.retry_delay)
                
                # Execute retry
                retry_result = await self.test_engine.execute_test(
                    test_case, 
                    retry_count=retry_count
                )
                
                # Update result if retry succeeded
                if retry_result.status == TestStatus.PASSED:
                    log.success(f"✅ Retry succeeded for {test_case.test_id}")
                    # Replace original result
                    index = next(
                        i for i, r in enumerate(final_results) 
                        if r.test_id == test_case.test_id
                    )
                    final_results[index] = retry_result
                    break
                else:
                    retry_count += 1
                    if retry_count > settings.max_retries:
                        log.error(f"❌ All retries failed for {test_case.test_id}")
        
        return final_results

    def _is_widget_flow_description(self, description: str) -> bool:
        """Detect widget conversational OTP flow where retries are usually redundant."""
        lower_desc = description.lower()
        return (
            "widget" in lower_desc
            or "chat icon" in lower_desc
            or ("ask for" in lower_desc and "otp" in lower_desc and "email" in lower_desc)
            or ("ask for otp" in lower_desc)
        )
    
    def _aggregate_results(
        self,
        test_cases: List[TestCase],
        test_results: List[TestResult],
        start_time: datetime,
        end_time: datetime,
        duration: float
    ) -> TestSuiteResult:
        """Aggregate individual test results into suite result"""
        
        passed = sum(1 for r in test_results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in test_results if r.status == TestStatus.FAILED)
        error = sum(1 for r in test_results if r.status == TestStatus.ERROR)
        skipped = sum(1 for r in test_results if r.status == TestStatus.SKIPPED)
        
        return TestSuiteResult(
            suite_name=f"Test Suite - {start_time.strftime('%Y%m%d_%H%M%S')}",
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration,
            total_tests=len(test_results),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            error_tests=error,
            test_results=test_results
        )
    
    def _log_suite_summary(self, suite_result: TestSuiteResult):
        """Log comprehensive suite summary"""
        
        log.info("\n" + "="*80)
        log.info("📊 TEST SUITE SUMMARY")
        log.info("="*80)
        log.info(f"Suite: {suite_result.suite_name}")
        log.info(f"Duration: {suite_result.duration_seconds:.2f}s")
        log.info(f"-"*80)
        log.info(f"Total Tests: {suite_result.total_tests}")
        log.info(f"✅ Passed: {suite_result.passed_tests} ({suite_result.pass_rate:.1f}%)")
        log.info(f"❌ Failed: {suite_result.failed_tests}")
        log.info(f"💥 Errors: {suite_result.error_tests}")
        log.info(f"⊘ Skipped: {suite_result.skipped_tests}")
        log.info(f"="*80)
        
        # List individual test results
        log.info("\n📋 Individual Test Results:")
        log.info("-"*80)
        
        for result in suite_result.test_results:
            status_emoji = {
                TestStatus.PASSED: "✅",
                TestStatus.FAILED: "❌",
                TestStatus.ERROR: "💥",
                TestStatus.SKIPPED: "⊘"
            }
            
            emoji = status_emoji.get(result.status, "?")
            log.info(
                f"{emoji} {result.test_id:20s} | "
                f"{result.status.upper():8s} | "
                f"{result.duration_seconds:6.2f}s | "
                f"Steps: {result.passed_steps}/{result.total_steps}"
            )
        
        log.info("="*80 + "\n")
