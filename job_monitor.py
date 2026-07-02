"""
LinkedIn Job Posting Automation
--------------------------------
Fetches new job postings from LinkedIn's public "guest" jobs search endpoint,
appends new (not-yet-seen) postings to a Google Sheet, and emails a summary
notification when it runs.

Settings are read from config.json (next to this script).
Designed to be triggered every hour by Windows Task Scheduler.
"""

import os
import sys
import json
import time
import random
import smtplib
import logging
import urllib.parse
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "job_fetcher.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as _f:
    CFG = json.load(_f)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
]

SHEET_HEADERS = ["Job ID", "Title", "Company", "Location", "Posted on LinkedIn", "Link", "Fetched At"]


# --------------------------------------------------------------------------
# LinkedIn fetching
# --------------------------------------------------------------------------
def build_base_url() -> str:
    keyword_query = "(" + " OR ".join(f'"{kw}"' for kw in CFG["keywords"]) + ")"
    params = {"keywords": keyword_query, "location": CFG["location"], "f_TPR": f"r{CFG['time_window_seconds']}"}
    return f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{urllib.parse.urlencode(params, safe='()')}"


def fetch_jobs_page(base_url: str, start: int) -> str:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/jobs/",
        "Connection": "keep-alive",
    }
    resp = requests.get(f"{base_url}&start={start}", headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_jobs(html: str) -> list[dict]:
    jobs = []
    for card in BeautifulSoup(html, "html.parser").find_all("li"):
        try:
            title_tag = card.find("h3", class_="base-search-card__title")
            link_tag = card.find("a", class_="base-card__full-link")
            if not title_tag or not link_tag:
                continue
            job_link = link_tag["href"].split("?")[0]
            company_tag = card.find("h4", class_="base-search-card__subtitle")
            location_tag = card.find("span", class_="job-search-card__location")
            time_tag = card.find("time")
            jobs.append({
                "job_id": job_link.rstrip("/").split("-")[-1],
                "title": title_tag.get_text(strip=True),
                "company": company_tag.get_text(strip=True) if company_tag else "",
                "location": location_tag.get_text(strip=True) if location_tag else "",
                "linkedin_posted": time_tag.get_text(strip=True) if time_tag else "",
                "link": job_link,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            log.warning("Failed to parse a job card: %s", exc)
    return jobs


def fetch_all_jobs() -> list[dict]:
    base_url = build_base_url()
    pages = CFG.get("pages_to_fetch", 2)
    per_page = CFG.get("results_per_page", 10)
    all_jobs = []
    for page_num in range(pages):
        start = page_num * per_page
        try:
            html = fetch_jobs_page(base_url, start)
        except requests.RequestException as exc:
            log.error("Request failed for start=%s: %s", start, exc)
            break
        jobs = parse_jobs(html)
        if not jobs:
            log.info("No results at start=%s, stopping pagination.", start)
            break
        all_jobs.extend(jobs)
        if page_num < pages - 1:
            time.sleep(random.uniform(5, 10))
    log.info("Fetched %d job listings total.", len(all_jobs))
    return all_jobs


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------
def cleanup_old_worksheets(spreadsheet):
    cutoff_date = datetime.now().date() - timedelta(days=7)
    for ws in spreadsheet.worksheets():
        try:
            if datetime.strptime(ws.title, "%d-%m-%Y").date() < cutoff_date:
                spreadsheet.del_worksheet(ws)
                log.info("Deleted old worksheet: %s", ws.title)
        except ValueError:
            continue


def get_sheet():
    creds_path = os.path.join(BASE_DIR, CFG["google_service_account_json"])
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Service account file not found at: {creds_path}\n"
            f"Make sure '{CFG['google_service_account_json']}' is in the same folder as job_monitor.py."
        )
    client = gspread.authorize(Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    ))
    try:
        sh = client.open_by_key(CFG["spreadsheet_id"])
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(f"Spreadsheet '{CFG['spreadsheet_id']}' not found or not shared with the service account.")
    except Exception as exc:
        raise RuntimeError(f"Failed to open spreadsheet: {exc}")

    cleanup_old_worksheets(sh)
    today = datetime.now().strftime("%d-%m-%Y")
    try:
        worksheet = sh.worksheet(today)
        log.info("Using worksheet: %s", today)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Worksheet '%s' not found — creating it.", today)
        worksheet = sh.add_worksheet(title=today, rows=1000, cols=10)
    return worksheet


def ensure_headers(worksheet):
    if worksheet.row_values(1) != SHEET_HEADERS:
        worksheet.update(range_name="A1:G1", values=[SHEET_HEADERS])
        worksheet.format("A1:G1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"})
        worksheet.freeze(rows=1)


