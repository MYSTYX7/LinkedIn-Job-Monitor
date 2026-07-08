"""
LinkedIn Job Posting Automation
--------------------------------
Fetches new job postings from LinkedIn's public "guest" jobs search endpoint,
appends new (not-yet-seen) postings to a Google Sheet, and emails a summary
notification when it runs.

Settings are read from config.json (next to this script).
Designed to be triggered by Windows Task Scheduler with a --group argument:

    python job_monitor.py --group india           (every 2 hours)
    python job_monitor.py --group international   (every 6 hours)
"""

import os
import sys
import json
import time
import random
import smtplib
import logging
import argparse
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

SHEET_HEADERS = ["Job ID", "Title", "Company", "Location", "Posted on LinkedIn", "Link", "Applied?", "Fetched At"]


# --------------------------------------------------------------------------
# LinkedIn fetching
# --------------------------------------------------------------------------
def build_base_url(location: str, time_window_seconds: int) -> str:
    keyword_query = "(" + " OR ".join(f'"{kw}"' for kw in CFG["keywords"]) + ")"
    params = {"keywords": keyword_query, "location": location, "f_TPR": f"r{time_window_seconds}"}
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


def shorten_url(url: str) -> str:
    try:
        resp = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10,
        )
        if resp.status_code == 200 and resp.text.startswith("https://tinyurl.com"):
            return resp.text.strip()
    except Exception as exc:
        log.warning("TinyURL shortening failed for %s: %s — using original URL.", url, exc)
    return url
 
 
def shorten_job_urls(jobs: list[dict]) -> list[dict]:
    if not CFG.get("shorten_urls", False):
        return jobs
    log.info("Shortening URLs for %d job(s) via TinyURL...", len(jobs))
    for job in jobs:
        job["link"] = shorten_url(job["link"])
        time.sleep(0.3)
    return jobs


def filter_jobs(jobs: list[dict]) -> list[dict]:
    exclude = [kw.lower() for kw in CFG.get("exclude_title_keywords", [])]
    if not exclude:
        return jobs
    filtered = [j for j in jobs if not any(kw in j["title"].lower() for kw in exclude)]
    removed = len(jobs) - len(filtered)
    if removed:
        log.info("Filtered out %d senior-level job(s) based on exclude_title_keywords.", removed)
    return filtered


def fetch_jobs_for_location(location: str, time_window_seconds: int, pages_to_fetch: int) -> list[dict]:
    base_url = build_base_url(location, time_window_seconds)
    per_page = CFG.get("results_per_page", 10)
    all_jobs = []
    for page_num in range(pages_to_fetch):
        start = page_num * per_page
        try:
            html = fetch_jobs_page(base_url, start)
        except requests.RequestException as exc:
            log.error("Request failed for location=%s start=%s: %s", location, start, exc)
            break
        jobs = parse_jobs(html)
        if not jobs:
            log.info("No results at location=%s start=%s, stopping pagination.", location, start)
            break
        all_jobs.extend(jobs)
        if page_num < pages_to_fetch - 1:
            time.sleep(random.uniform(5, 10))
    return all_jobs


def fetch_all_jobs(group_cfg: dict) -> list[dict]:
    locations = group_cfg["locations"]
    time_window = group_cfg["time_window_seconds"]
    pages_to_fetch = group_cfg["pages_to_fetch"]
    all_jobs = []
    seen_ids = set()
    for i, location in enumerate(locations):
        log.info("Fetching jobs for location: %s", location)
        jobs = fetch_jobs_for_location(location, time_window, pages_to_fetch)
        for job in jobs:
            if job["job_id"] not in seen_ids:
                seen_ids.add(job["job_id"])
                all_jobs.append(job)
        log.info("Fetched %d job(s) for %s (%d unique total so far).", len(jobs), location, len(all_jobs))
        if i < len(locations) - 1:
            time.sleep(random.uniform(8, 15))  # pause between locations
    log.info("Fetched %d unique job(s) total across all locations.", len(all_jobs))
    return all_jobs


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------
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


