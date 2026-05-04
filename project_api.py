"""
Project management API — CRUD for projects, prompt-based test generation,
CSV download, and HTML report endpoints.

Mounted as an APIRouter and included by test_case_api.py.

Storage: PostgreSQL via SQLAlchemy (database.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import io
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from fastapi import APIRouter, HTTPException, Form, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from database import SessionLocal, Project, TestCaseDB

# These are imported from the main module at include-time via
# the dependency injection below.
_deps: Dict[str, Any] = {}

def _dep(name: str):
    """Retrieve a dependency registered by the main module."""
    return _deps[name]


def register_dependencies(
    *,
    output_dir: Path,
    slugify_fn,
    write_csv_fn,
    infer_auth_ctx_fn,
    inspect_url_fn,
    generate_llm_fn,
    fallback_generate_fn,
    normalize_cases_fn,
    read_source_fn,
    build_source_rows_fn,
):
    """Called once by test_case_api.py to inject shared helpers."""
    _deps["OUTPUT_DIR"] = output_dir
    _deps["slugify"] = slugify_fn
    _deps["write_csv"] = write_csv_fn
    _deps["infer_auth"] = infer_auth_ctx_fn
    _deps["inspect_url"] = inspect_url_fn
    _deps["generate_llm"] = generate_llm_fn
    _deps["fallback"] = fallback_generate_fn
    _deps["normalize"] = normalize_cases_fn
    _deps["read_source"] = read_source_fn
    _deps["build_rows"] = build_source_rows_fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPORTS_DIR = Path("test_results")


def _get_db():
    """Create and return a new database session."""
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["projects"])

# In-memory execution tracking
EXECUTIONS: dict = {}


def _mark_execution(exec_id: str, **kwargs):
    EXECUTIONS.setdefault(exec_id, {}).update(kwargs)


def _sanitize_execution(rec: dict) -> dict:
    """Return a JSON-safe copy of an execution record (strip queues, etc.)."""
    skip_keys = {"command_queue", "response_queue"}
    return {k: v for k, v in rec.items() if k not in skip_keys}


@router.get("/projects")
def list_projects():
    db = _get_db()
    try:
        projects = db.query(Project).order_by(Project.created_at.desc()).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "test_case_count": len(p.test_cases),
            }
            for p in projects
        ]
    finally:
        db.close()


@router.post("/projects")
def create_project(body: CreateProjectRequest):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Project name is required")
    db = _get_db()
    try:
        pid = str(uuid.uuid4())[:8]
        project = Project(
            id=pid,
            name=body.name.strip(),
            created_at=datetime.now(),
        )
        db.add(project)
        db.commit()
        return {"id": pid, "name": body.name.strip()}
    finally:
        db.close()


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        return proj.to_dict()
    finally:
        db.close()


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        db.delete(proj)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


def _run_csv_in_background(exec_id: str, csv_path: Path):
    """Run main.py -i <csv> -y in a thread and track status in EXECUTIONS."""
    try:
        _mark_execution(exec_id, status="running", progress=10, csv_file=str(csv_path))
        cmd = [sys.executable, "main.py", "-i", str(csv_path), "-y"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,  # 10 min timeout
            cwd=str(Path.cwd()),
        )
        _mark_execution(exec_id, progress=90)

        # Collect reports
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        reports = sorted(
            [f.name for f in REPORTS_DIR.glob("test_report_*.html")],
            key=lambda n: (REPORTS_DIR / n).stat().st_mtime,
            reverse=True,
        )

        if proc.returncode == 0:
            _mark_execution(
                exec_id,
                status="completed",
                progress=100,
                reports=reports,
                finished_at=datetime.now().isoformat(),
            )
        else:
            _mark_execution(
                exec_id,
                status="failed",
                progress=100,
                message=proc.stderr or proc.stdout or "Process exited with non-zero code",
                reports=reports,
                finished_at=datetime.now().isoformat(),
            )
    except subprocess.TimeoutExpired:
        _mark_execution(exec_id, status="failed", progress=100, message="Execution timed out after 10 minutes", finished_at=datetime.now().isoformat())
    except Exception as e:
        _mark_execution(exec_id, status="failed", progress=100, message=str(e), finished_at=datetime.now().isoformat())


@router.post("/run-generated")
def run_generated_cases():
    """Execute the latest CSV in generated_cases/ in the background."""
    dir_path = Path("generated_cases")
    if not dir_path.exists():
        raise HTTPException(status_code=404, detail="generated_cases directory not found")

    csv_files = sorted(dir_path.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not csv_files:
        raise HTTPException(status_code=404, detail="No CSV files found in generated_cases")

    exec_id = uuid.uuid4().hex[:8]
    _mark_execution(exec_id, status="queued", started_at=datetime.now().isoformat(), progress=0)

    thread = threading.Thread(target=_run_csv_in_background, args=(exec_id, csv_files[0]), daemon=True)
    thread.start()

    return {"status": "started", "execution_id": exec_id, "csv_file": str(csv_files[0])}


class RunCsvRequest(BaseModel):
    csv_filename: str


@router.post("/run-csv")
def run_csv(body: RunCsvRequest):
    """Run a specific CSV file from generated_cases/ via main.py in the background."""
    dir_path = Path("generated_cases")
    csv_path = dir_path / body.csv_filename

    if not csv_path.exists():
        # Also try the raw filename as a full path
        csv_path = Path(body.csv_filename)
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail=f"CSV file not found: {body.csv_filename}")

    exec_id = uuid.uuid4().hex[:8]
    _mark_execution(exec_id, status="queued", started_at=datetime.now().isoformat(), progress=0, csv_file=str(csv_path))

    thread = threading.Thread(target=_run_csv_in_background, args=(exec_id, csv_path), daemon=True)
    thread.start()

    return {"status": "started", "execution_id": exec_id, "csv_file": str(csv_path)}

@router.get("/projects/{project_id}/csv")
def download_project_csv(project_id: str):
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")

        test_cases = [tc.to_dict() for tc in proj.test_cases]
        if not test_cases:
            raise HTTPException(status_code=404, detail="No test cases in project")

        safe_name = _dep("slugify")(f"{proj.name}_test_cases")
        out_path = _dep("OUTPUT_DIR") / safe_name
        df = pd.DataFrame(
            test_cases,
            columns=["test_id", "test_name", "description", "expected_result", "priority", "category"],
        )
        _dep("write_csv")(df, out_path)
        return FileResponse(
            path=str(out_path),
            filename=safe_name,
            media_type="text/csv",
        )
    finally:
        db.close()


@router.post("/projects/{project_id}/run")
async def run_project_tests(
    project_id: str,
    csv_path: Optional[str] = Form(default=None),
    split_cases: bool = Form(default=False),
):
    """
    Start a background test run and return an execution id. Poll the execution
    status via `/api/executions/{execution_id}/status`.
    """
    import uuid
    from core.test_runner import TestRunner
    from core.result_aggregator import ResultAggregator
    from reporters import ReporterFactory
    from models.test_case import TestCase
    import pandas as pd

    # Validate project or CSV
    if csv_path:
        p = Path(csv_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")
    else:
        db = _get_db()
        try:
            proj = db.query(Project).filter(Project.id == project_id).first()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            if not proj.test_cases:
                raise HTTPException(status_code=400, detail="No test cases in project to run.")
        finally:
            db.close()

    exec_id = uuid.uuid4().hex[:8]
    command_queue = asyncio.Queue()
    response_queue = asyncio.Queue()
    _mark_execution(
        exec_id,
        status="queued",
        project_id=project_id,
        csv_path=csv_path,
        started_at=datetime.now().isoformat(),
        progress=0,
        command_queue=command_queue,
        response_queue=response_queue,
    )

    browser_queues = {
        "command_queue": command_queue,
        "response_queue": response_queue,
    }

    async def _background_run(
        execution_id: str,
        proj_id: str,
        csv_p: Optional[str],
        split: bool,
        bq: dict,
    ):
        try:
            _mark_execution(execution_id, status="running", progress=5)
            test_cases = []
            if csv_p:
                df = pd.read_csv(Path(csv_p))
                for _, row in df.iterrows():
                    test_cases.append(TestCase(
                        test_id=str(row.get("test_id", "")),
                        test_name=str(row.get("test_name", "")),
                        description=str(row.get("description", "")),
                        expected_result=str(row.get("expected_result", "")),
                        priority=row.get("priority", "Medium"),
                        category=row.get("category", "Functional"),
                    ))
            else:
                db_local = _get_db()
                try:
                    proj_local = db_local.query(Project).filter(Project.id == proj_id).first()
                    if proj_local:
                        for td in proj_local.test_cases:
                            test_cases.append(TestCase(
                                test_id=str(td.test_id),
                                test_name=str(td.test_name),
                                description=str(td.description),
                                expected_result=str(td.expected_result),
                                priority=td.priority or "Medium",
                                category=td.category or "Functional",
                            ))
                finally:
                    db_local.close()

            if not test_cases:
                _mark_execution(execution_id, status="failed", progress=100, message="No test cases found to run")
                return

            runner = TestRunner()
            _mark_execution(execution_id, status="running", progress=20)

            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            all_reports: dict = {}
            overall_stats = {"total": 0, "passed": 0, "failed": 0, "duration": 0, "pass_rate": 0}

            if split and len(test_cases) > 1:
                for idx, case in enumerate(test_cases, start=1):
                    single_suite = await runner.run_test_suite([case], browser_queues=bq)
                    stats = ResultAggregator.get_statistics(single_suite)
                    analysis = ResultAggregator.get_failure_analysis(single_suite)
                    reports = ReporterFactory.generate_all_reports(single_suite, statistics=stats, failure_analysis=analysis)
                    if reports:
                        all_reports[f"case_{case.test_id}"] = str(list(reports.values())[0].name)

                    overall_stats["total"] += stats.get("total", 0)
                    overall_stats["passed"] += stats.get("passed", 0)
                    overall_stats["failed"] += stats.get("failed", 0)
                    overall_stats["duration"] += stats.get("duration", 0)
                    _mark_execution(execution_id, progress=20 + int(60 * idx / len(test_cases)))
                overall_stats["pass_rate"] = (
                    (overall_stats["passed"] / overall_stats["total"] * 100)
                    if overall_stats["total"] > 0 else 0
                )
            else:
                suite_result = await runner.run_test_suite(test_cases, browser_queues=bq)
                stats = ResultAggregator.get_statistics(suite_result)
                analysis = ResultAggregator.get_failure_analysis(suite_result)
                reports = ReporterFactory.generate_all_reports(suite_result, statistics=stats, failure_analysis=analysis)
                if reports:
                    all_reports.update({k: str(v.name) for k, v in reports.items()})
                overall_stats = stats

            _mark_execution(
                execution_id,
                status="completed",
                progress=100,
                results=overall_stats,
                reports=all_reports,
                finished_at=datetime.now().isoformat(),
            )
        except Exception as e:
            _mark_execution(execution_id, status="failed", progress=100, message=str(e), finished_at=datetime.now().isoformat())

    try:
        asyncio.create_task(_background_run(exec_id, project_id, csv_path, split_cases, browser_queues))
    except Exception:
        loop = asyncio.get_event_loop()
        loop.create_task(_background_run(exec_id, project_id, csv_path, split_cases, browser_queues))

    return {"status": "started", "execution_id": exec_id}


@router.websocket("/ws/browser/{execution_id}")
async def browser_ws(ws: WebSocket, execution_id: str):
    await ws.accept()

    rec = EXECUTIONS.get(execution_id)
    if not rec or "command_queue" not in rec:
        await ws.send_json({"error": "No browser queues for this execution"})
        await ws.close()
        return

    cmd_q: asyncio.Queue = rec["command_queue"]
    resp_q: asyncio.Queue = rec["response_queue"]

    print("✅ WS connected:", execution_id)

    try:
        while True:
            # 1️⃣ Send command if exists
            try:
                command = await asyncio.wait_for(cmd_q.get(), timeout=5.0)
                await ws.send_json(command)
            except asyncio.TimeoutError:
                # 2️⃣ Send ping to keep connection alive
                await ws.send_json({"type": "ping"})

            # 3️⃣ Try receiving response (NON-BLOCKING SAFE)
            try:
                result = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                await resp_q.put(result)
            except asyncio.TimeoutError:
                # No response → OK, continue loop
                pass

    except WebSocketDisconnect:
        print("❌ WS disconnected:", execution_id)

    except Exception as e:
        print("❌ WS error:", str(e))

@router.get("/executions")
def list_executions():
    """List all rich HTML reports in the test_results directory."""
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for file in REPORTS_DIR.glob("test_report_*.html"):
        mtime = file.stat().st_mtime
        reports.append({
            "filename": file.name,
            "created_at": datetime.fromtimestamp(mtime).isoformat(),
            "size_kb": round(file.stat().st_size / 1024, 2)
        })

    reports.sort(key=lambda x: x["created_at"], reverse=True)
    return reports


@router.get("/executions/{execution_id}/status")
def get_execution_status(execution_id: str):
    """Return the current status record for a background execution."""
    rec = EXECUTIONS.get(execution_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Execution not found")
    return _sanitize_execution(rec)


@router.get("/executions/{execution_id}/reports")
def get_execution_reports(execution_id: str):
    rec = EXECUTIONS.get(execution_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Execution not found")
    if rec.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Execution not completed yet")
    reports = rec.get("reports") or {}
    urls = {k: str((REPORTS_DIR / v).name) for k, v in reports.items()} if reports else {}
    return {"reports": urls}


@router.get("/executions/{filename}")
def get_execution_report(filename: str):
    """Serve a specific HTML report from test_results."""
    file_path = REPORTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Report file not found")

    return FileResponse(
        path=str(file_path),
        media_type="text/html"
    )

@router.get("/projects/{project_id}/report")
def download_project_report(project_id: str):
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")

        test_cases = [tc.to_dict() for tc in proj.test_cases]
        project_name = proj.name
        created_at = proj.created_at.isoformat() if proj.created_at else ""

        if test_cases and REPORTS_DIR.exists():
            test_ids = {tc.get("test_id") for tc in test_cases if tc.get("test_id")}

            all_reports = sorted(
                REPORTS_DIR.glob("test_report_*.html"),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )

            for report_file in all_reports:
                try:
                    content = report_file.read_text(encoding="utf-8")
                    if any(tid in content for tid in test_ids):
                        return FileResponse(
                            path=str(report_file),
                            media_type="text/html",
                            filename=report_file.name
                        )
                except Exception:
                    continue

        priority_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        for tc in test_cases:
            p = tc.get("priority", "Medium")
            c = tc.get("category", "Functional")
            priority_counts[p] = priority_counts.get(p, 0) + 1
            category_counts[c] = category_counts.get(c, 0) + 1

        priority_bars = ""
        for p, cnt in sorted(priority_counts.items(), key=lambda x: -x[1]):
            color = {"Critical": "#ef4444", "High": "#f59e0b", "Medium": "#6c5ce7", "Low": "#22c55e"}.get(p, "#6c5ce7")
            priority_bars += f'<div class="stat-item"><span class="stat-label">{p}</span><span class="stat-value" style="color:{color}">{cnt}</span></div>\n'

        category_bars = ""
        for c, cnt in sorted(category_counts.items(), key=lambda x: -x[1]):
            category_bars += f'<div class="stat-item"><span class="stat-label">{c}</span><span class="stat-value">{cnt}</span></div>\n'

        tc_rows = ""
        for tc in test_cases:
            p = tc.get("priority", "Medium")
            badge_class = p.lower()
            desc_html = tc.get("description", "").replace("\n", "<br>")
            tc_rows += f"""<tr>
                <td><code>{tc.get('test_id','')}</code></td>
                <td>{tc.get('test_name','')}</td>
                <td class="desc-cell">{desc_html}</td>
                <td>{tc.get('expected_result','')}</td>
                <td><span class="badge {badge_class}">{p}</span></td>
                <td>{tc.get('category','')}</td>
            </tr>
    """

        html = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{project_name} — Test Report</title>
    <style>
      :root {{ --bg: #0f1117; --surface: #1a1d27; --border: #2e3140; --text: #e4e6ed; --muted: #8b8fa3; --accent: #6c5ce7; }}
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 40px; }}
      .container {{ max-width: 1200px; margin: 0 auto; }}
      h1 {{ font-size: 28px; margin-bottom: 4px; background: linear-gradient(135deg, #6c5ce7, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
      .subtitle {{ color: var(--muted); font-size: 14px; margin-bottom: 32px; }}
      .stats {{ display: flex; gap: 32px; margin-bottom: 32px; }}
      .stat-group {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; flex: 1; }}
      .stat-group h3 {{ font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }}
      .stat-item {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); }}
      .stat-item:last-child {{ border-bottom: none; }}
      .stat-label {{ color: var(--muted); }}
      .stat-value {{ font-weight: 700; }}
      table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 12px; overflow: hidden; border: 1px solid var(--border); }}
      th {{ background: #22252f; text-align: left; padding: 14px 16px; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }}
      td {{ padding: 14px 16px; border-top: 1px solid var(--border); font-size: 14px; vertical-align: top; }}
      tr:hover td {{ background: rgba(108,92,231,0.05); }}
      .desc-cell {{ max-width: 400px; font-size: 13px; line-height: 1.5; }}
      code {{ background: var(--border); padding: 2px 8px; border-radius: 4px; font-size: 13px; }}
      .badge {{ padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
      .badge.critical {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
      .badge.high {{ background: rgba(245,158,11,0.15); color: #f59e0b; }}
      .badge.medium {{ background: rgba(108,92,231,0.15); color: #6c5ce7; }}
      .badge.low {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
      .footer {{ margin-top: 32px; text-align: center; color: var(--muted); font-size: 12px; }}
    </style>
    </head>
    <body>
    <div class="container">
      <h1>{project_name} — Test Report</h1>
      <p class="subtitle">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} &middot; {len(test_cases)} test cases &middot; Created {created_at[:10] if created_at else 'N/A'}</p>
      <div class="stats">
        <div class="stat-group">
          <h3>By Priority</h3>
          {priority_bars}
        </div>
        <div class="stat-group">
          <h3>By Category</h3>
          {category_bars}
        </div>
        <div class="stat-group">
          <h3>Summary</h3>
          <div class="stat-item"><span class="stat-label">Total Cases</span><span class="stat-value">{len(test_cases)}</span></div>
        </div>
      </div>
      <table>
        <thead><tr><th>ID</th><th>Test Name</th><th>Description</th><th>Expected Result</th><th>Priority</th><th>Category</th></tr></thead>
        <tbody>
    {tc_rows}
        </tbody>
      </table>
      <p class="footer">AI UI Tester &mdash; Test Report</p>
    </div>
    </body>
    </html>"""
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.delete("/projects/{project_id}/test-cases/{test_id}")
def delete_test_case(project_id: str, test_id: str):
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        tc = db.query(TestCaseDB).filter(
            TestCaseDB.project_id == project_id,
            TestCaseDB.test_id == test_id,
        ).first()
        if not tc:
            raise HTTPException(status_code=404, detail="Test case not found")
        db.delete(tc)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()

