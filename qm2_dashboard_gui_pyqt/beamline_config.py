"""
beamline_config.py — load the beamline YAML config.

Everything that differs between beamlines (titles, data-folder layout, live
signal channels and PVs, Summary-tab layout, plot defaults, detector frame
type, ...) lives in one YAML file under configs/, so the same dashboard code
can run at QM2 (ID4B), ID3A, or elsewhere. See configs/qm2.yaml for the full,
commented reference.
"""

import os
from typing import Dict, Optional

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "id3a.yaml")

# Fixed tab keys the code refers to, with the label used when the config
# doesn't give one. The config can relabel, reorder or hide these, but not
# add new ones.
TAB_LABELS: Dict[str, str] = {
    "home": "Home",
    "summary": "Overall Summary",
    "plot": "SPEC Plot",
    "live_image": "Live Image",
    "timeline": "Folder Timeline",
    "scan_info": "Scan Info",
    "motor_positions": "Motor Positions",
    "export": "Export",
    "ion_flux": "Ion Chamber Flux",
    "slack_alerts": "Slack Alerts",
}

REQUIRED_SECTIONS = (
    "beamline", "app", "tabs", "signals", "summary", "plot", "timeline",
    "data_layout", "spec_parsing", "live_image", "slack",
)


class ConfigError(ValueError):
    """The beamline config file is missing something or is inconsistent."""


def load_config(path: Optional[str] = None) -> Dict:
    """Read and check a beamline YAML config. Returns the parsed dict with
    `tabs` normalized to one {"key", "label", "description", "show"} entry
    per known tab (tabs the file doesn't list are appended, shown, at the
    end) and `_path` set to the file's absolute path."""
    path = os.path.abspath(path or DEFAULT_CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ConfigError(f"{path}: missing section(s): {', '.join(missing)}")
    for section in REQUIRED_SECTIONS:
        if cfg[section] is None:
            cfg[section] = [] if section == "tabs" else {}

    channels = cfg["signals"].get("channels") or {}
    cfg["signals"]["channels"] = channels

    tabs = []
    seen = set()
    for entry in cfg["tabs"]:
        if isinstance(entry, str):
            entry = {"key": entry}
        key = entry.get("key")
        if key not in TAB_LABELS:
            raise ConfigError(
                f"{path}: unknown tab key {key!r} (known: {', '.join(TAB_LABELS)})"
            )
        if key in seen:
            raise ConfigError(f"{path}: tab {key!r} is listed twice")
        seen.add(key)
        tabs.append({
            "key": key,
            "label": entry.get("label") or TAB_LABELS[key],
            "description": entry.get("description") or "",
            "show": bool(entry.get("show", True)),
        })
    for key, label in TAB_LABELS.items():
        if key not in seen:
            tabs.append({"key": key, "label": label, "description": "", "show": True})
    cfg["tabs"] = tabs

    groups = cfg["summary"].get("groups") or []
    cfg["summary"]["groups"] = groups
    for group in groups:
        for canonical in group.get("channels") or []:
            if canonical not in channels:
                raise ConfigError(
                    f"{path}: summary group {group.get('title')!r} lists channel "
                    f"{canonical!r}, which is not defined under signals.channels"
                )

    no_beam = cfg["signals"].get("no_beam") or {}
    cfg["signals"]["no_beam"] = no_beam
    if no_beam.get("channel") and no_beam["channel"] not in channels:
        raise ConfigError(
            f"{path}: signals.no_beam.channel {no_beam['channel']!r} is not "
            "defined under signals.channels"
        )

    cfg["_path"] = path
    return cfg
