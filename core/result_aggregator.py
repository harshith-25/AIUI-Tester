from typing import List, Dict, Any
from models.test_result import TestResult, TestSuiteResult, TestStatus
from collections import Counter
import statistics


class ResultAggregator:
    """Aggregate and analyze test results"""
    
    @staticmethod
    def get_statistics(suite_result: TestSuiteResult) -> Dict[str, Any]:
        """Calculate comprehensive statistics"""
        
        durations = [r.duration_seconds for r in suite_result.test_results]
        step_counts = [r.total_steps for r in suite_result.test_results]
        success_rates = [r.success_rate for r in suite_result.test_results]
        
        return {
            "execution": {
                "total_duration_seconds": suite_result.duration_seconds,
                "average_test_duration": statistics.mean(durations) if durations else 0,
                "median_test_duration": statistics.median(durations) if durations else 0,
                "min_duration": min(durations) if durations else 0,
                "max_duration": max(durations) if durations else 0,
            },
            "steps": {
                "total_steps": sum(step_counts),
                "average_steps_per_test": statistics.mean(step_counts) if step_counts else 0,
                "max_steps": max(step_counts) if step_counts else 0,
            },
            "success": {
                "overall_pass_rate": suite_result.pass_rate,
                "average_step_success_rate": statistics.mean(success_rates) if success_rates else 0,
            },
            "retries": {
                "tests_retried": sum(1 for r in suite_result.test_results if r.retry_count > 0),
                "total_retries": sum(r.retry_count for r in suite_result.test_results),
            }
        }
    
    @staticmethod
    def get_failure_analysis(suite_result: TestSuiteResult) -> Dict[str, Any]:
        """Analyze failures and errors"""
        
        failed_tests = [
            r for r in suite_result.test_results 
            if r.status in [TestStatus.FAILED, TestStatus.ERROR]
        ]
        
        if not failed_tests:
            return {"total_failures": 0}
        
        # Analyze error types
        error_types = Counter(r.error_type for r in failed_tests if r.error_type)
        
        # Analyze common failure points
        failure_steps = []
        for test in failed_tests:
            failed_step_actions = [
                s.action for s in test.step_results 
                if s.status == TestStatus.FAILED
            ]
            failure_steps.extend(failed_step_actions)
        
        common_failure_steps = Counter(failure_steps)
        
        return {
            "total_failures": len(failed_tests),
            "error_types": dict(error_types.most_common()),
            "common_failure_steps": dict(common_failure_steps.most_common(5)),
            "failed_test_ids": [t.test_id for t in failed_tests]
        }
    
    @staticmethod
    def get_slowest_tests(suite_result: TestSuiteResult, top_n: int = 5) -> List[Dict[str, Any]]:
        """Get slowest tests"""
        
        sorted_tests = sorted(
            suite_result.test_results,
            key=lambda x: x.duration_seconds,
            reverse=True
        )
        
        return [
            {
                "test_id": t.test_id,
                "test_name": t.test_name,
                "duration_seconds": t.duration_seconds,
                "total_steps": t.total_steps
            }
            for t in sorted_tests[:top_n]
        ]