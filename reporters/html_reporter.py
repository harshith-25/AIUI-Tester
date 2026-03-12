from pathlib import Path
from datetime import datetime
from typing import Dict, Any
from jinja2 import Environment, FileSystemLoader, select_autoescape
import json
import shutil
import base64

from models.test_result import TestResult, TestSuiteResult, TestStatus
from utils.logger import log
from config.settings import settings

class HTMLReporter:
    """Generate beautiful HTML reports with charts and visualizations"""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or settings.results_dir
        self.output_dir.mkdir(exist_ok=True)
        self.templates_dir = Path(__file__).parent / "templates"
        self.templates_dir.mkdir(exist_ok=True)
        
        # Create template if not exists
        self._ensure_template_exists()
        
        # Setup Jinja2 environment
        self.env = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            autoescape=select_autoescape(['html', 'xml'])
        )
        
        # Add custom filters
        self.env.filters['format_duration'] = self._format_duration
        self.env.filters['status_color'] = self._status_color
        self.env.filters['status_icon'] = self._status_icon
        self.env.filters['asset_path'] = self._asset_path
        self.env.filters['video_mime'] = self._video_mime

    def _report_suffix(self) -> str:
        """Return unique suffix that always ends with DDMMYYYY date."""
        return datetime.now().strftime('%H%M%S_%d%m%Y')
    
    def generate_report(
        self, 
        suite_result: TestSuiteResult,
        statistics: Dict[str, Any] = None,
        failure_analysis: Dict[str, Any] = None
    ) -> Path:
        """Generate comprehensive HTML report"""
        
        suffix = self._report_suffix()
        report_path = self.output_dir / f"test_report_{suffix}.html"
        
        log.info(f"🎨 Generating HTML report: {report_path}")
        
        # Prepare data for template
        serializable_results = [
            test.model_dump(mode='json')
            for test in suite_result.test_results
        ]
        media_assets = self._prepare_media_assets(suite_result, suffix)

        template_data = {
            'suite': suite_result,
            'statistics': statistics or {},
            'failure_analysis': failure_analysis or {},
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'chart_data': self._prepare_chart_data(suite_result),
            'timeline_data': self._prepare_timeline_data(suite_result),
            'test_results_json': serializable_results,
            'test_videos': media_assets["videos"],
            'test_screenshots': media_assets["screenshots"],
            'embedded_test_videos': media_assets["embedded_videos"],
            'embedded_test_screenshots': media_assets["embedded_screenshots"]
        }
        
        # Render template
        template = self.env.get_template('report_template.html')
        html_content = template.render(**template_data)
        
        # Write to file
        report_path.write_text(html_content, encoding='utf-8')
        
        log.success(f"✅ HTML report generated: {report_path}")
        log.info(f"   File size: {report_path.stat().st_size / 1024:.2f} KB")
        log.info(f"   Open in browser: file://{report_path.absolute()}")
        
        return report_path
    
    def _prepare_chart_data(self, suite_result: TestSuiteResult) -> Dict[str, Any]:
        """Prepare data for charts"""
        
        # Status distribution
        status_data = {
            'labels': ['Passed', 'Failed', 'Error', 'Skipped'],
            'data': [
                suite_result.passed_tests,
                suite_result.failed_tests,
                suite_result.error_tests,
                suite_result.skipped_tests
            ],
            'colors': ['#10b981', '#ef4444', '#f59e0b', '#6b7280']
        }
        
        # Duration by test
        duration_data = {
            'labels': [t.test_id for t in suite_result.test_results[:10]],
            'data': [round(t.duration_seconds, 2) for t in suite_result.test_results[:10]]
        }
        
        # Success rate by test
        success_rate_data = {
            'labels': [t.test_id for t in suite_result.test_results],
            'data': [round(t.success_rate, 1) for t in suite_result.test_results]
        }
        
        return {
            'status': status_data,
            'duration': duration_data,
            'success_rate': success_rate_data
        }
    
    def _prepare_timeline_data(self, suite_result: TestSuiteResult) -> list:
        """Prepare timeline data for visualization"""
        
        timeline = []
        for test in suite_result.test_results:
            timeline.append({
                'test_id': test.test_id,
                'test_name': test.test_name,
                'start_time': test.start_time.isoformat(),
                'end_time': test.end_time.isoformat(),
                'duration': test.duration_seconds,
                'status': test.status.value
            })
        
        return timeline
    
    def _format_duration(self, seconds: float) -> str:
        """Format duration for display"""
        if seconds < 60:
            return f"{seconds:.2f}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = seconds % 60
            return f"{minutes}m {secs:.0f}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    def _status_color(self, status: TestStatus) -> str:
        """Get color for status"""
        colors = {
            TestStatus.PASSED: 'success',
            TestStatus.FAILED: 'danger',
            TestStatus.ERROR: 'warning',
            TestStatus.SKIPPED: 'secondary'
        }
        return colors.get(status, 'secondary')
    
    def _status_icon(self, status: TestStatus) -> str:
        """Get icon for status"""
        icons = {
            TestStatus.PASSED: '✅',
            TestStatus.FAILED: '❌',
            TestStatus.ERROR: '💥',
            TestStatus.SKIPPED: '⊘'
        }
        return icons.get(status, '?')

    def _asset_path(self, path_value: str) -> str:
        """Normalize media path for browser rendering."""
        if not path_value:
            return ""
        return str(path_value).replace("\\", "/")

    def _video_mime(self, path_value: str) -> str:
        """Return MIME type based on video extension."""
        suffix = Path(str(path_value)).suffix.lower()
        if suffix == ".mp4":
            return "video/mp4"
        return "video/webm"

    def _discover_test_videos(self, suite_result: TestSuiteResult) -> Dict[str, list]:
        """Discover recorded videos for each test under test_results/<test_id>/videos."""
        videos_by_test: Dict[str, list] = {}

        for test in suite_result.test_results:
            video_dir = self.output_dir / test.test_id / "videos"
            video_files = []

            if video_dir.exists():
                for pattern in ("*.webm", "*.mp4"):
                    for video in video_dir.glob(pattern):
                        try:
                            rel_path = video.relative_to(self.output_dir)
                            video_files.append(str(rel_path).replace("\\", "/"))
                        except Exception:
                            video_files.append(str(video).replace("\\", "/"))

            videos_by_test[test.test_id] = sorted(video_files)

        return videos_by_test

    def _resolve_media_source(self, raw_path: str) -> Path | None:
        """Resolve a media file path from different known roots."""
        if not raw_path:
            return None

        normalized = str(raw_path).replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() and path.exists():
            return path

        candidates = [
            Path.cwd() / normalized,
            self.output_dir / normalized
        ]

        if normalized.startswith("screenshots/"):
            candidates.append(Path.cwd() / normalized)

        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _prepare_media_assets(self, suite_result: TestSuiteResult, timestamp: str) -> Dict[str, Dict[str, list]]:
        """Copy screenshots/videos into a report-local asset folder and return relative web paths."""
        assets_dir = self.output_dir / f"report_assets_{timestamp}"
        shots_dir = assets_dir / "screenshots"
        videos_dir = assets_dir / "videos"
        shots_dir.mkdir(parents=True, exist_ok=True)
        videos_dir.mkdir(parents=True, exist_ok=True)

        screenshots_by_test: Dict[str, list] = {}
        videos_by_test: Dict[str, list] = {}
        embedded_screenshots_by_test: Dict[str, list] = {}
        embedded_videos_by_test: Dict[str, list] = {}

        discovered_videos = self._discover_test_videos(suite_result)

        for test in suite_result.test_results:
            test_shots: list[str] = []
            embedded_shots: list[str] = []
            for shot in test.screenshots:
                src = self._resolve_media_source(shot)
                if not src:
                    continue
                dst_name = f"{test.test_id}_{src.name}"
                dst = shots_dir / dst_name
                shutil.copy2(src, dst)
                rel = dst.relative_to(self.output_dir)
                test_shots.append(str(rel).replace("\\", "/"))
                embedded_shots.append(self._to_data_uri(src, "image/png"))

            test_videos: list[str] = []
            embedded_videos: list[dict] = []
            
            # Use explicit video_path if available or fallback to discovered videos
            all_videos = list(discovered_videos.get(test.test_id, []))
            if getattr(test, "video_path", None):
                all_videos.append(str(test.video_path))

            for video in all_videos:
                src = self._resolve_media_source(video)
                if not src:
                    continue
                dst_name = f"{test.test_id}_{src.name}"
                dst = videos_dir / dst_name
                shutil.copy2(src, dst)
                rel = dst.relative_to(self.output_dir)
                test_videos.append(str(rel).replace("\\", "/"))
                mime = self._video_mime(src.name)
                embedded_videos.append({
                    "src": self._to_data_uri(src, mime),
                    "mime": mime,
                    "name": src.name
                })

            screenshots_by_test[test.test_id] = test_shots
            videos_by_test[test.test_id] = test_videos
            embedded_screenshots_by_test[test.test_id] = embedded_shots
            embedded_videos_by_test[test.test_id] = embedded_videos

        return {
            "screenshots": screenshots_by_test,
            "videos": videos_by_test,
            "embedded_screenshots": embedded_screenshots_by_test,
            "embedded_videos": embedded_videos_by_test
        }

    def _to_data_uri(self, file_path: Path, mime: str) -> str:
        """Encode a file as a data URI to make report fully self-contained."""
        payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{payload}"
    
    def _ensure_template_exists(self):
        """Create HTML template if it doesn't exist"""
        template_path = self.templates_dir / "report_template.html"
        
        if not template_path.exists():
            log.info("Creating HTML report template...")
            template_path.write_text(HTML_TEMPLATE, encoding='utf-8')