LIGHTHOUSE_REPORTS_DIR = Path(__file__).parent / "bulk_lighthouse_reports"


class LighthouseRunRequest(BaseModel):
    url: str


@router.post("/lighthouse/run")
async def run_lighthouse(body: LighthouseRunRequest):
    """
    Start a Lighthouse audit + real browser page-load measurement.
    """
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")

    exec_id = uuid.uuid4().hex[:8]
    report_id = exec_id

    command_queue = asyncio.Queue()
    response_queue = asyncio.Queue()

    _mark_execution(
        exec_id,
        status="queued",
        type="lighthouse",
        url=body.url.strip(),
        started_at=datetime.now().isoformat(),
        progress=0,
        report_id=report_id,
        command_queue=command_queue,
        response_queue=response_queue,
    )

    async def _lighthouse_background(execution_id: str, url: str, rid: str):
        from browser.remote_browser_manager import RemoteBrowserManager

        rec = EXECUTIONS.get(execution_id)
        cmd_q = rec["command_queue"]
        resp_q = rec["response_queue"]

        browser = RemoteBrowserManager(
            test_id=rid,
            command_queue=cmd_q,
            response_queue=resp_q,
        )

        page_load_result = {}

        try:
            _mark_execution(execution_id, status="running", progress=5)

            LIGHTHOUSE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            runner_script = Path(__file__).parent / "lighthouse-runner.mjs"
            loop = asyncio.get_event_loop()

            # ── Step 1 (Optional): Open browser via extension & measure page load ──
            _mark_execution(execution_id, progress=10)
            try:
                await browser.start()
                _mark_execution(execution_id, progress=15)
                await browser.navigate(url)
                await browser.wait(3)

                _mark_execution(execution_id, progress=20)
                try:
                    timings = await browser.evaluate_js("""
                        (() => {
                            const nav = performance.getEntriesByType('navigation')[0];
                            if (nav) {
                                return JSON.stringify({
                                    pageLoadTimeMs: Math.round(nav.loadEventEnd - nav.startTime),
                                    domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd - nav.startTime),
                                    domInteractiveMs: Math.round(nav.domInteractive - nav.startTime),
                                    responseTimeMs: Math.round(nav.responseEnd - nav.requestStart),
                                    resourceCount: performance.getEntriesByType('resource').length
                                });
                            }
                            const t = performance.timing;
                            return JSON.stringify({
                                pageLoadTimeMs: t.loadEventEnd > 0 ? (t.loadEventEnd - t.navigationStart) : null,
                                domContentLoadedMs: t.domContentLoadedEventEnd > 0 ? (t.domContentLoadedEventEnd - t.navigationStart) : null,
                                domInteractiveMs: t.domInteractive > 0 ? (t.domInteractive - t.navigationStart) : null,
                                responseTimeMs: t.responseEnd > 0 ? (t.responseEnd - t.requestStart) : null,
                                resourceCount: performance.getEntriesByType('resource').length
                            });
                        })()
                    """)
                    if timings:
                        if isinstance(timings, str):
                            page_load_result = json.loads(timings)
                        elif isinstance(timings, dict):
                            page_load_result = timings
                except Exception as e:
                    print(f"[Lighthouse] Failed to extract timings from extension browser: {e}")

                try:
                    await browser.close()
                except Exception:
                    pass

            except Exception as ext_err:
                print(f"[Lighthouse] Chrome Extension not connected, skipping real-browser metrics: {ext_err}")
                _mark_execution(execution_id, progress=25)

            page_load_ms = page_load_result.get("pageLoadTimeMs")

            # ── Step 2: Lighthouse audit ───────────────────────────────
            _mark_execution(execution_id, progress=30)

            import shutil
            node_bin = shutil.which("node") or "node"

            cmd = [
                node_bin,
                "--max-old-space-size=1024",
                str(runner_script),
                url,
                str(LIGHTHOUSE_REPORTS_DIR),
                rid,
                "performance",
                "best-practices",
            ]

            import os as _os
            sub_env = _os.environ.copy()
            if "CHROME_PATH" not in sub_env:
                for candidate in [
                    "/usr/bin/google-chrome-stable",
                    "/usr/bin/google-chrome",
                    "/usr/bin/chromium-browser",
                    "/usr/bin/chromium",
                    "/snap/bin/chromium",
                ]:
                    if Path(candidate).exists():
                        sub_env["CHROME_PATH"] = candidate
                        break

            print(f"[Lighthouse] Running: {' '.join(cmd)}")
            print(f"[Lighthouse] CHROME_PATH={sub_env.get('CHROME_PATH', '(not set)')}")
            print(f"[Lighthouse] CWD={Path(__file__).parent}")

            def _run_lighthouse_subprocess():
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(Path(__file__).parent),
                    env=sub_env,
                )
                try:
                    stdout, stderr = process.communicate(timeout=3000)
                    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr}
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    return {"returncode": -1, "stdout": stdout or "", "stderr": stderr or "", "timed_out": True}

            result = await loop.run_in_executor(None, _run_lighthouse_subprocess)

            if result.get("timed_out"):
                err_detail = result.get("stderr", "").strip()[:500]
                _mark_execution(
                    execution_id,
                    status="failed",
                    progress=100,
                    message=f"Lighthouse audit timed out after 300 seconds. Partial stderr: {err_detail}",
                    finished_at=datetime.now().isoformat(),
                )
                return

            _mark_execution(execution_id, progress=85)

            proc_stdout = result["stdout"].strip() if result["stdout"] else ""
            proc_stderr = result["stderr"].strip() if result["stderr"] else ""

            print(f"[Lighthouse] returncode={result['returncode']}")
            if proc_stderr:
                print(f"[Lighthouse] stderr: {proc_stderr[:500]}")

            if result["returncode"] != 0:
                error_msg = proc_stderr or proc_stdout or "Lighthouse process failed"
                try:
                    error_data = json.loads(error_msg)
                    error_msg = error_data.get("message", error_msg)
                except (json.JSONDecodeError, ValueError):
                    pass
                _mark_execution(
                    execution_id,
                    status="failed",
                    progress=100,
                    message=error_msg,
                    finished_at=datetime.now().isoformat(),
                )
                return

            # Parse Lighthouse JSON summary
            try:
                summary = json.loads(proc_stdout)
            except (json.JSONDecodeError, ValueError):
                _mark_execution(
                    execution_id,
                    status="failed",
                    progress=100,
                    message=f"Failed to parse Lighthouse output: {proc_stdout[:300]}",
                    finished_at=datetime.now().isoformat(),
                )
                return

            # ── Step 3: Merge extension browser page-load metrics ──────
            if "metrics" not in summary:
                summary["metrics"] = {}

            if page_load_ms is not None and page_load_ms > 0:
                summary["metrics"]["pageLoadTime"] = {
                    "value": page_load_ms,
                    "displayValue": f"{round(page_load_ms / 1000, 2)} s",
                    "score": None,
                }

            for extra_key in ["domContentLoadedMs", "domInteractiveMs", "resourceCount"]:
                val = page_load_result.get(extra_key)
                if val is not None:
                    summary["metrics"][extra_key] = {
                        "value": val,
                        "displayValue": f"{round(val / 1000, 2)} s" if extra_key != "resourceCount" else str(val),
                        "score": None,
                    }

            # ── Step 4: Resolve failed requests ───────────────────────
            # The runner now deduplicates before writing the CSV, so the
            # in-memory list and the CSV file are always consistent.
            # We trust the runner's list rather than re-reading the CSV.
            failed_requests: list = summary.get("failedRequests", [])
            csv_written: bool = summary.get("failedRequestsCsvWritten", False)

            # Determine the CSV filename only if the runner confirms it wrote one.
            # Store just the filename (not a full path) so all callers can
            # construct the full path themselves using LIGHTHOUSE_REPORTS_DIR.
            failed_csv_filename: Optional[str] = None
            if csv_written:
                candidate = LIGHTHOUSE_REPORTS_DIR / f"{rid}.failed-requests.csv"
                if candidate.exists():
                    failed_csv_filename = candidate.name

            _mark_execution(
                execution_id,
                status="completed",
                progress=100,
                report_id=rid,
                score=summary.get("score"),
                metrics=summary.get("metrics", {}),
                categories=summary.get("categories", {}),
                # Single source of truth: always use the runner's deduplicated list
                failed_requests=failed_requests,
                failed_requests_count=len(failed_requests),
                # CSV metadata — filename only, never a full path
                failed_requests_csv=failed_csv_filename,
                failed_requests_csv_written=csv_written,
                finished_at=datetime.now().isoformat(),
            )
        except Exception as e:
            import traceback
            err_str = traceback.format_exc()
            print("Lighthouse background error:", err_str)
            _mark_execution(
                execution_id,
                status="failed",
                progress=100,
                message=repr(e) + "\n" + err_str,
                finished_at=datetime.now().isoformat(),
            )

    asyncio.create_task(_lighthouse_background(exec_id, body.url.strip(), report_id))
    return {"status": "started", "execution_id": exec_id, "report_id": report_id}


