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

    If ``split_cases`` is True the runner will execute each test case in
    isolation, generating a separate report per case. The execution record
    will include all report file names.
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
    # Create async queues for remote browser communication
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
                # run each test case separately
                for idx, case in enumerate(test_cases, start=1):
                    single_suite = await runner.run_test_suite([case], browser_queues=bq)
                    stats = ResultAggregator.get_statistics(single_suite)
                    analysis = ResultAggregator.get_failure_analysis(single_suite)
                    reports = ReporterFactory.generate_all_reports(single_suite, statistics=stats, failure_analysis=analysis)
                    if reports:
                        # just take first report file
                        all_reports[f"case_{case.test_id}"] = str(list(reports.values())[0].name)

                    # update overall counters
                    overall_stats["total"] += stats.get("total", 0)
                    overall_stats["passed"] += stats.get("passed", 0)
                    overall_stats["failed"] += stats.get("failed", 0)
                    overall_stats["duration"] += stats.get("duration", 0)
                    # progress update
                    _mark_execution(execution_id, progress=20 + int(60 * idx / len(test_cases)))
                # compute pass_rate
                overall_stats["pass_rate"] = (
                    (overall_stats["passed"] / overall_stats["total"] * 100)
                    if overall_stats["total"] > 0 else 0
                )
            else:
                # single combined run
                suite_result = await runner.run_test_suite(test_cases, browser_queues=bq)
                stats = ResultAggregator.get_statistics(suite_result)
                analysis = ResultAggregator.get_failure_analysis(suite_result)
                reports = ReporterFactory.generate_all_reports(suite_result, statistics=stats, failure_analysis=analysis)
                if reports:
                    all_reports.update({k: str(v.name) for k, v in reports.items()})
                overall_stats = stats

            # finalize
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

    # Schedule background task
    try:
        asyncio.create_task(_background_run(exec_id, project_id, csv_path, split_cases, browser_queues))
    except Exception:
        # Fallback: run in thread pool
        loop = asyncio.get_event_loop()
        loop.create_task(_background_run(exec_id, project_id, csv_path, split_cases, browser_queues))

    return {"status": "started", "execution_id": exec_id}


@router.websocket("/ws/browser/{execution_id}")
async def browser_ws(ws: WebSocket, execution_id: str):
    """
    WebSocket relay between the backend's RemoteBrowserManager queues and
    the frontend's BrowserBridge → Chrome Extension.

    Flow:
      1. RemoteBrowserManager puts a command on command_queue
      2. This handler reads it and sends it over WebSocket
      3. BrowserBridge forwards it to the Chrome Extension
      4. Extension executes it and sends the result back
      5. BrowserBridge sends the result over WebSocket
      6. This handler puts it on response_queue
      7. RemoteBrowserManager reads the response
    """
    await ws.accept()
    rec = EXECUTIONS.get(execution_id)
    if not rec or "command_queue" not in rec:
        await ws.send_json({"error": "No browser queues for this execution"})
        await ws.close()
        return

    cmd_q: asyncio.Queue = rec["command_queue"]
    resp_q: asyncio.Queue = rec["response_queue"]

    try:
        while True:
            # Wait for the next command from RemoteBrowserManager
            try:
                command = await asyncio.wait_for(cmd_q.get(), timeout=5.0)
            except asyncio.TimeoutError:
                # No command yet — send a keep-alive ping so the browser
                # doesn't close the socket.  BrowserBridge silently absorbs
                # these pings.
                try:
                    await ws.send_json({"action": "ping"})
                except Exception:
                    break
                continue

            # Forward command to the frontend
            await ws.send_json(command)

            # Wait for the extension's result from the frontend
            result = await ws.receive_json()

            # Put the result on the response queue for RemoteBrowserManager
            await resp_q.put(result)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass



@router.get("/executions")
def list_executions():
    """List all rich HTML reports in the test_results directory."""
    if not REPORTS_DIR.exists():
        return []

    reports = []
    # Match test_report_*.html
    for file in REPORTS_DIR.glob("test_report_*.html"):
        mtime = file.stat().st_mtime
        reports.append({
            "filename": file.name,
            "created_at": datetime.fromtimestamp(mtime).isoformat(),
            "size_kb": round(file.stat().st_size / 1024, 2)
        })

    # Sort by newest first
    reports.sort(key=lambda x: x["created_at"], reverse=True)
    return reports


@router.get("/executions/{execution_id}/status")
def get_execution_status(execution_id: str):
    """Return the current status record for a background execution."""
    rec = EXECUTIONS.get(execution_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Execution not found")
    # Return a sanitized copy (strips non-serializable queue objects)
    return _sanitize_execution(rec)


@router.get("/executions/{execution_id}/reports")
def get_execution_reports(execution_id: str):
    rec = EXECUTIONS.get(execution_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Execution not found")
    if rec.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Execution not completed yet")
    reports = rec.get("reports") or {}
    # Convert filenames to accessible URLs
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

        # Attempt to find the latest rich HTML report in test_results that contains this project's tests
        if test_cases and REPORTS_DIR.exists():
            test_ids = {tc.get("test_id") for tc in test_cases if tc.get("test_id")}

            # Get all reports sorted by newest first
            all_reports = sorted(
                REPORTS_DIR.glob("test_report_*.html"),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )

            for report_file in all_reports:
                try:
                    content = report_file.read_text(encoding="utf-8")
                    # Simple heuristic: check if at least one of our test IDs is mentioned in the report
                    if any(tid in content for tid in test_ids):
                        return FileResponse(
                            path=str(report_file),
                            media_type="text/html",
                            filename=report_file.name
                        )
                except Exception:
                    continue

        # Fallback to dynamic summary report

        # Count by priority / category
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
            # Remove existing test cases for this project first
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