# HTML Template with Bootstrap 5 and Chart.js
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Test Report - {{ suite.suite_name }}</title>
    
    <!-- Bootstrap 5 CSS -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    
    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    
    <!-- Font Awesome -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {
            --primary-color: #3b82f6;
            --success-color: #10b981;
            --danger-color: #ef4444;
            --warning-color: #f59e0b;
            --info-color: #06b6d4;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #f8f9fa;
        }
        
        .report-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 2rem;
            border-radius: 10px;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .stat-card {
            background: white;
            border-radius: 10px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .stat-value {
            font-size: 2.5rem;
            font-weight: bold;
            margin: 0;
        }
        
        .stat-label {
            color: #6b7280;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .progress-ring {
            width: 120px;
            height: 120px;
        }
        
        .test-card {
            background: white;
            border-radius: 10px;
            padding: 1.5rem;
            margin-bottom: 1rem;
            border-left: 4px solid #e5e7eb;
            transition: all 0.3s;
        }
        
        .test-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        
        .test-card.passed {
            border-left-color: var(--success-color);
        }
        
        .test-card.failed {
            border-left-color: var(--danger-color);
        }
        
        .test-card.error {
            border-left-color: var(--warning-color);
        }
        
        .badge-custom {
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-weight: 600;
        }
        
        .step-item {
            padding: 0.75rem;
            margin: 0.5rem 0;
            border-radius: 5px;
            background: #f9fafb;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .step-item.passed {
            background: #d1fae5;
            border-left: 3px solid var(--success-color);
        }
        
        .step-item.failed {
            background: #fee2e2;
            border-left: 3px solid var(--danger-color);
        }
        
        .chart-container {
            position: relative;
            height: 300px;
            margin-bottom: 2rem;
        }
        
        .screenshot-thumb {
            max-width: 200px;
            border-radius: 5px;
            cursor: pointer;
            transition: transform 0.2s;
        }
        
        .screenshot-thumb:hover {
            transform: scale(1.05);
        }
        
        .ai-observation {
            background: #eff6ff;
            border-left: 4px solid #3b82f6;
            padding: 1rem;
            margin: 1rem 0;
            border-radius: 5px;
            font-style: italic;
        }
        
        .timeline {
            position: relative;
            padding: 1rem 0;
        }
        
        .timeline-item {
            position: relative;
            padding-left: 2rem;
            padding-bottom: 1rem;
            border-left: 2px solid #e5e7eb;
        }
        
        .timeline-item::before {
            content: '';
            position: absolute;
            left: -6px;
            top: 0;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--primary-color);
        }
        
        .collapsible {
            cursor: pointer;
            user-select: none;
        }
        
        .collapsible:hover {
            background: #f3f4f6;
        }
        
        @media print {
            .no-print {
                display: none;
            }
        }
    </style>
