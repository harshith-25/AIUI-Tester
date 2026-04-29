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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from project_api import EXECUTIONS, router as project_router, register_dependencies, _sanitize_execution  # noqa: E402
from database import init_db  # noqa: E402

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
        if not any(str(col).lower().startswith("unnamed:") for col in source_df.columns):
            return source_df

        for idx in range(min(5, len(source_df))):
            row_values = [_clean_text(v) for v in source_df.iloc[idx].tolist()]
            norm = {_normalize_name(v) for v in row_values if v}
            if {"role", "description"}.issubset(norm):
                body = source_df.iloc[idx + 1 :].copy()
                header = [v if v else f"col_{i}" for i, v in enumerate(row_values)]

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
            if role_val in {"\u201c", '"', "''", "ditto", "same"}:
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


# ---------------------------------------------------------------------------
# FIX 1: Robust auth context — always prefer explicitly passed username/password
# ---------------------------------------------------------------------------

def _infer_auth_context(
    rows: List[Dict[str, Any]],
    username: Optional[str],
    password: Optional[str],
) -> Dict[str, Optional[str]]:
    """
    Build a complete auth context.

    Priority order for each credential field:
      1. Explicitly passed username / password form params  ← HIGHEST PRIORITY
      2. Extracted from spreadsheet descriptions
      3. Sensible fallback placeholders                     ← LOWEST PRIORITY

    This guarantees that if the caller supplies username/password they are ALWAYS
    embedded in every generated test step — the original code had a bug where the
    extracted values could silently overwrite the caller-supplied ones when the
    cleaned string was empty.
    """
    descriptions: List[str] = []
    for row in rows:
        for key in ("Description", "description", "Feature", "feature", "Name", "name", "title"):
            val = str(row.get(key, "")).strip()
            if val:
                descriptions.append(val)
    blob = "\n".join(descriptions)
    blob_l = blob.lower()

    # --- Extract supplementary fields from the spreadsheet text ---
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    otp_match = re.search(
        r"\botp\b(?:[^\n]*?(?:as|is|:|give\s+(?:it\s+)?as))?\s*['\"]?(\d{4,8})['\"]?",
        blob,
        flags=re.IGNORECASE,
    )
    extracted_username = _extract_credential_like_value(blob, "username")
    extracted_password = _extract_credential_like_value(blob, "password")
    extracted_name = _extract_credential_like_value(blob, "name")

    def _as_clean(value: Any) -> str:
        """Return stripped non-empty string or empty string."""
        return value.strip() if isinstance(value, str) and value.strip() else ""

    # FIX: caller-supplied values win — only fall back to extracted values when
    # the caller supplied nothing.
    final_username: Optional[str] = (
        _as_clean(username) or _as_clean(extracted_username) or None
    )
    final_password: Optional[str] = (
        _as_clean(password) or _as_clean(extracted_password) or None
    )
    final_name: Optional[str] = _as_clean(extracted_name) or None
    final_email: Optional[str] = (
        email_match.group(0).strip() if email_match else None
    )
    final_otp: Optional[str] = (
        otp_match.group(1).strip() if otp_match else None
    )

    # Determine auth mode
    has_widget_auth = ("otp" in blob_l) or ("email" in blob_l and "name" in blob_l)
    has_standard_auth = bool(final_username or final_password)

    if not has_widget_auth and not has_standard_auth:
        mode = "none"
    elif has_widget_auth and not has_standard_auth:
        mode = "widget"
    else:
        mode = "standard"

    return {
        "mode": mode,
        "name": final_name,
        "email": final_email,
        "otp": final_otp,
        "username": final_username,
        "password": final_password,
    }


# ---------------------------------------------------------------------------
# FIX 2: _build_auth_steps — always produce concrete, non-empty steps
# ---------------------------------------------------------------------------

