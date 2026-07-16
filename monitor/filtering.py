"""Cheap, deterministic keyword-based title filtering."""

import logging

log = logging.getLogger("job_monitor")


def filter_jobs(cfg: dict, jobs: list[dict]) -> list[dict]:
    """Drop jobs whose title contains any of the configured exclude keywords
    (e.g. "Senior", "Lead", "Manager") before any AI or network calls happen.
    """
    exclude = [kw.lower() for kw in cfg.get("exclude_title_keywords", [])]
    if not exclude:
        return jobs
    filtered = [j for j in jobs if not any(kw in j["title"].lower() for kw in exclude)]
    
    removed = [j for j in jobs if any(kw in j["title"].lower() for kw in exclude)]

    if removed:
        log.info("Filtered out %d senior-level job(s):", len(removed))
        for job in removed:
            log.info("❌ %s | %s | %s", job["title"], job["company"],  job["location"])

    return filtered