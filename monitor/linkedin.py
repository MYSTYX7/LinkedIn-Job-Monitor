"""Fetching and parsing of LinkedIn's public "guest" jobs search endpoint."""

import time
import random
import logging
import urllib.parse
from datetime import datetime

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("job_monitor")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
]


def build_base_url(cfg: dict, location: str, time_window_seconds: int) -> str:
    keyword_query = "(" + " OR ".join(f'"{kw}"' for kw in cfg["keywords"]) + ")"
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


def fetch_jobs_for_location(cfg: dict, location: str, time_window_seconds: int, pages_to_fetch: int) -> list[dict]:
    base_url = build_base_url(cfg, location, time_window_seconds)
    per_page = cfg.get("results_per_page", 10)
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


def fetch_all_jobs(cfg: dict, group_cfg: dict) -> list[dict]:
    locations = group_cfg["locations"]
    time_window = group_cfg["time_window_seconds"]
    pages_to_fetch = group_cfg["pages_to_fetch"]
    all_jobs = []
    seen_ids = set()
    for i, location in enumerate(locations):
        log.info("Fetching jobs for location: %s", location)
        jobs = fetch_jobs_for_location(cfg, location, time_window, pages_to_fetch)
        for job in jobs:
            if job["job_id"] not in seen_ids:
                seen_ids.add(job["job_id"])
                all_jobs.append(job)
        log.info("Fetched %d job(s) for %s (%d unique total so far).", len(jobs), location, len(all_jobs))
        if i < len(locations) - 1:
            time.sleep(random.uniform(8, 15))  # pause between locations
    log.info("Fetched %d unique job(s) total across all locations.", len(all_jobs))
    return all_jobs