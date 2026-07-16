"""Loads config.json from the project root (next to job_monitor.py)."""

import os
import json

# monitor/config.py -> monitor/ -> project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str = None) -> dict:
    """Load and return the JSON config dict.

    Args:
        path: optional override path to a config file. Defaults to
              config.json in the project root.
    """
    config_path = path or os.path.join(BASE_DIR, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)