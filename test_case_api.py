"""
API service to generate runner-compatible UI test case CSV files.

Run:
  uvicorn test_case_api:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


OUTPUT_DIR = Path("generated_cases")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Test Case Generator API", version="2.0.0")

# CORS – allow the frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Browsers reject '*' with credentials; keep API broadly open for dev.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Project management router (code-split into project_api.py)
# ---------------------------------------------------------------------------
from project_api import EXECUTIONS, router as project_router, register_dependencies, _sanitize_execution  # noqa: E402
from database import init_db  # noqa: E402

# Initialise PostgreSQL tables (creates them if they don't exist)
init_db()


def _latest_execution() -> Optional[Dict[str, Any]]:
    if not EXECUTIONS:
        return None
    latest_id, latest_rec = max(
        EXECUTIONS.items(),
        key=lambda item: item[1].get("started_at", ""),
    )
    return {"execution_id": latest_id, **_sanitize_execution(latest_rec)}


@app.get("/status")
@app.get("/api/status")
def get_status(execution_id: Optional[str] = None):
    now_iso = datetime.now().isoformat()
    if execution_id:
        rec = EXECUTIONS.get(execution_id)
        if not rec:
            return {
                "status": "ok",
                "timestamp": now_iso,
                "execution": {
                    "execution_id": execution_id,
                    "status": "not_found",
                    "message": "Execution ID not found",
                },
            }
        return {
            "status": "ok",
            "timestamp": now_iso,
            "execution": {"execution_id": execution_id, **_sanitize_execution(rec)},
        }

    return {
        "status": "ok",
        "timestamp": now_iso,
        "executions_total": len(EXECUTIONS),
        "latest_execution": _latest_execution(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify_filename(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("._")
    if not value:
        value = "generated_test_cases"
    if not value.lower().endswith(".csv"):
        value += ".csv"
    return value


def _google_sheet_to_csv_url(url: str) -> Optional[str]:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        return None
    sheet_id = match.group(1)
    gid_match = re.search(r"[?&]gid=(\d+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _read_source(
    spreadsheet_url: Optional[str],
    input_file: Optional[UploadFile],
    csv_path: Optional[str],
) -> pd.DataFrame:
    provided = sum(bool(x) for x in [spreadsheet_url, input_file, csv_path])
    if provided != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one source: spreadsheet_url, csv_path, or input_file (.csv).",
        )

    if spreadsheet_url:
        csv_url = _google_sheet_to_csv_url(spreadsheet_url) or spreadsheet_url
        try:
            if csv_url.lower().endswith(".xlsx") or "format=xlsx" in csv_url.lower():
                return pd.read_excel(csv_url)
            return pd.read_csv(csv_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to read spreadsheet_url: {exc}") from exc

    if csv_path:
        path = Path(csv_path)
        if not path.exists():
            raise HTTPException(status_code=400, detail=f"csv_path not found: {csv_path}")
        if path.suffix.lower() != ".csv":
            raise HTTPException(status_code=400, detail="csv_path must point to a .csv file.")
        try:
            return pd.read_csv(path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to read csv_path: {exc}") from exc

    assert input_file is not None
    if not input_file.filename or not input_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="input_file must be a .csv file.")
    try:
        raw = input_file.file.read()
        return pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded CSV: {exc}") from exc


def _build_source_rows(df: pd.DataFrame, max_rows: int = 200) -> List[Dict[str, Any]]:
    def _clean_text(value: Any) -> str:
        text = str(value).strip()
        if text.lower() in {"nan", "none", "null"}:
            return ""
        return text

    def _normalize_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", name.lower())

    def _promote_header_if_embedded(source_df: pd.DataFrame) -> pd.DataFrame:
        # Many Google Sheets exports with merged cells arrive as Unnamed columns.
        # If row content looks like headers, promote that row to actual header.
        if not any(str(col).lower().startswith("unnamed:") for col in source_df.columns):
            return source_df

        for idx in range(min(5, len(source_df))):
            row_values = [_clean_text(v) for v in source_df.iloc[idx].tolist()]
            norm = {_normalize_name(v) for v in row_values if v}
            if {"role", "description"}.issubset(norm):
                body = source_df.iloc[idx + 1 :].copy()
                header = [v if v else f"col_{i}" for i, v in enumerate(row_values)]

                # Deduplicate header names while preserving order.
                seen: Dict[str, int] = {}
                final_header: List[str] = []
                for h in header:
                    if h not in seen:
                        seen[h] = 0
                        final_header.append(h)
                    else:
                        seen[h] += 1
                        final_header.append(f"{h}_{seen[h]}")

                body.columns = final_header
                return body

        return source_df

    def _canonical_columns(source_df: pd.DataFrame) -> pd.DataFrame:
        aliases = {
            "sno": "S. No.",
            "serialno": "S. No.",
            "serialnumber": "S. No.",
            "role": "Role",
            "description": "Description",
            "feature": "Feature",
            "title": "Feature",
            "name": "Feature",
        }
        rename_map: Dict[str, str] = {}
        for col in source_df.columns:
            norm = _normalize_name(str(col))
            if norm in aliases:
                rename_map[col] = aliases[norm]
        return source_df.rename(columns=rename_map)

    def _is_planning_row(role: str, description: str) -> bool:
        if role:
            return False
        d = description.lower()
        planning_keywords = [
            "design",
            "analysis",
            "setup",
            "database migration",
            "updating code",
            "starting testing",
            "backend/frontend setup",
        ]
        return any(keyword in d for keyword in planning_keywords)

    normalized_df = _promote_header_if_embedded(df)
    normalized_df = _canonical_columns(normalized_df)
    cleaned = normalized_df.fillna("").astype(str)
    rows: List[Dict[str, Any]] = []
    last_role = ""
    for _, row in cleaned.iterrows():
        raw_item = {str(col): _clean_text(val) for col, val in row.to_dict().items()}
        item = {k: v for k, v in raw_item.items() if v}
        if item:
            role_val = item.get("Role", "")
            if role_val in {"”", '"', "''", "ditto", "same"}:
                role_val = last_role
            if role_val:
                last_role = role_val
                item["Role"] = role_val

            description_val = item.get("Description", "") or item.get("Feature", "")
            if _is_planning_row(role=item.get("Role", ""), description=description_val):
                continue

            rows.append(item)
            if len(rows) >= max_rows:
                break
    return rows


def _inspect_target_url(target_url: str, timeout_seconds: int = 8) -> Dict[str, Any]:
    """Best-effort URL probe so generation can use live page context."""
    result: Dict[str, Any] = {
        "url": target_url,
        "reachable": False,
        "status_code": None,
        "final_url": target_url,
        "title": None,
        "error": None,
    }
    try:
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UITestGenerator/2.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read(200_000).decode("utf-8", errors="ignore")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
            title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None
            result.update(
                {
                    "reachable": True,
                    "status_code": int(getattr(resp, "status", 200)),
                    "final_url": str(getattr(resp, "url", target_url)),
                    "title": title,
                }
            )
    except urllib.error.HTTPError as exc:
        result.update({"status_code": int(exc.code), "error": f"HTTP {exc.code}"})
    except Exception as exc:
        result.update({"error": str(exc)})
    return result


def _extract_credential_like_value(text: str, label: str) -> Optional[str]:
    patterns = [
        rf"{label}\s*(?:as|is|:)\s*['\"]?([^,\n;\"']+)['\"]?",
        rf"{label}[^\n]*?give\s+(?:it\s+)?as\s*['\"]?([^,\n;\"']+)['\"]?",
    ]
    match = None
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            break
    if not match:
        return None
    value = match.group(1).strip()
    return value if value else None


def _infer_auth_context(
    rows: List[Dict[str, Any]],
    username: Optional[str],
    password: Optional[str],
) -> Dict[str, Optional[str]]:
    """Infer whether flow is username/password or widget name/email/otp."""
    descriptions: List[str] = []
    for row in rows:
        for key in ("Description", "description", "Feature", "feature", "Name", "name", "title"):
            val = str(row.get(key, "")).strip()
            if val:
                descriptions.append(val)
    blob = "\n".join(descriptions)
    blob_l = blob.lower()

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    otp_match = re.search(
        r"\botp\b(?:[^\n]*?(?:as|is|:|give\s+(?:it\s+)?as))?\s*['\"]?(\d{4,8})['\"]?",
        blob,
        flags=re.IGNORECASE,
    )

    extracted_username = _extract_credential_like_value(blob, "username")
    extracted_password = _extract_credential_like_value(blob, "password")
    extracted_name = _extract_credential_like_value(blob, "name")

    def _as_text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    auth_username = (_as_text(username) or extracted_username or "").strip() or None
    auth_password = (_as_text(password) or extracted_password or "").strip() or None
    auth_name = (extracted_name or "").strip() or None
    auth_email = email_match.group(0).strip() if email_match else None
    auth_otp = otp_match.group(1).strip() if otp_match else None

    has_widget_auth = ("otp" in blob_l) or ("email" in blob_l and "name" in blob_l)
    mode = "name_email_otp" if has_widget_auth else "username_password"
    if not has_widget_auth and not auth_username and not auth_password:
        mode = "none"

    return {
        "mode": mode,
        "name": auth_name,
        "email": auth_email,
        "otp": auth_otp,
        "username": auth_username,
        "password": auth_password,
    }


def _build_auth_steps(auth_context: Dict[str, Optional[str]]) -> List[str]:
    mode = auth_context.get("mode")
    if mode == "name_email_otp":
        name_val = auth_context.get("name") or "<enter_name>"
        email_val = auth_context.get("email") or "<enter_email>"
        otp_val = auth_context.get("otp") or "<enter_otp>"
        return [
            f"It will first ask for the name, give it as {name_val}.",
            f"Later it will ask for email, give {email_val}.",
            f"Then it will ask for OTP, give it as {otp_val}, submit, and wait for response.",
            "Confirm the widget shows successful verification or login-complete style confirmation text.",
        ]
    if mode == "username_password":
        user_val = auth_context.get("username") or "<enter_username>"
        pass_val = auth_context.get("password") or "<enter_password>"
        return [
            f"Enter username as {user_val}.",
            f"Enter password as {pass_val}.",
            "Click the Login button and wait for the dashboard to load.",
        ]
    return ["Complete the app authentication flow and wait for post-login state."]


# ---------------------------------------------------------------------------
# The core prompt — designed to produce the same quality as a senior QA engineer
# ---------------------------------------------------------------------------

def _build_llm_prompt(
    rows: List[Dict[str, Any]],
    target_url: str,
    auth_context: Dict[str, Optional[str]],
    url_probe: Dict[str, Any],
    role_filter: Optional[str],
    max_cases: int,
    user_prompt: Optional[str],
) -> str:
    source_blob = json.dumps(rows, ensure_ascii=True, indent=2)

    role_instruction = (
        f'IMPORTANT: Only generate test cases for rows where the role column value is "{role_filter}". '
        f"Ignore all other roles entirely.\n"
        if role_filter
        else "Generate test cases for ALL rows in the source data.\n"
    )
    user_instruction = (
        f"\nAdditional user instruction (highest priority after schema constraints):\n{user_prompt.strip()}\n"
        if user_prompt and user_prompt.strip()
        else ""
    )

    auth_mode = str(auth_context.get("mode") or "none")
    auth_steps = _build_auth_steps(auth_context)
    auth_instruction = "\n".join(f"  {chr(97 + i)}. {step}" for i, step in enumerate(auth_steps))
    page_hint = (
        f"URL probe: reachable={url_probe.get('reachable')}, "
        f"status_code={url_probe.get('status_code')}, "
        f"final_url={url_probe.get('final_url')}, "
        f"title={url_probe.get('title')}"
    )

    return f"""You are a senior QA engineer writing a detailed manual test case suite.

