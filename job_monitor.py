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

# --------------------------------------------------------------------------
# Paths / logging
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "job_fetcher.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
]


# --------------------------------------------------------------------------
# Build search URL from config
# --------------------------------------------------------------------------
def build_keyword_query(keywords: list[str]) -> str:
    """Turns ["DevOps Engineer", "SRE"] into ("DevOps Engineer" OR "SRE")"""
    quoted = [f'"{kw}"' for kw in keywords]
    return "(" + " OR ".join(quoted) + ")"


def build_base_url() -> str:
    keyword_query = build_keyword_query(CFG["keywords"])
    params = {
        "keywords": keyword_query,
        "location": CFG["location"],
        "f_TPR": f"r{CFG['time_window_seconds']}",
    }
    query_string = urllib.parse.urlencode(params, safe='()"')
    return f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{query_string}"


# --------------------------------------------------------------------------
# LinkedIn fetching
# --------------------------------------------------------------------------
def fetch_jobs_page(base_url: str, start: int) -> str:
    url = f"{base_url}&start={start}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/jobs/",
        "Connection": "keep-alive",
    }

    resp = requests.get(
        url,
        headers=headers,
        timeout=20
    )
    resp.raise_for_status()
    return resp.text


def parse_jobs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("li")
    jobs = []

    for card in cards:
        try:
            title_tag = card.find("h3", class_="base-search-card__title")
            company_tag = card.find("h4", class_="base-search-card__subtitle")
            location_tag = card.find("span", class_="job-search-card__location")
            link_tag = card.find("a", class_="base-card__full-link")
            time_tag = card.find("time")

            if not title_tag or not link_tag:
                continue

            job_link = link_tag["href"].split("?")[0]

            # Extract Job ID from URL
            job_id = job_link.rstrip("/").split("-")[-1]

            job = {
                "job_id": job_id,
                "title": title_tag.get_text(strip=True),
                "company": company_tag.get_text(strip=True) if company_tag else "",
                "location": location_tag.get_text(strip=True) if location_tag else "",
                "linkedin_posted": time_tag.get_text(strip=True) if time_tag else "",
                "link": job_link,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse a job card: %s", exc)
            continue

    return jobs


def fetch_all_jobs() -> list[dict]:
    base_url = build_base_url()
    pages = CFG.get("pages_to_fetch", 2)
    per_page = CFG.get("results_per_page", 10)
    delay = random.uniform(5, 10)
    
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
            time.sleep(delay)

    log.info("Fetched %d job listings total.", len(all_jobs))
    return all_jobs


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------
SHEET_HEADERS = [
    "Job ID",
    "Title",
    "Company",
    "Location",
    "Posted on LinkedIn",
    "Link",
    "Fetched At"
]


def cleanup_old_worksheets(spreadsheet):
    """
    Deletes worksheets older than 7 days.
    Worksheet names must be in DD-MM-YYYY format.
    """
    cutoff_date = datetime.now().date() - timedelta(days=7)

    for ws in spreadsheet.worksheets():
        try:
            sheet_date = datetime.strptime(ws.title, "%d-%m-%Y").date()

            if sheet_date < cutoff_date:
                spreadsheet.del_worksheet(ws)
                log.info("Deleted old worksheet: %s", ws.title)

        except ValueError:
            # Ignore worksheets not matching DD-MM-YYYY format
            continue


def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_path = os.path.join(BASE_DIR, CFG["google_service_account_json"])

    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Service account file not found at: {creds_path}\n"
            f"Make sure the JSON key file is named exactly '{CFG['google_service_account_json']}' "
            f"and placed in the same folder as job_fetcher.py."
        )

    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        sh = client.open_by_key(CFG["spreadsheet_id"])
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(
            f"Spreadsheet with ID '{CFG['spreadsheet_id']}' not found or not shared "
            f"with the service account."
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to open spreadsheet: {exc}")

    cleanup_old_worksheets(sh)

    today_sheet_name = datetime.now().strftime("%d-%m-%Y")

    try:
        worksheet = sh.worksheet(today_sheet_name)
        log.info("Using worksheet: %s", today_sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        log.info(
            "Worksheet '%s' not found — creating it.",
            today_sheet_name
        )
        worksheet = sh.add_worksheet(
            title=today_sheet_name, rows=1000, cols=10
        )

    return worksheet

def ensure_headers(worksheet):
    first_row = worksheet.row_values(1)

    if first_row != SHEET_HEADERS:
        # Write headers
        worksheet.update(
            range_name="A1:G1",
            values=[SHEET_HEADERS]
        )

        # Format header row
        worksheet.format(
            "A1:G1",
            {
                "textFormat": {
                    "bold": True
                },
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE"
            }
        )

        # Freeze header row
        worksheet.freeze(rows=1)


def get_existing_links(worksheet) -> set:
    try:
        links = worksheet.col_values(4)  # column D = Link
    except Exception:
        links = []
    return set(links[1:])  # skip header


def append_new_jobs(worksheet, jobs: list[dict]) -> list[dict]:
    existing_links = get_existing_links(worksheet)
    new_jobs = [j for j in jobs if j["link"] not in existing_links]

    if new_jobs:
        rows = [
            [
                j["job_id"],
                j["title"],
                j["company"],
                j["location"],
                j["linkedin_posted"],
                j["link"],
                j["fetched_at"]
            ]
            for j in new_jobs
        ]
        worksheet.append_rows(rows, value_input_option="RAW")
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

    smtp_host = CFG.get("smtp_host", "smtp.gmail.com")
    smtp_port = CFG.get("smtp_port", 587)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(CFG["email_from"], CFG["email_app_password"])
            server.sendmail(CFG["email_from"], [CFG["email_to"]], msg.as_string())
        log.info("Notification email sent to %s", CFG["email_to"])
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send email: %s", exc)


def build_email_body(new_jobs: list[dict], total_fetched: int) -> str:
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if new_jobs:
        rows_html = "".join(
            f"<tr>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['title']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['company']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['location']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{j['linkedin_posted']}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>"
            f"<a href='{j['link']}'>View</a></td>"
            f"</tr>"
            for j in new_jobs
        )
        table = (
            "<table style='border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px;'>"
            "<tr style='background:#f2f2f2;'>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Title</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Company</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Location</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Posted on LinkedIn</th>"
            "<th style='padding:6px;border:1px solid #ddd;text-align:left;'>Link</th>"
            "</tr>"
            f"{rows_html}"
            "</table>"
        )
        summary = f"<p><b>{len(new_jobs)} new job(s)</b> found.</p>"
    else:
        table = ""
        summary = "<p>No new jobs were found in this run.</p>"

    sheet_url = f"https://docs.google.com/spreadsheets/d/{CFG['spreadsheet_id']}"
    body = (
        f"<html><body style='font-family:sans-serif;'>"
        f"{summary}"
        f"{table}"
        f"<p style='margin-top:20px;'><a href='{sheet_url}'>Open Spreadsheet</a></p>"
        f"</body></html>"
    )
    return body


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
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to write to Google Sheet")
        new_jobs = []
        sheet_error = str(exc)

    should_email = CFG.get("email_on_every_run", True) or len(new_jobs) > 0 or sheet_error

    if should_email:
        if sheet_error:
            subject = "LinkedIn Jobs: ERROR writing to Google Sheet"
            body = (
                f"<html><body style='font-family:sans-serif;'>"
                f"<h2>LinkedIn Job Fetcher — Sheet Write Failed</h2>"
                f"<p>Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
                f"<p>{len(jobs)} job(s) were fetched from LinkedIn, but writing to the "
                f"Google Sheet failed with this error:</p>"
                f"<pre style='background:#f7f7f7;padding:10px;border:1px solid #ddd;"
                f"white-space:pre-wrap;'>{sheet_error}</pre>"
                f"<p>Check job_fetcher.log on your machine for the full traceback.</p>"
                f"</body></html>"
            )
        else:
            subject = (
                f"🟢 {len(new_jobs)} new job(s) found"
                if new_jobs
                else "LinkedIn Jobs: run complete, no new postings"
            )
            body = build_email_body(new_jobs, len(jobs))
        send_email(subject, body)
    else:
        log.info("email_on_every_run is false and no new jobs found — skipping email.")

    log.info("===== Job fetcher run finished =====")


if __name__ == "__main__":
    main()