def _build_auth_steps(auth_context: Dict[str, Optional[str]]) -> List[str]:
    """
    Return a deterministic ordered list of concrete auth steps.

    Rules:
    - If mode == "none" → single generic fallback step.
    - Otherwise, ALWAYS emit credential steps with literal values (or
      safe angle-bracket placeholders when values are absent so the LLM
      never outputs raw empty strings or Python-None literals).
    - username/password step is emitted whenever at least one value exists.
    - widget fields (name, email, otp) are emitted when present.
    - A final "submit" step is always appended when there are credential steps.
    """
    mode = auth_context.get("mode", "none")

    if mode == "none":
        return ["Complete the app authentication flow and wait for post-login state."]

    def _val(key: str, placeholder: str) -> str:
        v = auth_context.get(key)
        return str(v).strip() if v and str(v).strip() else placeholder

    steps: List[str] = []

    # --- Widget / OTP auth fields ---
    name_val = auth_context.get("name")
    email_val = auth_context.get("email")
    otp_val = auth_context.get("otp")

    if name_val:
        steps.append(f"Enter name as {name_val}.")
    if email_val:
        steps.append(f"Enter email as {email_val}.")
    if otp_val:
        steps.append(f"Enter OTP as {otp_val}.")

    # --- Standard username / password fields ---
    username = auth_context.get("username")
    password = auth_context.get("password")

    # FIX: always produce steps for username/password when the mode is standard,
    # using safe placeholders instead of silently skipping
    if mode == "standard" or username or password:
        u_val = str(username).strip() if username and str(username).strip() else "<enter_username>"
        p_val = str(password).strip() if password and str(password).strip() else "<enter_password>"
        steps.append(f"Enter username as {u_val}.")
        steps.append(f"Enter password as {p_val}.")

    if steps:
        steps.append("Click the Login button and wait for the dashboard/home screen to load.")
        return steps

    # Ultimate fallback — should be unreachable but defensive
    return ["Complete the app authentication flow and wait for post-login state."]


# ---------------------------------------------------------------------------
# The core prompt
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

    auth_steps = _build_auth_steps(auth_context)
    auth_instruction = "\n".join(f"  {chr(97 + i)}. {step}" for i, step in enumerate(auth_steps))
    next_letter = chr(97 + len(auth_steps))

    # --- FIX 3: Embed resolved credential values directly in the prompt so the
    #     LLM cannot hallucinate different credentials or use stale placeholders ---
    username = auth_context.get("username")
    password = auth_context.get("password")
    cred_hint = ""
    if username or password:
        u_display = username if username else "<enter_username>"
        p_display = password if password else "<enter_password>"
        cred_hint = (
            f"\n\nCREDENTIAL REMINDER — use EXACTLY these values in every login step:\n"
            f"  Username: {u_display}\n"
            f"  Password: {p_display}\n"
            f"Do NOT substitute, invent, or omit them."
        )

    auth_mode = str(auth_context.get("mode") or "none")
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

=== STRICT RULES FOR EVERY FIELD ==={cred_hint}

test_id:
  Format: TC-001, TC-002, TC-003 ... (sequential, zero-padded to 3 digits)

test_name:
  Short, specific title. Example: "Super Admin - Delete Institution"
  Must reflect the exact feature/action being tested.

description:
  THIS IS THE MOST IMPORTANT FIELD. Follow this format exactly:

  "Navigate to {target_url}.
{auth_instruction}
  {next_letter}. [Next actionable step specific to this feature]
  {chr(ord(next_letter)+1)}. [Next step...]
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
  - For password/auth flows: include current password entry, new password entry, confirmation, and login verification.
  - CRITICAL: Never replace the auth steps above with generic text. They are pre-filled and must appear exactly.
  - Auth mode for this request is: {auth_mode}. Respect it for all test cases.
  - NEVER write generic steps like "Perform required input actions" or "Open the relevant module".
  - Minimum 6 steps, maximum 12 steps per test case.

expected_result:
  A clear, specific success statement. What does success look like for THIS test case?
  Must mention the specific feature outcome.
  NEVER write generic outcomes like "Expected UI behavior is correct."

