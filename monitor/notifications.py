"""Email and Telegram notifications."""

import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

log = logging.getLogger("job_monitor")


def send_email(cfg: dict, subject: str, body_html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["email_from"]
    msg["To"] = cfg["email_to"]
    msg.attach(MIMEText(body_html, "html"))
    try:
        with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"), cfg.get("smtp_port", 587)) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["email_from"], cfg["email_app_password"])
            server.sendmail(cfg["email_from"], [cfg["email_to"]], msg.as_string())
        log.info("Notification email sent to %s", cfg["email_to"])
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


def send_telegram(cfg: dict, new_jobs: list[dict], group: str):
    tg = cfg.get("telegram", {})
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
        if cfg.get("email_on_every_run", True):
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


def build_email_body(cfg: dict, new_jobs: list[dict], group: str) -> str:
    sheet_url = f"https://docs.google.com/spreadsheets/d/{cfg['spreadsheet_id']}"
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