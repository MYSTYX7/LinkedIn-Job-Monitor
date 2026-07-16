"""
LinkedIn Job Posting Automation
--------------------------------
Fetches new job postings from LinkedIn's public "guest" jobs search endpoint,
dedups against the Google Sheet, then (for genuinely new postings only)
fetches each job's full description, generates a Qwen-powered cold-email
hook, and scores resume-vs-job-description ATS compatibility out of 10 —
both written to the Google Sheet only (never to email/Telegram). Emails/
Telegrams a summary notification when it runs.

Settings are read from config.json (next to this script).
Designed to be triggered by Windows Task Scheduler with a --group argument:

    python job_monitor.py --group india           (every 2 hours)
    python job_monitor.py --group international   (every 6 hours)

This file is intentionally thin — all logic lives in the `monitor` package
next to it. See monitor/ for: linkedin.py, filtering.py, job_description.py,
cold_email_hook.py, resume_match.py, shortener.py, sheets.py,
notifications.py, config.py, logging_setup.py.
"""

import sys
import argparse
from datetime import datetime

from monitor.config import load_config, BASE_DIR
from monitor.logging_setup import setup_logging
from monitor.linkedin import fetch_all_jobs
from monitor.filtering import filter_jobs
from monitor.job_description import attach_descriptions
from monitor.resume_match import score_jobs
from monitor.shortener import shorten_job_urls
from monitor.sheets import get_sheet, ensure_headers, get_new_jobs, append_jobs
from monitor.notifications import send_email, send_telegram, build_email_body

log = setup_logging(BASE_DIR)


def run(cfg: dict, group: str):
    if group not in cfg["location_groups"]:
        log.error("Unknown group '%s'. Available groups: %s", group, list(cfg["location_groups"].keys()))
        sys.exit(1)

    group_cfg = cfg["location_groups"][group]
    log.info("===== Job fetcher run started [group: %s] =====", group)

    jobs = fetch_all_jobs(cfg, group_cfg)
    jobs = filter_jobs(cfg, jobs)

    try:
        worksheet = get_sheet(cfg, BASE_DIR, group)
        ensure_headers(worksheet)
        new_jobs = get_new_jobs(worksheet, jobs)          # dedup FIRST — cheap, one sheet read
        new_jobs = attach_descriptions(new_jobs)           # fetch each new job's full description ONCE
        new_jobs = score_jobs(cfg, new_jobs)               # ATS score vs resume, sheet only
        new_jobs = shorten_job_urls(cfg, new_jobs)         # only shorten links for genuinely new jobs
        new_jobs = append_jobs(worksheet, new_jobs)
        sheet_error = None
    except Exception as exc:
        log.exception("Failed to write to Google Sheet")
        new_jobs = []
        sheet_error = str(exc)

    if cfg.get("email_on_every_run", True) or new_jobs or sheet_error:
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
            body = build_email_body(cfg, new_jobs, group)
        send_email(cfg, subject, body)
        send_telegram(cfg, new_jobs, group)
    else:
        log.info("email_on_every_run is false and no new jobs found — skipping email.")

    log.info("===== Job fetcher run finished [group: %s] =====", group)


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Job Monitor")
    parser.add_argument("--group", required=True, help="Location group to run (e.g. india, international)")
    args = parser.parse_args()

    cfg = load_config()
    run(cfg, args.group.lower())


if __name__ == "__main__":
    main()