def get_sheet(group: str):
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
        worksheet.update(range_name="A1:H1", values=[SHEET_HEADERS])
        worksheet.format("A1:H1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"})
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
        next_row = len(worksheet.get_all_values()) + 1
        worksheet.append_rows(
            [[j["job_id"], j["title"], j["company"], j["location"], j["linkedin_posted"], j["link"], False, j["fetched_at"]] for j in new_jobs],
            value_input_option="RAW",
        )
        worksheet.spreadsheet.batch_update({"requests": [{
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
        }]})
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


def send_telegram(new_jobs: list[dict], group: str):
    tg = CFG.get("telegram", {})
    if not tg.get("enabled", False):
        return
    api = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
 
    def post(text: str):
        try:
            resp = requests.post(api, json={"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML"}, timeout=10)
            if not resp.ok:
                log.error("Telegram API error: %s", resp.text)
        except Exception as exc:
            log.error("Failed to send Telegram message: %s", exc)
 
    if not new_jobs:
        if CFG.get("email_on_every_run", True):
            post(f"✅ <b>LinkedIn Cloud & DevOps Jobs [{group.upper()}]</b>\nNo new postings currently.")
        return
 
    # Build one message per run, splitting only when Telegram's 4096-char limit is approached
    chunk = f"🔔 <b>LinkedIn Cloud & DevOps Jobs - {len(new_jobs)} new posting(s) [{group.upper()}]</b>\n"
    for i, j in enumerate(new_jobs, 1):
        entry = (
            f"\n<b>{i}. {j['title']}</b>\n"
            f"🏢 {j['company']}\n"
            f"📍 {j['location']}\n"
            f"🕐 {j['linkedin_posted']}\n"
            f"🔗 {j['link']}\n"
        )
        if len(chunk) + len(entry) > 4000:
            post(chunk)
            time.sleep(1)
            chunk = entry
        else:
            chunk += entry
    if chunk:
        post(chunk)
    
def build_email_body(new_jobs: list[dict], group: str) -> str:
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
            f"<p><b>{len(new_jobs)} new job(s)</b> found for <b>{group}</b>.</p>"
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
        content = f"<p>No new jobs were found for <b>{group}</b> in this run.</p>"
    return f"<html><body style='font-family:sans-serif;'>{content}<p style='margin-top:20px;'><a href='{sheet_url}'>Open Spreadsheet</a></p></body></html>"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="LinkedIn Job Monitor")
    parser.add_argument("--group", required=True, help="Location group to run (e.g. india, international)")
    args = parser.parse_args()

    group = args.group.lower()
    if group not in CFG["location_groups"]:
        log.error("Unknown group '%s'. Available groups: %s", group, list(CFG["location_groups"].keys()))
        sys.exit(1)

    group_cfg = CFG["location_groups"][group]
    log.info("===== Job fetcher run started [group: %s] =====", group)
    jobs = fetch_all_jobs(group_cfg)
    jobs = filter_jobs(jobs)
    jobs = shorten_job_urls(jobs)

    try:
        worksheet = get_sheet(group)
        ensure_headers(worksheet)
        new_jobs = append_new_jobs(worksheet, jobs)
        sheet_error = None
    except Exception as exc:
        log.exception("Failed to write to Google Sheet")
        new_jobs = []
        sheet_error = str(exc)

    if CFG.get("email_on_every_run", True) or new_jobs or sheet_error:
        if sheet_error:
            subject = f"LinkedIn Jobs [{group}]: ERROR writing to Google Sheet"
            body = (
                f"<html><body style='font-family:sans-serif;'>"
                f"<h2>LinkedIn Job Fetcher — Sheet Write Failed [{group}]</h2>"
                f"<p>Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
                f"<p>{len(jobs)} job(s) fetched, but sheet write failed:</p>"
                f"<pre style='background:#f7f7f7;padding:10px;border:1px solid #ddd;white-space:pre-wrap;'>{sheet_error}</pre>"
                f"<p>Check job_fetcher.log for the full traceback.</p>"
                f"</body></html>"
            )
        else:
            subject = f"🟢 [{group}] {len(new_jobs)} new job(s) found" if new_jobs else f"LinkedIn Jobs [{group}]: run complete, no new postings"
            body = build_email_body(new_jobs, group)
        send_email(subject, body)
        send_telegram(new_jobs, group)
    else:
        log.info("email_on_every_run is false and no new jobs found — skipping email.")

    log.info("===== Job fetcher run finished [group: %s] =====", group)


if __name__ == "__main__":
    main()