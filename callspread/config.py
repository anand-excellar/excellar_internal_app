"""Load and persist the CallSpread tracker's config.json.

The Settings panel in the dashboard writes holdings and policy thresholds back
to this file, so it lives under the writable data directory rather than beside
the code. `default_config.json` ships with the app and seeds it on first run.
"""

import json
import shutil
import threading
from pathlib import Path

from django.conf import settings

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "default_config.json"

_lock = threading.Lock()


def config_path() -> Path:
    return Path(settings.CALLSPREAD["config_path"])


def load() -> dict:
    """Read config.json, seeding it from the shipped defaults if absent."""
    path = config_path()
    if not path.exists():
        with _lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(DEFAULT_CONFIG, path)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save(config: dict) -> dict:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        # Write-then-replace: a crash mid-write must not leave a truncated
        # config that stops the dashboard booting.
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
        tmp.replace(path)
    return config


# Keys the dashboard's Settings panel is allowed to change. Anything else in the
# file (book definition, ports, polling) is deployment configuration and is not
# editable from the browser.
POLICY_KEYS = (
    "hardFloor", "warningBuffer", "target", "upperBand",
    "coverageRatio", "coverageWarnBuffer",
    "rollDaysBeforeExpiry", "rollApproachDays",
)


def apply_patch(config: dict, patch: dict) -> dict:
    """Apply a Settings-panel patch, mirroring the original POST /api/config."""
    if patch.get("custodyCcUnits") is not None:
        config["externalHoldings"]["custodyCcUnits"] = float(patch["custodyCcUnits"])
    if patch.get("venue") is not None:
        config["externalHoldings"]["venue"] = str(patch["venue"])
    for key in POLICY_KEYS:
        if patch.get(key) is not None:
            config["policy"][key] = float(patch[key])
    # referenceSpot is allowed to clear back to null (blank field = "not set
    # yet"), so it is checked for presence rather than truthiness.
    if "referenceSpot" in patch:
        value = patch["referenceSpot"]
        config.setdefault("underlyingPnl", {})["referenceSpot"] = (
            None if value in (None, "") else float(value)
        )
    if "acquisitionCost" in patch:
        value = patch["acquisitionCost"]
        config.setdefault("underlyingPnl", {})["acquisitionCost"] = (
            0.0 if value in (None, "") else float(value)
        )
    return config