</head>
<body>
    <div class="container-fluid py-4">
        <!-- Header -->
        <div class="report-header">
            <div class="row align-items-center">
                <div class="col-md-8">
                    <h1 class="mb-2">
                        <i class="fas fa-flask"></i> Test Execution Report
                    </h1>
                    <h3 class="mb-0 opacity-75">{{ suite.suite_name }}</h3>
                    <p class="mb-0 mt-2 opacity-75">
                        <i class="far fa-calendar"></i> {{ generated_at }}
                    </p>
                </div>
                <div class="col-md-4 text-end">
                    <div class="btn-group no-print" role="group">
                        <button onclick="window.print()" class="btn btn-light">
                            <i class="fas fa-print"></i> Print
                        </button>
                        <button onclick="exportToJSON()" class="btn btn-light">
                            <i class="fas fa-download"></i> Export JSON
                        </button>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Summary Statistics -->
        <div class="row mb-4">
            <div class="col-md-3">
                <div class="stat-card text-center">
                    <i class="fas fa-vial fa-2x text-primary mb-3"></i>
                    <p class="stat-value text-primary">{{ suite.total_tests }}</p>
                    <p class="stat-label">Total Tests</p>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card text-center">
                    <i class="fas fa-check-circle fa-2x text-success mb-3"></i>
                    <p class="stat-value text-success">{{ suite.passed_tests }}</p>
                    <p class="stat-label">Passed</p>
                    <small class="text-muted">{{ suite.pass_rate|round(1) }}%</small>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card text-center">
                    <i class="fas fa-times-circle fa-2x text-danger mb-3"></i>
                    <p class="stat-value text-danger">{{ suite.failed_tests }}</p>
                    <p class="stat-label">Failed</p>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card text-center">
                    <i class="fas fa-clock fa-2x text-info mb-3"></i>
                    <p class="stat-value text-info">{{ suite.duration_seconds|format_duration }}</p>
                    <p class="stat-label">Duration</p>
                </div>
            </div>
        </div>
        
        <!-- Charts -->
        <div class="row mb-4">
            <div class="col-md-6">
                <div class="stat-card">
                    <h5 class="mb-3"><i class="fas fa-chart-pie"></i> Test Status Distribution</h5>
                    <div class="chart-container">
                        <canvas id="statusChart"></canvas>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="stat-card">
                    <h5 class="mb-3"><i class="fas fa-chart-bar"></i> Test Duration (Top 10)</h5>
                    <div class="chart-container">
                        <canvas id="durationChart"></canvas>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Statistics -->
        {% if statistics %}
        <div class="stat-card mb-4">
            <h5 class="mb-3"><i class="fas fa-chart-line"></i> Execution Statistics</h5>
            <div class="row">
                <div class="col-md-4">
                    <h6>Execution Metrics</h6>
                    <ul class="list-unstyled">
                        <li><strong>Average Duration:</strong> {{ statistics.execution.average_test_duration|format_duration }}</li>
                        <li><strong>Median Duration:</strong> {{ statistics.execution.median_test_duration|format_duration }}</li>
                        <li><strong>Fastest Test:</strong> {{ statistics.execution.min_duration|format_duration }}</li>
                        <li><strong>Slowest Test:</strong> {{ statistics.execution.max_duration|format_duration }}</li>
                    </ul>
                </div>
                <div class="col-md-4">
                    <h6>Step Metrics</h6>
                    <ul class="list-unstyled">
                        <li><strong>Total Steps:</strong> {{ statistics.steps.total_steps }}</li>
                        <li><strong>Avg Steps/Test:</strong> {{ statistics.steps.average_steps_per_test|round(1) }}</li>
                        <li><strong>Max Steps:</strong> {{ statistics.steps.max_steps }}</li>
                    </ul>
                </div>
                <div class="col-md-4">
                    <h6>Retry Information</h6>
                    <ul class="list-unstyled">
                        <li><strong>Tests Retried:</strong> {{ statistics.retries.tests_retried }}</li>
                        <li><strong>Total Retries:</strong> {{ statistics.retries.total_retries }}</li>
                        <li><strong>Avg Success Rate:</strong> {{ statistics.success.average_step_success_rate|round(1) }}%</li>
                    </ul>
                </div>
            </div>
        </div>
        {% endif %}
        
        <!-- Failure Analysis -->
        {% if failure_analysis and failure_analysis.total_failures > 0 %}
        <div class="stat-card mb-4">
            <h5 class="mb-3"><i class="fas fa-exclamation-triangle text-danger"></i> Failure Analysis</h5>
            <div class="row">
                <div class="col-md-6">
                    <h6>Error Types</h6>
                    <ul class="list-unstyled">
                        {% for error_type, count in failure_analysis.error_types.items() %}
                        <li>
                            <span class="badge bg-danger">{{ count }}</span>
                            {{ error_type or 'Unknown' }}
                        </li>
                        {% endfor %}
                    </ul>
                </div>
                <div class="col-md-6">
                    <h6>Common Failure Points</h6>
                    <ul class="list-unstyled">
                        {% for step, count in failure_analysis.common_failure_steps.items() %}
                        <li>
                            <span class="badge bg-warning">{{ count }}</span>
                            {{ step }}
                        </li>
                        {% endfor %}
                    </ul>
                </div>
            </div>
        </div>
        {% endif %}
        
        <!-- Test Results -->
        <div class="stat-card">
            <h5 class="mb-3"><i class="fas fa-list"></i> Test Results Details</h5>
            
            <!-- Filter Buttons -->
            <div class="btn-group mb-3 no-print" role="group">
                <button type="button" class="btn btn-outline-primary active" onclick="filterTests('all')">
                    All ({{ suite.total_tests }})
                </button>
                <button type="button" class="btn btn-outline-success" onclick="filterTests('passed')">
                    Passed ({{ suite.passed_tests }})
                </button>
                <button type="button" class="btn btn-outline-danger" onclick="filterTests('failed')">
                    Failed ({{ suite.failed_tests }})
                </button>
            </div>
            
            <!-- Test Cards -->
            <div id="testResultsContainer">
                {% for test in suite.test_results %}
                <div class="test-card {{ test.status.value }}" data-status="{{ test.status.value }}">
                    <div class="d-flex justify-content-between align-items-start mb-3">
                        <div>
                            <h5 class="mb-1">
                                {{ test.status|status_icon }}
                                {{ test.test_name }}
                            </h5>
                            <small class="text-muted">{{ test.test_id }}</small>
                        </div>
                        <div class="text-end">
                            <span class="badge badge-custom bg-{{ test.status|status_color }}">
                                {{ test.status.value.upper() }}
                            </span>
                            <br>
                            <small class="text-muted">
                                <i class="far fa-clock"></i> {{ test.duration_seconds|format_duration }}
                            </small>
                        </div>
                    </div>
                    
                    <!-- Test Metrics -->
                    <div class="row mb-3">
                        <div class="col-md-3">
                            <small class="text-muted">Steps</small>
                            <div class="progress" style="height: 20px;">
                                <div class="progress-bar bg-success" style="width: {{ (test.passed_steps / test.total_steps * 100)|round if test.total_steps > 0 else 0 }}%">
                                    {{ test.passed_steps }}/{{ test.total_steps }}
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <small class="text-muted">Success Rate</small>
                            <div><strong>{{ test.success_rate|round(1) }}%</strong></div>
                        </div>
                        <div class="col-md-3">
                            <small class="text-muted">Validation</small>
                            <div>
                                {% if test.validation_passed %}
                                <span class="badge bg-success">✓ Passed</span>
                                {% else %}
                                <span class="badge bg-danger">✗ Failed</span>
                                {% endif %}
                            </div>
                        </div>
                        <div class="col-md-3">
                            <small class="text-muted">Retry Count</small>
                            <div><strong>{{ test.retry_count }}</strong></div>
                        </div>
                    </div>
                    
                    <!-- Expected vs Actual -->
                    <div class="mb-3">
                        <div class="row">
                            <div class="col-md-6">
                                <small class="text-muted">Expected Result:</small>
                                <div class="alert alert-info mb-0 py-2">{{ test.expected_result }}</div>
                            </div>
                            <div class="col-md-6">
                                <small class="text-muted">Actual Result:</small>
                                <div class="alert alert-{{ 'success' if test.validation_passed else 'warning' }} mb-0 py-2">
                                    {{ test.actual_result }}
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Steps Details (Collapsible) -->
                    {% if test.step_results %}
                    <div class="collapsible" data-bs-toggle="collapse" data-bs-target="#steps-{{ loop.index }}">
                        <h6 class="mb-2">
                            <i class="fas fa-chevron-down"></i>
                            Step Details ({{ test.step_results|length }})
                        </h6>
                    </div>
                    <div class="collapse" id="steps-{{ loop.index }}">
                        {% for step in test.step_results %}
                        <div class="step-item {{ step.status.value }}">
                            <div>
                                <strong>Step {{ step.step_number }}:</strong>
                                {{ step.action }}
                                {% if step.target %}
                                → <code>{{ step.target }}</code>
                                {% endif %}
                                {% if step.value %}
                                = "{{ step.value }}"
                                {% endif %}
                            </div>
                            <div class="text-end">
                                <span class="badge bg-{{ step.status|status_color }}">
                                    {{ step.status.value }}
                                </span>
                                <small class="text-muted ms-2">{{ step.duration_ms|round }}ms</small>
                            </div>
                        </div>
                        {% if step.error_message %}
                        <div class="alert alert-danger py-2 my-2">
                            <small><strong>Error:</strong> {{ step.error_message }}</small>
                        </div>
                        {% endif %}
                        {% if step.ai_observation %}
                        <div class="ai-observation">
                            <small><i class="fas fa-robot"></i> <strong>AI Agent:</strong> {{ step.ai_observation }}</small>
                        </div>
                        {% endif %}
                        {% endfor %}
                    </div>
                    {% endif %}
                    
                    <!-- AI Observations -->
                    {% if test.ai_observations %}
                    <div class="collapsible mt-3" data-bs-toggle="collapse" data-bs-target="#ai-obs-{{ loop.index }}">
                        <h6 class="mb-2">
                            <i class="fas fa-chevron-down"></i>
                            <i class="fas fa-robot"></i> AI Agent Observations
                        </h6>
                    </div>
                    <div class="collapse" id="ai-obs-{{ loop.index }}">
                        {% for observation in test.ai_observations %}
                        <div class="ai-observation">
                            {{ observation }}
                        </div>
                        {% endfor %}
                    </div>
                    {% endif %}
                    
                    <!-- Screenshots -->
                    {% set screenshots = embedded_test_screenshots.get(test.test_id, []) %}
                    {% if not screenshots %}
                    {% set screenshots = test_screenshots.get(test.test_id, []) %}
                    {% endif %}
                    {% if screenshots %}
                    <div class="mt-3">
                        <h6><i class="fas fa-camera"></i> Screenshots ({{ screenshots|length }})</h6>
                        <div class="d-flex gap-2 flex-wrap">
                            {% for screenshot in screenshots %}
                            {% set shot_path = screenshot|asset_path %}
                            {% if shot_path.startswith('data:') %}
                            <img src="{{ shot_path }}" class="screenshot-thumb" alt="Screenshot" 
                                 onclick='showScreenshot({{ shot_path|tojson }})'>
                            {% else %}
                            <img src="{{ shot_path }}" data-asset-path="{{ shot_path }}" class="screenshot-thumb" alt="Screenshot" 
                                 onclick='showScreenshot(this.dataset.assetPath)'>
                            {% endif %}
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}

                    {% set embedded_videos = embedded_test_videos.get(test.test_id, []) %}
                    {% set videos = test_videos.get(test.test_id, []) %}
                    {% if embedded_videos %}
                    {% set videos = embedded_videos %}
                    {% endif %}
                    {% if videos %}
                    <div class="mt-3">
                        <h6><i class="fas fa-video"></i> Videos ({{ videos|length }})</h6>
                        <div class="d-flex flex-column gap-3">
                            {% for video in videos %}
                            {% if video is mapping %}
                            {% set video_path = video.src %}
                            {% set video_type = video.mime %}
                            {% else %}
                            {% set video_path = video|asset_path %}
                            {% set video_type = video_path|video_mime %}
                            {% endif %}
                            <video controls preload="metadata" style="max-width: 100%; border-radius: 8px;">
                                {% if video_path.startswith('data:') %}
                                <source src="{{ video_path }}" type="{{ video_type }}">
                                {% else %}
                                <source src="{{ video_path }}" data-asset-path="{{ video_path }}" type="{{ video_type }}">
                                {% endif %}
                                Your browser does not support the video tag.
                            </video>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
                    
                    <!-- Timestamps -->
                    <div class="mt-3 text-muted">
                        <small>
                            <i class="far fa-clock"></i>
                            Started: {{ test.start_time.strftime('%Y-%m-%d %H:%M:%S') }} |
                            Ended: {{ test.end_time.strftime('%Y-%m-%d %H:%M:%S') }}
                        </small>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>
    
    <!-- Screenshot Modal -->
    <div class="modal fade" id="screenshotModal" tabindex="-1">
        <div class="modal-dialog modal-xl">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">Screenshot</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body text-center">
                    <img id="modalScreenshot" src="" alt="Screenshot" style="max-width: 100%;">
                </div>
            </div>
        </div>
    </div>
    
    <!-- Bootstrap JS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    
    <script>
        // Status Distribution Chart
        const statusCtx = document.getElementById('statusChart').getContext('2d');
        new Chart(statusCtx, {
            type: 'doughnut',
            data: {
                labels: {{ chart_data.status.labels|tojson }},
                datasets: [{
                    data: {{ chart_data.status.data|tojson }},
                    backgroundColor: {{ chart_data.status.colors|tojson }},
                    borderWidth: 2,
                    borderColor: '#fff'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom'
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                const label = context.label || '';
                                const value = context.parsed || 0;
                                const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                const percentage = ((value / total) * 100).toFixed(1);
                                return `${label}: ${value} (${percentage}%)`;
                            }
                        }
                    }
                }
            }
        });
        
        // Duration Chart
        const durationCtx = document.getElementById('durationChart').getContext('2d');
        new Chart(durationCtx, {
            type: 'bar',
            data: {
                labels: {{ chart_data.duration.labels|tojson }},
                datasets: [{
                    label: 'Duration (seconds)',
                    data: {{ chart_data.duration.data|tojson }},
                    backgroundColor: 'rgba(59, 130, 246, 0.5)',
                    borderColor: 'rgb(59, 130, 246)',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        title: {
                            display: true,
                            text: 'Seconds'
                        }
                    }
                },
                plugins: {
                    legend: {
                        display: false
                    }
                }
            }
        });
        
        // Filter tests
        function filterTests(status) {
            const cards = document.querySelectorAll('.test-card');
            const buttons = document.querySelectorAll('.btn-group button');
            
            // Update active button
            buttons.forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
            
            // Filter cards
            cards.forEach(card => {
                if (status === 'all' || card.dataset.status === status) {
                    card.style.display = 'block';
                } else {
                    card.style.display = 'none';
                }
            });
        }
        
        // Resolve asset path for both:
        // 1) direct report open under /test_results/
        // 2) report opened from project root (/index.html via Live Server)
        function resolveAssetPath(path) {
            if (!path) return path;
            const normalized = String(path).replace(/\\/g, '/');
            if (/^(https?:|file:|data:|\\/)/i.test(normalized)) {
                return normalized;
            }
            const pagePath = window.location.pathname.replace(/\\/g, '/').toLowerCase();
            if (pagePath.includes('/test_results/')) {
                return normalized;
            }
            return `test_results/${normalized}`;
        }

        // Resolve media URLs at runtime based on hosting location.
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('[data-asset-path]').forEach((el) => {
                const resolved = resolveAssetPath(el.dataset.assetPath);
                if (el.tagName === 'SOURCE') {
                    el.src = resolved;
                    const video = el.closest('video');
                    if (video) video.load();
                } else {
                    el.src = resolved;
                }
            });
        });

        // Show screenshot in modal
        function showScreenshot(path) {
            document.getElementById('modalScreenshot').src = resolveAssetPath(path);
            new bootstrap.Modal(document.getElementById('screenshotModal')).show();
        }
        
        // Export to JSON
        function exportToJSON() {
            const data = {
                suite_name: '{{ suite.suite_name }}',
                generated_at: '{{ generated_at }}',
                total_tests: {{ suite.total_tests }},
                passed: {{ suite.passed_tests }},
                failed: {{ suite.failed_tests }},
                pass_rate: {{ suite.pass_rate|round(2) }},
                duration: {{ suite.duration_seconds }},
                results: {{ test_results_json|tojson }}
            };
            
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'test_results_{{ suite.suite_name }}.json';
            a.click();
            URL.revokeObjectURL(url);
        }
    </script>
</body>
</html>
'''