Your output must be a JSON object with exactly this shape:
{{"test_cases": [ ... ]}}

Each test case object must have these exact keys:
  test_id, test_name, description, expected_result, priority, category

=== STRICT RULES FOR EVERY FIELD ===

test_id:
  Format: TC-001, TC-002, TC-003 ... (sequential, zero-padded to 3 digits)

test_name:
  Short, specific title. Example: "Super Admin - Delete Institution"
  Must reflect the exact feature/action being tested.

description:
  THIS IS THE MOST IMPORTANT FIELD. Follow this format exactly:

  "Navigate to {target_url}.
{auth_instruction}
  d. [Next actionable step specific to this feature — e.g. Navigate to the Institutes section from the main menu.]
  e. [Next step...]
  f. [Next step...]
  ... continue with as many lettered steps as needed ..."

  Rules for description steps:
  - Every step must be a concrete, actionable UI instruction.
  - Use real verbs: Navigate, Click, Enter, Select, Wait, Verify, Confirm, Scroll, Hover, Upload, Toggle, Submit.
  - Mention specific UI elements: button names, section names, dialog boxes, confirmation prompts, icons, tabs, dropdowns, form fields.
  - For add/edit flows: include filling in form fields, clicking Save/Submit, verifying success message, and reloading to confirm persistence.
  - For delete flows: include selecting item, clicking Delete, confirming the dialog, and verifying item is removed.
  - For list/view flows: include navigating to section, waiting for load, verifying column headers and data.
  - For toggle/config flows: include changing the setting, saving, verifying the state change, optionally reverting.
  - For audit/log flows: include navigating to sub-section, verifying log columns (user, timestamp, action), and testing filters.
  - For password/auth flows: include current password entry, new password entry, confirmation, and login verification with new credentials.
  - Never invent placeholders like <valid_username> or <valid_password>. Use exact values from source rows when present.
  - Auth mode for this request is: {auth_mode}. Respect it for all test cases.
  - NEVER write generic steps like "Perform required input actions" or "Open the relevant module".
  - Minimum 6 steps, maximum 12 steps per test case.

