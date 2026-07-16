"""TinyURL-based link shortening."""

import time
import logging
import requests

log = logging.getLogger("job_monitor")


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


def shorten_job_urls(cfg: dict, jobs: list[dict]) -> list[dict]:
    if not cfg.get("shorten_urls", False):
        return jobs
    log.info("Shortening URLs for %d job(s) via TinyURL...", len(jobs))
    for job in jobs:
        job["link"] = shorten_url(job["link"])
        time.sleep(0.3)
    return jobs