@router.get("/lighthouse/report/{report_id}")
def get_lighthouse_html_report(report_id: str):
    """Serve the Lighthouse HTML report."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)
    file_path = LIGHTHOUSE_REPORTS_DIR / f"{safe_id}.report.html"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Lighthouse report not found")
    return FileResponse(path=str(file_path), media_type="text/html")


@router.get("/lighthouse/json/{report_id}")
def get_lighthouse_json_report(report_id: str):
    """Serve the raw Lighthouse JSON result (LHR)."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)
    file_path = LIGHTHOUSE_REPORTS_DIR / f"{safe_id}.report.json"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Lighthouse JSON report not found")
    return FileResponse(path=str(file_path), media_type="application/json")


@router.get("/lighthouse/failed-requests/{report_id}")
def get_lighthouse_failed_requests(report_id: str):
    """
    Return the list of failed/broken requests for a Lighthouse report as JSON.

    Source priority:
      1. In-memory execution record (always consistent with the CSV, deduplicated
         by the runner before both were written).
      2. On-disk CSV fallback (for reports from a previous server session where
         the in-memory record no longer exists).
    """
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)

    # 1. Try the in-memory record first — it is always consistent with the CSV.
    for rec in EXECUTIONS.values():
        if rec.get("report_id") == safe_id and "failed_requests" in rec:
            requests = rec["failed_requests"]
            return {"failedRequests": requests, "count": len(requests)}

    # 2. Fall back to the CSV on disk (e.g. after a server restart).
    csv_path = LIGHTHOUSE_REPORTS_DIR / f"{safe_id}.failed-requests.csv"
    if not csv_path.exists():
        return {"failedRequests": [], "count": 0}

    try:
        df = pd.read_csv(csv_path)
        # Normalise column names to camelCase for API consistency
        col_map = {
            "URL": "url",
            "Status Code": "statusCode",
            "MIME Type": "mimeType",
            "Error Type": "errorType",
        }
        df.rename(columns=col_map, inplace=True)
        records = df.to_dict(orient="records")
        return {"failedRequests": records, "count": len(records)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse CSV: {str(e)}")


@router.get("/lighthouse/failed-requests/{report_id}/csv")
def download_lighthouse_failed_csv(report_id: str):
    """
    Download the failed-requests CSV for a Lighthouse report.

    If the on-disk CSV is missing but we have an in-memory record, regenerate
    it on the fly so the download always works.
    """
    import csv
    import io as _io

    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", report_id)
    csv_path = LIGHTHOUSE_REPORTS_DIR / f"{safe_id}.failed-requests.csv"

    # If the file exists, serve it directly.
    if csv_path.exists():
        return FileResponse(
            path=str(csv_path),
            media_type="text/csv",
            filename=f"failed_requests_{safe_id}.csv",
        )

    # File missing — try to regenerate from the in-memory execution record.
    failed_requests: Optional[list] = None
    for rec in EXECUTIONS.values():
        if rec.get("report_id") == safe_id and "failed_requests" in rec:
            failed_requests = rec["failed_requests"]
            break

    if failed_requests is None:
        raise HTTPException(
            status_code=404,
            detail="No failed requests CSV found for this report and no in-memory data available."
        )

    if not failed_requests:
        raise HTTPException(status_code=404, detail="No failed requests recorded for this report.")

    # Build CSV in-memory and write to disk so future requests hit the fast path.
    buf = _io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["url", "statusCode", "mimeType", "errorType"],
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(failed_requests)
    csv_content = buf.getvalue()

    # Persist to disk for next time.
    try:
        LIGHTHOUSE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(csv_content, encoding="utf-8")
    except Exception:
        pass  # Non-fatal — we still serve the content below.

    from fastapi.responses import Response
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="failed_requests_{safe_id}.csv"'},
    )


