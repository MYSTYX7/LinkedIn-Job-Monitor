"""Scores each job description against the user's resume — an "ATS Score"
out of 10 — using a Qwen-compatible chat API. Written to the Google Sheet
only ("ATS Score" column); the cell's background color (green/yellow/red)
is applied in monitor/sheets.py based on the score.

Fails open by design: if the resume can't be loaded, the job has no
description, the API key is missing, or the request fails, the score is
left as None (a blank, uncolored cell) rather than blocking the row from
being written.
"""

import re
import logging

import requests

from monitor.resume import RESUME_SUMMARY

log = logging.getLogger("job_monitor")

# _resume_text_cache: dict[str, str] = {}


def _load_resume_text(cfg: dict) -> str:
    return RESUME_SUMMARY


def _chat(cfg: dict, system_prompt: str, user_prompt: str) -> str | None:
    """Low-level helper: one chat completion call. Returns None on any failure."""
    resume_cfg = cfg.get("resume_match", {})
    if not resume_cfg.get("enabled", False):
        return None

    api_key = resume_cfg.get("api_key", "")
    if not api_key:
        log.warning("resume_match is enabled but 'resume_match.api_key' is missing from config; skipping.")
        return None

    base_url = resume_cfg.get("base_url", "https://qwen.aikit.club/v1").rstrip("/")
    model = resume_cfg.get("model", "qwen3.7-plus")

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                # "max_tokens": 10,
            },
            timeout=resume_cfg.get("timeout_seconds", 30),
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log.warning("Resume match request failed: %s", exc)
        return None


def _parse_score(text: str | None) -> int | None:
    """Extract an integer 0-100 from the model's reply. Returns None if it
    can't find one (fail open rather than guessing).
    """
    if not text:
        return None
    match = re.search(r"\b(100|[1-9]?\d)\b", text)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def score_job(cfg: dict, job: dict, resume_text: str) -> int | None:
    """Ask the model to strictly score resume-vs-job-description fit, 0-100."""
    description = job.get("description", "")
    if not resume_text or not description:
        return None

    system_prompt = (
        "You are a strict ATS (Applicant Tracking System) resume screener. "
        "Compare the candidate's resume against the job description and "
        "score, on a scale of 0 to 100, how well the resume matches the "
        "job's required skills, tools, responsibilities, and seniority "
        "level. 100 means an excellent match; 0 means completely unrelated. "
        "Be strict and realistic — do not default to the middle of the "
        "range just to be safe. Reply with ONLY the integer score and "
        "nothing else — no words, no explanation, no punctuation."
    )
    user_prompt = (
        f"Job Title: {job['title']}\n"
        f"Company: {job['company']}\n"
        f"Job Description:\n{description[:4000]}\n\n"
        f"Candidate Resume:\n{resume_text[:4000]}"
    )
    return _parse_score(_chat(cfg, system_prompt, user_prompt))


def score_jobs(cfg: dict, jobs: list[dict]) -> list[dict]:
    """Attach an 'ats_score' field (int 0-100, or None) to every job."""
    resume_cfg = cfg.get("resume_match", {})
    if not resume_cfg.get("enabled", False):
        for job in jobs:
            job["ats_score"] = None
        return jobs

    resume_text = _load_resume_text(cfg)
    if not resume_text:
        log.warning("resume_match is enabled but the resume could not be loaded; skipping ATS scoring for this run.")
        for job in jobs:
            job["ats_score"] = None
        return jobs

    for job in jobs:
        job["ats_score"] = score_job(cfg, job, resume_text)
        if job["ats_score"] is not None:
            log.info("ATS score %s/100 for %s @ %s", job["ats_score"], job["title"], job["company"])
    return jobs