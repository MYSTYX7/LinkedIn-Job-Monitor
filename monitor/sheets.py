"""Google Sheets read/write: worksheet management, dedup, and appending jobs."""

import os
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("job_monitor")

# "Applied?" stays column G (index 6) to match any existing sheets/checkboxes;
# "Cold Email Hook" and "ATS Score" are appended at the end so older sheets
# don't need to be reshaped.
SHEET_HEADERS = ["Job ID", "Title", "Company", "Location", "Posted on LinkedIn", "Link", "Applied?", "Fetched At", "Cold Email Hook", "ATS Score"]

# ATS Score column (0-indexed) — used both to write the value and to color the cell.
ATS_SCORE_COLUMN_INDEX = 9


def _score_color(score: int | None) -> dict | None:
    """Background color for the ATS Score cell: green (>=80), yellow (60-70), red (<60).
    Returns None (no coloring) if there's no score.
    """
    if score is None:
        return None
    if score >= 80:
        return {"red": 0.71, "green": 0.88, "blue": 0.71}   # green
    if score >= 60:
        return {"red": 1.0, "green": 0.95, "blue": 0.6}     # yellow
    return {"red": 0.96, "green": 0.71, "blue": 0.71}       # red


def cleanup_old_worksheets(spreadsheet, group: str):
    cutoff_date = datetime.now().date() - timedelta(days=7)
    for ws in spreadsheet.worksheets():
        # Only clean up worksheets belonging to this group (e.g. "30-06-2025-india")
        if not ws.title.endswith(f"-{group}"):
            continue
        try:
            sheet_date = datetime.strptime(ws.title.replace(f"-{group}", ""), "%d-%m-%Y").date()
            if sheet_date < cutoff_date:
                spreadsheet.del_worksheet(ws)
                log.info("Deleted old worksheet: %s", ws.title)
        except ValueError:
            continue


def get_sheet(cfg: dict, base_dir: str, group: str):
    creds_path = os.path.join(base_dir, cfg["google_service_account_json"])
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Service account file not found at: {creds_path}\n"
            f"Make sure '{cfg['google_service_account_json']}' is in the same folder as job_monitor.py."
        )
    client = gspread.authorize(Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    ))
    try:
        sh = client.open_by_key(cfg["spreadsheet_id"])
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(f"Spreadsheet '{cfg['spreadsheet_id']}' not found or not shared with the service account.")
    except Exception as exc:
        raise RuntimeError(f"Failed to open spreadsheet: {exc}")

    cleanup_old_worksheets(sh, group)
    tab_name = f"{datetime.now().strftime('%d-%m-%Y')}-{group}"
    try:
        worksheet = sh.worksheet(tab_name)
        log.info("Using worksheet: %s", tab_name)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Worksheet '%s' not found — creating it.", tab_name)
        worksheet = sh.add_worksheet(title=tab_name, rows=1000, cols=10)
    return worksheet


def ensure_headers(worksheet):
    if worksheet.row_values(1) != SHEET_HEADERS:
        worksheet.update(range_name="A1:J1", values=[SHEET_HEADERS])
        worksheet.format("A1:J1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"})
        worksheet.freeze(rows=1)


def get_existing_job_ids(worksheet) -> set:
    try:
        job_ids = worksheet.col_values(1)  # column A = Job ID
    except Exception:
        job_ids = []
    return set(job_ids[1:])  # skip header


def get_new_jobs(worksheet, jobs: list[dict]) -> list[dict]:
    """Return only the jobs not already present in the sheet.

    Call this BEFORE generating cold-email hooks or shortening links, so
    those (comparatively expensive) steps only ever run on postings that
    are actually going to be written to the sheet.
    """
    existing_job_ids = get_existing_job_ids(worksheet)
    new_jobs = [j for j in jobs if j["job_id"] not in existing_job_ids]
    removed = len(jobs) - len(new_jobs)
    if removed:
        log.info("Skipped %d job(s) already present in the sheet.", removed)
    if not new_jobs:
        log.info("No new jobs found this run.")
    return new_jobs


def append_jobs(worksheet, new_jobs: list[dict]) -> list[dict]:
    """Append already-deduplicated jobs to the sheet. Assumes `new_jobs` was
    produced by get_new_jobs() and every entry already has its cold-email
    hook, ATS score, and (possibly) shortened link set.
    """
    if not new_jobs:
        return new_jobs

    next_row = len(worksheet.get_all_values()) + 1
    worksheet.append_rows(
        [[j["job_id"], j["title"], j["company"], j["location"], j["linkedin_posted"],
          j["link"], False, j["fetched_at"], j.get("cold_email_hook", ""),
          j["ats_score"] if j.get("ats_score") is not None else ""] for j in new_jobs],
        value_input_option="RAW",
    )

    requests_batch = [{
        "setDataValidation": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": next_row - 1,
                "endRowIndex": next_row - 1 + len(new_jobs),
                "startColumnIndex": 6,  # column G = Applied? (0-indexed)
                "endColumnIndex": 7,
            },
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }
    }]

    for i, job in enumerate(new_jobs):
        color = _score_color(job.get("ats_score"))
        if color is None:
            continue
        row_index = next_row - 1 + i
        requests_batch.append({
            "repeatCell": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": ATS_SCORE_COLUMN_INDEX,
                    "endColumnIndex": ATS_SCORE_COLUMN_INDEX + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    worksheet.spreadsheet.batch_update({"requests": requests_batch})
    log.info("Appended %d new job(s) to the sheet.", len(new_jobs))
    return new_jobs