@router.post("/lighthouse/page-load")
async def measure_page_load(url: str = Form(...)):
    """Measure the exact time it takes to fully load all elements on a page."""
    try:
        from playwright.async_api import async_playwright
        import time
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            start_time = time.time()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            end_time = time.time()
            await browser.close()
            return {"status": "ok", "pageLoadTimeMs": int((end_time - start_time) * 1000)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/lighthouse/history")
def get_lighthouse_history():
    """List all past Lighthouse reports."""
    if not LIGHTHOUSE_REPORTS_DIR.exists():
        return []

    reports = []
    for file in LIGHTHOUSE_REPORTS_DIR.glob("*.report.json"):
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)

            url = data.get("finalUrl") or data.get("requestedUrl") or "Unknown URL"
            perf_score = None
            if "categories" in data and "performance" in data["categories"]:
                s = data["categories"]["performance"].get("score")
                if s is not None:
                    perf_score = int(s * 100)

            metrics = {}
            audits = data.get("audits", {})
            metric_map = {
                'first-contentful-paint': 'fcp',
                'largest-contentful-paint': 'lcp',
                'cumulative-layout-shift': 'cls',
                'total-blocking-time': 'tbt',
                'speed-index': 'si',
                'interactive': 'tti',
                'server-response-time': 'ttfb',
            }
            for audit_id, key in metric_map.items():
                if audit_id in audits:
                    audit = audits[audit_id]
                    metrics[key] = {
                        "value": audit.get("numericValue"),
                        "displayValue": audit.get("displayValue", ""),
                        "score": audit.get("score")
                    }

            try:
                metrics_items = audits.get("metrics", {}).get("details", {}).get("items", [])
                if metrics_items:
                    observed_load = metrics_items[0].get("observedLoad")
                    if observed_load is not None:
                        metrics["pageLoadTime"] = {
                            "value": round(observed_load),
                            "displayValue": f"{round(observed_load / 1000, 1)} s",
                            "score": None
                        }
            except Exception:
                pass

            report_id = file.name.replace(".report.json", "")
            timestamp = datetime.fromtimestamp(file.stat().st_mtime).isoformat()

            failed_csv = LIGHTHOUSE_REPORTS_DIR / f"{report_id}.failed-requests.csv"
            failed_count = 0
            has_failed_csv = failed_csv.exists()
            if has_failed_csv:
                try:
                    with open(failed_csv, "r", encoding="utf-8") as fc:
                        # Subtract 1 for header row; guard against empty files
                        failed_count = max(0, sum(1 for _ in fc) - 1)
                except Exception:
                    pass

            # Also check in-memory for a more accurate count (covers the case
            # where the CSV hasn't been written yet but the run just completed).
            for rec in EXECUTIONS.values():
                if rec.get("report_id") == report_id and "failed_requests_count" in rec:
                    failed_count = rec["failed_requests_count"]
                    has_failed_csv = rec.get("failed_requests_csv_written", has_failed_csv)
                    break

            reports.append({
                "url": url,
                "score": perf_score,
                "reportId": report_id,
                "metrics": metrics,
                "timestamp": timestamp,
                "failedRequestsCount": failed_count,
                "hasFailedRequestsCsv": has_failed_csv,
            })
        except Exception:
            continue

    reports.sort(key=lambda x: x["timestamp"], reverse=True)
    return reports


@router.post("/projects/{project_id}/import-test-cases")
def import_test_cases(project_id: str, csv_path: str = Form(...)):
    """Import test cases from a CSV file into the project database."""
    db = _get_db()
    try:
        proj = db.query(Project).filter(Project.id == project_id).first()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")

        p = Path(csv_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")

        try:
            df = pd.read_csv(p)
            db.query(TestCaseDB).filter(TestCaseDB.project_id == project_id).delete()

            count = 0
            for _, row in df.iterrows():
                tc = TestCaseDB(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    test_id=str(row.get("test_id", "")),
                    test_name=str(row.get("test_name", "")),
                    description=str(row.get("description", "")),
                    expected_result=str(row.get("expected_result", "")),
                    priority=str(row.get("priority", "Medium")),
                    category=str(row.get("category", "Functional")),
                )
                db.add(tc)
                count += 1

            db.commit()
            return {
                "status": "ok",
                "imported": count,
                "message": f"Successfully imported {count} test cases"
            }
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"Failed to import test cases: {str(e)}")
    finally:
        db.close()