def get_existing_job_ids(worksheet) -> set:
    try:
        job_ids = worksheet.col_values(1)  # column A = Job ID
    except Exception:
        job_ids = []
    return set(job_ids[1:])  # skip header


def append_new_jobs(worksheet, jobs: list[dict]) -> list[dict]:
    existing_job_ids = get_existing_job_ids(worksheet)
    new_jobs = [j for j in jobs if j["job_id"] not in existing_job_ids]
    if new_jobs:
        worksheet.append_rows(
            [[j["job_id"], j["title"], j["company"], j["location"], j["linkedin_posted"], j["link"], j["fetched_at"]] for j in new_jobs],
            value_input_option="RAW",
        )
        log.info("Appended %d new job(s) to the sheet.", len(new_jobs))
    else:
        log.info("No new jobs found this run.")
    return new_jobs


# --------------------------------------------------------------------------
# Email notification
# --------------------------------------------------------------------------
def send_email(subject: str, body_html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = CFG["email_from"]
    msg["To"] = CFG["email_to"]
    msg.attach(MIMEText(body_html, "html"))
    try:
        with smtplib.SMTP(CFG.get("smtp_host", "smtp.gmail.com"), CFG.get("smtp_port", 587)) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(CFG["email_from"], CFG["email_app_password"])
            server.sendmail(CFG["email_from"], [CFG["email_to"]], msg.as_string())
        log.info("Notification email sent to %s", CFG["email_to"])
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


def build_email_body(new_jobs: list[dict]) -> str:
    sheet_url = f"https://docs.google.com/spreadsheets/d/{CFG['spreadsheet_id']}"
    if new_jobs:
        rows_html = "".join(
            f"<tr>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['title']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['company']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['location']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['linkedin_posted']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'><a href='{j['link']}'>View</a></td>"
            f"</tr>"
            for j in new_jobs
        )
        content = (
            f"<p><b>{len(new_jobs)} new job(s)</b> found.</p>"
            "<table style='border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px;'>"
            "<tr style='background:#f2f2f2;'>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Title</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Company</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Location</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Posted on LinkedIn</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Link</th>"
            "</tr>"
            f"{rows_html}</table>"
        )
    else:
        content = "<p>No new jobs were found in this run.</p>"
    return f"<html><body style='font-family:sans-serif;'>{content}<p style='margin-top:20px;'><a href='{sheet_url}'>Open Spreadsheet</a></p></body></html>"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    log.info("===== Job fetcher run started =====")
    jobs = fetch_all_jobs()

    try:
        worksheet = get_sheet()
        ensure_headers(worksheet)
        new_jobs = append_new_jobs(worksheet, jobs)
        sheet_error = None
    except Exception as exc:
        log.exception("Failed to write to Google Sheet")
        new_jobs = []
        sheet_error = str(exc)

    if CFG.get("email_on_every_run", True) or new_jobs or sheet_error:
        if sheet_error:
            subject = "LinkedIn Jobs: ERROR writing to Google Sheet"
            body = (
                f"<html><body style='font-family:sans-serif;'>"
                f"<h2>LinkedIn Job Fetcher — Sheet Write Failed</h2>"
                f"<p>Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
                f"<p>{len(jobs)} job(s) fetched, but sheet write failed:</p>"
                f"<pre style='background:#f7f7f7;padding:10px;border:1px solid #ddd;white-space:pre-wrap;'>{sheet_error}</pre>"
                f"<p>Check job_fetcher.log for the full traceback.</p>"
                f"</body></html>"
            )
        else:
            subject = f"🟢 {len(new_jobs)} new job(s) found" if new_jobs else "LinkedIn Jobs: run complete, no new postings"
            body = build_email_body(new_jobs)
        send_email(subject, body)
    else:
        log.info("email_on_every_run is false and no new jobs found — skipping email.")

    log.info("===== Job fetcher run finished =====")


if __name__ == "__main__":
    main()