expected_result:
  A clear, specific success statement. What does success look like for THIS test case?
  Must mention the specific feature outcome (e.g. "The institution is removed from the list and a success toast is displayed.")
  NEVER write generic outcomes like "Expected UI behavior is correct."

priority:
  One of: Critical, High, Medium, Low
  Use Critical for login, auth, data deletion. High for CRUD operations. Medium for view/list. Low for cosmetic/minor.

category:
  One of: Smoke Test, Regression, Functional, Functionality Test, Integration, End-to-End, Security

=== QUALITY REFERENCE — match this exact style ===

Example test case for "Delete Institution":
{{
  "test_id": "TC-005",
  "test_name": "Super Admin - Delete Institution",
  "description": "Navigate to {target_url}.\\na. Enter username as <example_username>.\\nb. Enter password as <example_password>.\\nc. Click the Login button and wait for the dashboard to load.\\nd. Navigate to the Institutes section from the main menu.\\ne. Select an existing institution from the list.\\nf. Click the Delete button or icon for that institution.\\ng. Wait for the confirmation dialog to appear and click Confirm or Yes.\\nh. Verify the institution is removed from the list.\\ni. Confirm a success toast or message is shown.",
  "expected_result": "Super Admin can delete an institution; the institution no longer appears in the institutes list and a success confirmation message is displayed.",
  "priority": "High",
  "category": "Functionality Test"
}}

Example test case for "Audit Trails Login Sessions":
{{
  "test_id": "TC-015",
  "test_name": "Super Admin - Audit Trails Login Sessions",
  "description": "Navigate to {target_url}.\\na. Enter username as <example_username>.\\nb. Enter password as <example_password>.\\nc. Click the Login button and wait for the dashboard to load.\\nd. Navigate to Audit Trails from the admin menu.\\ne. Click on the Login Sessions sub-section.\\nf. Wait for the session logs to load.\\ng. Verify the list shows user names, timestamps, IP addresses, and session statuses.\\nh. Apply a date filter and verify the results update to show only entries within that range.\\ni. Apply a user filter and verify only that user's sessions are shown.",
  "expected_result": "Super Admin can view all login session audit logs with accurate user, timestamp, and IP details; date and user filters work correctly and return relevant results.",
  "priority": "High",
  "category": "Security"
}}

