"""Fetches the full job description text for a single LinkedIn posting.

The search-results endpoint (see linkedin.py) only returns title/company/
location/snippet — no description. This hits LinkedIn's public job-detail
"guest" endpoint (by job ID) to get the full text.
"""

import time
import random
import logging

import requests
from bs4 import BeautifulSoup

from monitor.linkedin import USER_AGENTS

log = logging.getLogger("job_monitor")


def fetch_job_description(job_id: str) -> str:
    """Return the plain-text job description for a LinkedIn job ID, or ""
    if it can't be fetched or parsed.
    """
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/jobs/",
        "Connection": "keep-alive",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to fetch job description for job_id=%s: %s", job_id, exc)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    desc_tag = soup.find("div", class_="description__text") or soup.find("section", class_="description")
    if not desc_tag:
        log.warning("No description found in response for job_id=%s", job_id)
        return ""
    return desc_tag.get_text(separator="\n", strip=True)


def attach_descriptions(jobs: list[dict]) -> list[dict]:
    """Fetch the full description for each job ONCE and store it as
    job["description"]. Shared by both the cold-email hook and ATS-score
    features so neither has to fetch the same page twice.

    A short randomized delay is added between requests to avoid hammering
    LinkedIn's endpoint.
    """
    for i, job in enumerate(jobs):
        job["description"] = fetch_job_description(job["job_id"])
        if i < len(jobs) - 1:
            time.sleep(random.uniform(1, 3))
    return jobs