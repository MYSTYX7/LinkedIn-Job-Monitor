"""Shared logging configuration for the job monitor."""

import os
import sys
import logging


def setup_logging(base_dir: str, logger_name: str = "job_monitor") -> logging.Logger:
    """Configure root logging (file + stdout) and return a named logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(base_dir, "job_fetcher.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(logger_name)