Live page context:
{page_hint}

=== SOURCE DATA ===

{role_instruction}
Generate up to {max_cases} test cases. One test case per distinct feature/row. Do not duplicate.

Source rows:
{source_blob}
{user_instruction}

Return ONLY the JSON object. No markdown, no explanation, no code fences.
"""


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

def _create_openai_client() -> tuple[OpenAI, str]:
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return OpenAI(api_key=openai_key), os.getenv("OPENAI_MODEL", "gpt-4o")

    github_token = os.getenv("GITHUB_TOKEN") or os.getenv("github_token")
    if github_token:
        return (
            OpenAI(base_url="https://models.inference.ai.azure.com", api_key=github_token),
            os.getenv("OPENAI_MODEL", "gpt-4o"),
        )

    raise RuntimeError("No API key found. Set OPENAI_API_KEY or GITHUB_TOKEN.")


def _generate_with_llm(
    rows: List[Dict[str, Any]],
    target_url: str,
    auth_context: Dict[str, Optional[str]],
    url_probe: Dict[str, Any],
    role_filter: Optional[str],
    max_cases: int,
    user_prompt: Optional[str],
) -> List[Dict[str, str]]:
    client, model = _create_openai_client()
    prompt = _build_llm_prompt(
        rows=rows,
        target_url=target_url,
        auth_context=auth_context,
        url_probe=url_probe,
        role_filter=role_filter,
        max_cases=max_cases,
        user_prompt=user_prompt,
    )

    system_base = (
        "You are a senior QA engineer. You output ONLY valid JSON. "
        "No markdown, no prose, no code fences. "
        "Every test case description must have specific, actionable, numbered UI steps - "
        "never generic placeholders."
    )
    if user_prompt and user_prompt.strip():
        system_base += (
            " Follow this additional user guidance exactly where it does not conflict with required JSON schema: "
            f"{user_prompt.strip()}"
        )

    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,  # low temperature = consistent, structured output
        max_tokens=16000,
        messages=[
            {
                "role": "system",
                "content": system_base,
            },
            {"role": "user", "content": prompt},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()

    # Strip accidental markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find the JSON object
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise ValueError(f"Model returned non-JSON output: {text[:300]}")
        payload = json.loads(m.group(0))

    cases = payload.get("test_cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError("Model returned empty or invalid test_cases list.")
    return cases


# ---------------------------------------------------------------------------
# Fallback: context-aware test case generation (no LLM)
# ---------------------------------------------------------------------------

# Generic fallback templates; app-specific steps should come from spreadsheet descriptions.
_STEP_TEMPLATES: Dict[str, List[str]] = {
    "list": [
        "Open the target module for {feature}.",
        "Wait until the data/list view is fully loaded.",
        "Verify key columns/fields are visible and populated.",
        "Apply a search or filter and verify results update correctly.",
        "Clear filters and confirm the full list returns.",
    ],
    "add": [
        "Open the create/add flow for {feature}.",
        "Fill required fields with valid data.",
        "Submit the form.",
        "Verify success confirmation is visible.",
        "Confirm the new record appears in the list/view.",
    ],
    "edit": [
        "Open an existing record in the {feature} flow.",
        "Modify one or more editable fields.",
        "Save/submit the changes.",
        "Verify success confirmation is visible.",
        "Reload and confirm updated values persist.",
    ],
    "delete": [
        "Open an existing record in the {feature} flow.",
        "Trigger the delete/remove action.",
        "Confirm the delete dialog or prompt.",
        "Verify the record is no longer visible.",
        "Verify success confirmation is visible.",
    ],
    "auth": [
        "Complete the authentication flow for {feature}.",
        "Verify login/verification success state appears.",
        "Confirm post-login navigation is available.",
    ],
    "config": [
        "Open configuration/settings for {feature}.",
        "Change one setting value.",
        "Save/apply changes.",
        "Verify success confirmation is visible.",
        "Reload and confirm the change persists.",
    ],
    "generic": [
        "Open the feature/screen for {feature}.",
        "Perform the described user actions.",
        "Verify expected UI changes occur without errors.",
        "Refresh/reopen and confirm state is consistent.",
    ],
}

_PRIORITY_MAP = {
    "auth": "Critical",
    "delete": "High",
    "password": "High",
    "audit": "High",
    "config": "High",
    "toggle": "High",
    "add": "High",
    "edit": "High",
    "list": "High",
    "calendar": "Medium",
    "profile": "Medium",
    "workspace": "High",
    "mapping": "High",
    "document_validation": "High",
    "generic": "Medium",
}

_CATEGORY_MAP = {
    "auth": "Smoke Test",
    "delete": "Functional",
    "password": "Security",
    "audit": "Security",
    "config": "Functional",
    "toggle": "Functional",
    "add": "Functional",
    "edit": "Functional",
    "list": "Functional",
    "calendar": "Functional",
    "profile": "Functional",
    "workspace": "Smoke Test",
    "mapping": "Integration",
    "document_validation": "Smoke Test",
    "generic": "Functional",
}


def _detect_template_key(feature: str) -> str:
    fl = feature.lower()
    if any(k in fl for k in ["login", "log in", "sign in", "otp", "authentication", "verify"]):
        return "auth"
    if any(k in fl for k in ["validate content", "documents", "document checking", "folder icon"]):
        return "document_validation"
    if any(k in fl for k in ["audit", "log", "trail", "session log", "study log"]):
        return "audit"
    if any(k in fl for k in ["password", "change pass"]):
        return "password"
    if any(k in fl for k in ["delete", "trash", "remove"]):
        return "delete"
    if any(k in fl for k in ["add", "create", "new"]):
        return "add"
    if any(k in fl for k in ["edit", "update", "modify"]):
        return "edit"
    if any(k in fl for k in ["config", "configuration", "setting", "upload config", "dicom", "compression", "master pass"]):
        return "config"
    if any(k in fl for k in ["toggle", "turn on", "turn off", "enable", "disable", "feature"]):
        return "toggle"
    if any(k in fl for k in ["calendar", "meeting", "schedule"]):
        return "calendar"
    if any(k in fl for k in ["profile", "update user", "my account"]):
        return "profile"
    if any(k in fl for k in ["workspace", "scan", "study", "export"]):
        return "workspace"
    if any(k in fl for k in ["mapping", "map service", "service map"]):
        return "mapping"
    if any(k in fl for k in ["list", "show", "view", "display"]):
        return "list"
    return "generic"


def _normalize_role_name(role: str) -> str:
    role_l = role.lower().strip()
    if "super admin" in role_l:
        return "Super Admin"
    if "hospital admin" in role_l:
        return "Hospital Admin"
    if "group admin" in role_l:
        return "Group Admin"
    if "cardiologist" in role_l:
        return "Cardiologist"
    if "technician" in role_l or "physician" in role_l:
        return "Technician/Physician"
    return role.strip().title()


def _clean_feature_title(feature: str) -> str:
    value = re.sub(r"\s+", " ", feature.replace("\n", " ").replace(">", " ")).strip(" -:")
    replacements = {
        "Calender": "Calendar",
        "Logins Session": "Login Sessions",
        "Turn on /off": "Toggle",
        "My Doctors Calender": "My Doctors Calendar",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _expand_feature_variants(feature: str) -> List[str]:
    fl = feature.lower()
    if "add/edit" in fl and "signature" not in fl:
        add_variant = re.sub(r"(?i)add\s*/\s*edit", "Add", feature)
        edit_variant = re.sub(r"(?i)add\s*/\s*edit", "Edit", feature)
        return [_clean_feature_title(add_variant), _clean_feature_title(edit_variant)]
    return [_clean_feature_title(feature)]


def _build_fallback_description(
    feature: str,
    target_url: str,
    auth_context: Dict[str, Optional[str]],
    template_key: str,
    source_description: Optional[str] = None,
) -> str:
    steps = _STEP_TEMPLATES.get(template_key, _STEP_TEMPLATES["generic"])
    lines = [f"Navigate to {target_url}."]
    letter = ord("a")

    auth_steps = _build_auth_steps(auth_context)
    for s in auth_steps:
        lines.append(f"{chr(letter)}. {s}")
        letter += 1

    source_steps: List[str] = []
    if source_description:
        raw = str(source_description).replace("\r", "\n")
        chunks = [c.strip() for c in re.split(r"[\n;]+", raw) if c.strip()]
        if len(chunks) <= 2:
            chunks = [c.strip() for c in re.split(r"\.\s+", raw) if c.strip()]
        for chunk in chunks:
            c = re.sub(r"^[a-zA-Z0-9]+[.)]\s*", "", chunk).strip(" .")
            if not c:
                continue
            cl = c.lower()
            if cl.startswith("navigate to"):
                continue
            if any(
                k in cl
                for k in [
                    "enter username as",
                    "enter password as",
                    "login button",
                    "ask for the name",
                    "ask for email",
                    "ask for otp",
                    "successful verification",
                    "login-complete style confirmation",
                ]
            ):
                continue
            source_steps.append(c + ("" if c.endswith(".") else "."))

    steps_to_use = source_steps if source_steps else steps
    for step in steps_to_use:
        # Protect against literal braces in spreadsheet text (e.g. JSON snippets).
        try:
            filled = step.format(feature=feature, target_url=target_url)
        except (KeyError, ValueError):
            filled = step.replace("{feature}", feature).replace("{target_url}", target_url)
        lines.append(f"{chr(letter)}. {filled}")
        letter += 1

    return "\n".join(lines)


def _build_fallback_expected(
    feature: str,
    template_key: str,
    source_description: Optional[str] = None,
) -> str:
    if source_description and source_description.strip():
        return (
            f"The '{feature}' flow completes successfully, and the expected confirmations/results "
            "described in the source spreadsheet are visible without errors."
        )
    if template_key == "auth":
        return "Authentication/verification completes successfully, and the user reaches the expected signed-in state."
    if template_key == "delete":
        return f"The '{feature}' target item is removed successfully with visible confirmation and no unexpected errors."
    if template_key in {"add", "edit", "config"}:
        return f"The '{feature}' changes are saved successfully and remain visible after refresh/reopen."
    return f"The '{feature}' scenario executes successfully and all validations pass."


def _infer_entity_name(feature: str) -> str:
    value = _clean_feature_title(feature)
    value = re.sub(r"(?i)^show\s+list\s+of\s+all\s+", "", value).strip()
    value = re.sub(r"(?i)^show\s+list\s+of\s+", "", value).strip()
    value = re.sub(r"(?i)^list\s+of\s+", "", value).strip()
    value = re.sub(r"(?i)^list\s+all\s+", "", value).strip()
    value = re.sub(r"(?i)^add\s+", "", value).strip()
    value = re.sub(r"(?i)^edit\s+", "", value).strip()
    value = re.sub(r"(?i)^delete\s+", "", value).strip()
    value = re.sub(r"(?i)^manage\s+", "", value).strip()
    return value or feature


def _fallback_generate(
    rows: List[Dict[str, Any]],
    target_url: str,
    auth_context: Dict[str, Optional[str]],
    role_filter: Optional[str],
    max_cases: int,
) -> List[Dict[str, str]]:
    """
    Context-aware fallback that generates specific test cases from source row data.
    Much better than generic placeholders — uses feature-specific step templates.
    """
    cases: List[Dict[str, str]] = []
    counter = 1
    has_role_column = any((row.get("Role", "") or "").strip() for row in rows)

    for row in rows:
        if counter > max_cases:
            break

        # Try to extract a meaningful feature name from the row
        role = (row.get("Role", "") or "").strip()
        if role in {"”", '"', "''", "ditto", "same"}:
            role = ""

        if has_role_column and not role:
            continue

        feature = ""
        source_description = ""
        for key in ["Description", "description", "Feature", "feature", "Name", "name", "title"]:
            val = row.get(key, "").strip()
            if val and len(val) > 2:
                feature = val
                source_description = val
                break

        # If role_filter is set, skip rows that don't match
        if role_filter:
            if role_filter.lower() not in role.lower():
                continue

        role_name = _normalize_role_name(role) if role else ""

        if not feature:
            feature = f"Feature {counter}"
        else:
            feature = re.sub(r"\s+", " ", feature.replace("\n", " ")).strip()
            feature = feature[:90].strip()
            if "," in feature:
                feature = feature.split(",", 1)[0].strip()
            if "." in feature:
                feature = feature.split(".", 1)[0].strip()
            if feature.lower().startswith("navigate to"):
                feature = f"{role_name} Scenario".strip() if role_name else f"Scenario {counter}"
            if len(feature) < 3:
                feature = f"Feature {counter}"
        for feature_variant in _expand_feature_variants(feature):
            template_key = _detect_template_key(feature_variant)
            description = _build_fallback_description(
                feature=feature_variant,
                target_url=target_url,
                auth_context=auth_context,
                template_key=template_key,
                source_description=source_description,
            )
            test_name = f"{role_name} - {feature_variant}" if role_name else feature_variant
            category = _CATEGORY_MAP.get(template_key, "Functional")
            if "master password" in feature_variant.lower():
                category = "Security"
            if template_key in {"calendar", "profile"}:
                priority = "Medium"
            else:
                priority = _PRIORITY_MAP.get(template_key, "High")

            cases.append(
                {
                    "test_id": f"TC-{counter:03d}",
                    "test_name": test_name,
                    "description": description,
                    "expected_result": _build_fallback_expected(
                        feature_variant,
                        template_key,
                        source_description=source_description,
                    ),
                    "priority": priority,
                    "category": category,
                }
            )
            counter += 1
            if counter > max_cases:
                break

    if not cases:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No rows matched role_filter='{role_filter}'. "
                "Check that the Role column in your source data matches exactly."
            ),
        )

    return cases


# ---------------------------------------------------------------------------
# Normalise & write output
# ---------------------------------------------------------------------------

def _normalize_cases(
    cases: List[Dict[str, Any]],
    target_url: str,
    auth_context: Optional[Dict[str, Optional[str]]] = None,
) -> pd.DataFrame:
    def _format_lettered_steps(description_text: str) -> str:
        lines = [line.strip() for line in description_text.splitlines() if line.strip()]
        if not lines:
            return f"Navigate to {target_url}."
        head = lines[0]
        if not head.lower().startswith("navigate to"):
            head = f"Navigate to {target_url}."
            steps_src = lines
        else:
            steps_src = lines[1:]
        cleaned_steps: List[str] = []
        for line in steps_src:
            step = re.sub(r"^[a-zA-Z]\.\s*", "", line).strip()
            if step:
                cleaned_steps.append(step)
        numbered = [f"{chr(97 + i)}. {step}" for i, step in enumerate(cleaned_steps[:26])]
        return "\n".join([head] + numbered) if numbered else head

    def _sanitize_auth_placeholders(description_text: str) -> str:
        if not auth_context:
            return description_text
        mode = auth_context.get("mode")
        desc = description_text
        replacements = {
            "<valid_username>": auth_context.get("username") or "<enter_username>",
            "<valid_password>": auth_context.get("password") or "<enter_password>",
        }
        for old, new in replacements.items():
            desc = desc.replace(old, str(new))

        if mode == "name_email_otp":
            def _extract_widget_value(field: str, fallback: str) -> str:
                patterns = {
                    "name": [
                        r"name[^\n]*?give it as\s*([^\n,]+)",
                        r"name[^\n]*?as\s*([^\n,]+)",
                    ],
                    "email": [
                        r"email[^\n]*?give(?:\s+it)?\s+as\s*([^\n,]+)",
                        r"email[^\n]*?as\s*([^\n,]+)",
                        r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
                    ],
                    "otp": [
                        r"otp[^\n]*?give(?:\s+it)?\s+as\s*([0-9]{4,8})",
                        r"otp[^\n]*?as\s*([0-9]{4,8})",
                    ],
                }
                for pat in patterns[field]:
                    match = re.search(pat, desc, flags=re.IGNORECASE)
                    if match:
                        value = match.group(1).strip(" '\".,")
                        value = re.sub(r"\s+in the\s+.+$", "", value, flags=re.IGNORECASE)
                        value = re.sub(r"\s+field.*$", "", value, flags=re.IGNORECASE)
                        value = re.sub(r"\s+and wait.*$", "", value, flags=re.IGNORECASE)
                        return value.strip(" '\".,")
                return fallback

            name_val = _extract_widget_value("name", str(auth_context.get("name") or "<enter_name>"))
            email_val = _extract_widget_value("email", str(auth_context.get("email") or "<enter_email>"))
            otp_val = _extract_widget_value("otp", str(auth_context.get("otp") or "<enter_otp>"))

            lines = [line.strip() for line in desc.splitlines() if line.strip()]
            non_auth_steps: List[str] = []
            for raw_line in lines[1:]:
                step = re.sub(r"^[a-zA-Z]\.\s*", "", raw_line).strip()
                sl = step.lower()
                if any(
                    phrase in sl
                    for phrase in [
                        "enter username",
                        "enter password",
                        "click the login button",
                        "ask for the name",
                        "ask for email",
                        "ask for otp",
                        "enter the name",
                        "enter the email",
                        "enter the otp",
                        "complete the login process",
                    ]
                ):
                    continue
                if "successful verification" in sl and "after verification" not in sl:
                    continue
                if "documents folder icon" in sl or "validate content button" in sl:
                    continue
                non_auth_steps.append(step)

            normalized_followups: List[str] = []
            lower_desc = desc.lower()
            if (
                "back action" in lower_desc
                or "in-widget back" in lower_desc
                or ("back" in lower_desc and "widget" in lower_desc)
            ):
                normalized_followups.append(
                    "After verification, perform the post-login navigation: use the in-widget back action to move out of the current conversation panel."
                )
            if "documents" in lower_desc or "folder icon" in lower_desc:
                normalized_followups.append(
                    "Then click the folder icon (Documents) in the bottom widget toolbar, next to the chat icon, and open Documents."
                )
            if "validate content" in lower_desc:
                normalized_followups.append(
                    "Wait until the file list is visible, then click Validate Content for the first file only."
                )

            canonical_steps = [
                f"It will first ask for the name, give it as {name_val}.",
                f"Later it will ask for email, give {email_val}.",
                f"Then it will ask for otp give it as {otp_val}, and submit and wait for response and then do next step.",
                "Confirm the widget shows successful verification or login-complete style confirmation text.",
            ]

            merged_steps: List[str] = []
            for step in canonical_steps + normalized_followups + non_auth_steps:
                if step and step not in merged_steps:
                    merged_steps.append(step)

            merged_steps.append("Mark this complete case success only if all the steps are completed and passed.")
            desc = f"Navigate to {target_url}.\n" + "\n".join(
                f"{chr(97 + idx)}. {step}" for idx, step in enumerate(merged_steps[:26])
            )
        return _format_lettered_steps(desc)

    normalized: List[Dict[str, str]] = []
    for i, case in enumerate(cases, start=1):
        test_id = str(case.get("test_id") or f"TC-{i:03d}").strip()
        if not re.match(r"^TC-\d{3,}$", test_id):
            test_id = f"TC-{i:03d}"

        description = str(case.get("description") or "").strip()
        if not description:
            description = f"Navigate to {target_url}. Perform the described UI flow and validate results."
        # Ensure it starts with the target URL
        if not description.lower().startswith("navigate to"):
            description = f"Navigate to {target_url}.\n{description}"
        description = _sanitize_auth_placeholders(description)

        priority = str(case.get("priority") or "Medium").strip().title()
        if priority not in {"Critical", "High", "Medium", "Low"}:
            priority = "Medium"

        category = str(case.get("category") or "Functional").strip()
        allowed_categories = {
            "Smoke Test",
            "Regression",
            "Functionality Test",
            "Functional",
            "Integration",
            "End-to-End",
            "Security",
        }
        if category not in allowed_categories:
            category = "Functional"

        normalized.append(
            {
                "test_id": test_id,
                "test_name": str(case.get("test_name") or f"Generated Test {i}").strip(),
                "description": description,
                "expected_result": str(
                    case.get("expected_result")
                    or (
                        "Authentication/verification completes successfully and the expected post-login state is visible."
                        if auth_context and auth_context.get("mode") == "name_email_otp"
                        else "Expected behavior is observed without errors."
                    )
                ).strip(),
                "priority": priority,
                "category": category,
            }
        )

    return pd.DataFrame(
        normalized,
        columns=["test_id", "test_name", "description", "expected_result", "priority", "category"],
    )


def _write_csv(df: pd.DataFrame, out_path: Path) -> None:
    """Write CSV with proper quoting so multi-line descriptions survive."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["test_id", "test_name", "description", "expected_result", "priority", "category"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        for _, row in df.iterrows():
            writer.writerow(row.to_dict())


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/api/generate-test-cases")
def generate_test_cases(
    target_url: str = Form(..., description="Application URL where tests will run."),
    username: Optional[str] = Form(default=None, description="Optional login username for test steps."),
    password: Optional[str] = Form(default=None, description="Optional login password for test steps."),
    role_filter: Optional[str] = Form(
        default=None,
        description="Only generate test cases for rows whose Role column matches this value. Leave empty to include all rows.",
    ),
    user_prompt: Optional[str] = Form(
        default=None,
        description="Optional extra guidance to force specific style/content in generated test cases.",
    ),
    spreadsheet_url: Optional[str] = Form(default=None, description="Google Sheets or CSV URL."),
    csv_path: Optional[str] = Form(default=None, description="Local CSV path on API host."),
    output_filename: str = Form(default="generated_test_cases.csv"),
    max_cases: int = Form(default=50, description="Maximum number of test cases to generate (1–200)."),
    input_file: Optional[UploadFile] = File(default=None),
) -> Dict[str, Any]:
    # --- Validate target URL ---
    if not target_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="target_url must start with http:// or https://")

    # --- Read source data ---
    if not (spreadsheet_url or input_file or csv_path):
        # no external spreadsheet or file provided; allow a single prompt row
        if user_prompt and user_prompt.strip():
            df = pd.DataFrame([{"Description": user_prompt.strip()}])
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide a spreadsheet URL, csv_path, input_file, or a prompt in user_prompt.",
            )
    else:
        df = _read_source(spreadsheet_url=spreadsheet_url, input_file=input_file, csv_path=csv_path)
    rows = _build_source_rows(df)
    if not rows:
        raise HTTPException(status_code=400, detail="Source data is empty after parsing.")
    auth_context = _infer_auth_context(rows=rows, username=username, password=password)
    url_probe = _inspect_target_url(target_url)

    max_cases = max(1, min(max_cases, 200))

    # --- Generate test cases (LLM first, fallback second) ---
    generation_mode = "llm"
    try:
        generated_cases = _generate_with_llm(
            rows=rows,
            target_url=target_url,
            auth_context=auth_context,
            url_probe=url_probe,
            role_filter=role_filter,
            max_cases=max_cases,
            user_prompt=user_prompt,
        )
    except Exception as llm_error:
        # Log the error so operators can see why LLM failed
        print(f"[WARN] LLM generation failed ({llm_error}), switching to context-aware fallback.")
        generation_mode = "fallback"
        generated_cases = _fallback_generate(
            rows=rows,
            target_url=target_url,
            auth_context=auth_context,
            role_filter=role_filter,
            max_cases=max_cases,
        )

    # --- Normalise & write ---
    out_df = _normalize_cases(generated_cases, target_url=target_url, auth_context=auth_context)
    safe_name = _slugify_filename(output_filename)
    out_path = OUTPUT_DIR / safe_name
    _write_csv(out_df, out_path)

    return {
        "status": "ok",
        "mode": generation_mode,
        "output_file": str(out_path.resolve()),
        "total_cases": int(len(out_df)),
        "auth_mode": auth_context.get("mode"),
        "url_probe": url_probe,
        "role_filter_applied": role_filter or "none (all rows included)",
        "runner_command": f"python main.py -i {out_path.as_posix()} -y",
        "note": "Use the output_file path above as input to main.py.",
    }