priority:
  One of: Critical, High, Medium, Low
  Use Critical for login, auth, data deletion. High for CRUD operations. Medium for view/list. Low for cosmetic/minor.

category:
  One of: Smoke Test, Regression, Functional, Functionality Test, Integration, End-to-End, Security

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

    # FIX 4: Embed credential values in the system prompt too, so even
    # multi-turn model reasoning cannot forget them.
    username = auth_context.get("username")
    password = auth_context.get("password")
    cred_system_hint = ""
    if username or password:
        u = username or "<enter_username>"
        p = password or "<enter_password>"
        cred_system_hint = (
            f" IMPORTANT: Every test case description MUST include a step "
            f"'Enter username as {u}.' and a step 'Enter password as {p}.' "
            f"immediately after navigating to the URL. Never omit or alter these values."
        )

    system_base = (
        "You are a senior QA engineer. You output ONLY valid JSON. "
        "No markdown, no prose, no code fences. "
        "Every test case description must have specific, actionable, lettered UI steps — "
        f"never generic placeholders.{cred_system_hint}"
    )
    if user_prompt and user_prompt.strip():
        system_base += (
            " Follow this additional user guidance exactly where it does not conflict with required JSON schema: "
            f"{user_prompt.strip()}"
        )

    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=16000,
        messages=[
            {"role": "system", "content": system_base},
            {"role": "user", "content": prompt},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise ValueError(f"Model returned non-JSON output: {text[:300]}")
        payload = json.loads(m.group(0))

    cases = payload.get("test_cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError("Model returned empty or invalid test_cases list.")
    return cases


# ---------------------------------------------------------------------------
# Fallback templates
# ---------------------------------------------------------------------------

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
        "Verify post-login navigation is available.",
        "Confirm the expected dashboard or home screen is shown.",
        "Check that user profile/role indicators are correct.",
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
    """
    FIX 5: Always inject concrete auth steps (with real credential values)
    before any feature-specific steps, regardless of template_key.
    """
    lines = [f"Navigate to {target_url}."]
    letter = ord("a")

    # Always inject auth steps with real values
    auth_steps = _build_auth_steps(auth_context)
    for s in auth_steps:
        lines.append(f"{chr(letter)}. {s}")
        letter += 1

    steps = _STEP_TEMPLATES.get(template_key, _STEP_TEMPLATES["generic"])

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
            # Skip any step that looks like an auth step — we already emitted them above
            if any(
                k in cl
                for k in [
                    "enter username",
                    "enter password",
                    "login button",
                    "ask for the name",
                    "ask for email",
                    "ask for otp",
                    "enter the name",
                    "enter the email",
                    "enter the otp",
                    "complete the login",
                    "complete the app auth",
                ]
            ):
                continue
            source_steps.append(c + ("" if c.endswith(".") else "."))

    steps_to_use = source_steps if source_steps else steps
    for step in steps_to_use:
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
    cases: List[Dict[str, str]] = []
    counter = 1
    has_role_column = any((row.get("Role", "") or "").strip() for row in rows)

    for row in rows:
        if counter > max_cases:
            break

        role = (row.get("Role", "") or "").strip()
        if role in {"\u201c", '"', "''", "ditto", "same"}:
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

    # ---------------------------------------------------------------------------
    # FIX 6: Post-process normalisation — guarantee credentials appear in output
    # ---------------------------------------------------------------------------

    def _ensure_auth_steps_present(description_text: str) -> str:
        """
        After the LLM/fallback generates the description, scan for missing
        credential lines and inject them immediately after the Navigate line
        if they're absent.  This is a final safety net.
        """
        if not auth_context:
            return description_text

        username = auth_context.get("username")
        password = auth_context.get("password")

        if not username and not password:
            return description_text

        lines = description_text.splitlines()
        nav_line = lines[0] if lines else f"Navigate to {target_url}."
        rest = lines[1:]

        # Strip existing lettered steps so we can re-letter cleanly
        step_bodies: List[str] = []
        for raw in rest:
            body = re.sub(r"^[a-zA-Z]\.\s*", "", raw.strip()).strip()
            if body:
                step_bodies.append(body)

        # Check if credentials are already present (case-insensitive)
        blob = " ".join(step_bodies).lower()
        needs_username = username and ("enter username" not in blob)
        needs_password = password and ("enter password" not in blob)
        has_login_click = "click the login" in blob or "login button" in blob

        # Build canonical auth block
        canonical_auth = _build_auth_steps(auth_context)

        if not (needs_username or needs_password):
            # Nothing missing — just re-letter for consistency
            return _format_lettered_steps(description_text)

        # Separate pre-auth steps (shouldn't exist, but be safe) and post-auth steps
        auth_keywords = {
            "enter username", "enter password", "enter name", "enter email",
            "enter otp", "click the login", "login button", "wait for the dashboard",
            "wait for the home", "authentication flow",
        }

        non_auth_steps: List[str] = []
        for body in step_bodies:
            bl = body.lower()
            if any(kw in bl for kw in auth_keywords):
                continue  # remove stale/incorrect auth step
            non_auth_steps.append(body)

        # Reconstruct: nav → auth steps → feature steps
        all_steps = canonical_auth + non_auth_steps
        new_lines = [nav_line] + [
            f"{chr(97 + i)}. {s}" for i, s in enumerate(all_steps[:26])
        ]
        return "\n".join(new_lines)

    normalized: List[Dict[str, str]] = []
    for i, case in enumerate(cases, start=1):
        test_id = str(case.get("test_id") or f"TC-{i:03d}").strip()
        if not re.match(r"^TC-\d{3,}$", test_id):
            test_id = f"TC-{i:03d}"

        description = str(case.get("description") or "").strip()
        if not description:
            description = f"Navigate to {target_url}. Perform the described UI flow and validate results."
        if not description.lower().startswith("navigate to"):
            description = f"Navigate to {target_url}.\n{description}"

        # Apply the auth-guarantee pass
        description = _ensure_auth_steps_present(description)
        description = _format_lettered_steps(description)

        priority = str(case.get("priority") or "Medium").strip().title()
        if priority not in {"Critical", "High", "Medium", "Low"}:
            priority = "Medium"

        category = str(case.get("category") or "Functional").strip()
        allowed_categories = {
            "Smoke Test", "Regression", "Functionality Test", "Functional",
            "Integration", "End-to-End", "Security",
        }
        if category not in allowed_categories:
            category = "Functional"

        normalized.append(
            {
                "test_id": test_id,
                "test_name": str(case.get("test_name") or f"Generated Test {i}").strip(),
                "description": description,
                "expected_result": str(
                    case.get("expected_result") or "Expected behavior is observed without errors."
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
    if not target_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="target_url must start with http:// or https://")

    if not (spreadsheet_url or input_file or csv_path):
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
        print(f"[WARN] LLM generation failed ({llm_error}), switching to context-aware fallback.")
        generation_mode = "fallback"
        generated_cases = _fallback_generate(
            rows=rows,
            target_url=target_url,
            auth_context=auth_context,
            role_filter=role_filter,
            max_cases=max_cases,
        )

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
        match = re.search(r'-i\s+"?([^"]+)"?', runner_command)
        if not match:
            raise HTTPException(status_code=400, detail="Invalid runner command format")

        csv_path = match.group(1)
        csv_file = Path(csv_path)

        if not csv_file.exists():
            raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")

        import platform
        if platform.system() == "Windows":
            result = subprocess.run(
                runner_command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                cwd=str(Path.cwd()),
            )
        else:
            result = subprocess.run(
                runner_command.split(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                cwd=str(Path.cwd()),
            )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Runner execution failed: {result.stderr or result.stdout}",
            )

        return {
            "status": "ok",
            "message": "Test execution completed successfully",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "csv_path": csv_path,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Test execution timed out after 5 minutes")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run runner command: {str(e)}")


# ---------------------------------------------------------------------------
# Register shared helpers
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