# ---------------------------------------------------------------------------
# Run runner command endpoint
# ---------------------------------------------------------------------------

import subprocess
import sys

@app.post("/api/run-runner-command")
async def run_runner_command(runner_command: str = Form(...)):
    """Execute the runner command and return results."""
    try:
        # Extract the CSV path from the command
        # Command format: "python main.py -i path/to/file.csv -y"
        match = re.search(r'-i\s+"?([^"]+)"?', runner_command)
        if not match:
            raise HTTPException(status_code=400, detail="Invalid runner command format")
        
        csv_path = match.group(1)
        csv_file = Path(csv_path)
        
        if not csv_file.exists():
            raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")
        
        # Run the command with timeout
        import platform
        if platform.system() == "Windows":
            # On Windows, use shell=True for proper command parsing
            result = subprocess.run(
                runner_command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,  # 5 minute timeout
                cwd=str(Path.cwd())
            )
        else:
            # On Unix/Linux/Mac
            result = subprocess.run(
                runner_command.split(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                cwd=str(Path.cwd())
            )
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Runner execution failed: {result.stderr or result.stdout}"
            )
        
        return {
            "status": "ok",
            "message": "Test execution completed successfully",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "csv_path": csv_path
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Test execution timed out after 5 minutes")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run runner command: {str(e)}")


# ---------------------------------------------------------------------------
# Register shared helpers with the project API module & mount router
# ---------------------------------------------------------------------------

register_dependencies(
    output_dir=OUTPUT_DIR,
    slugify_fn=_slugify_filename,
    write_csv_fn=_write_csv,
    infer_auth_ctx_fn=_infer_auth_context,
    inspect_url_fn=_inspect_target_url,
    generate_llm_fn=_generate_with_llm,
    fallback_generate_fn=_fallback_generate,
    normalize_cases_fn=_normalize_cases,
    read_source_fn=_read_source,
    build_source_rows_fn=_build_source_rows,
)

app.include_router(project_router)
