"""
spec_core.py
============

Pure-Python core logic for parsing and analyzing SPEC data files from
synchrotron beamline experiments (developed for the QM2 beamline, CHESS
ID4B, but works with any standard SPEC-format file).

This module has NO web-framework dependency (no FastAPI/pydantic/uvicorn) —
only pandas, numpy, scipy and the standard library — so it can be reused by
any front end. It is the logic extracted from the original
`spec_dashboard.py` browser dashboard, adapted to be called directly by a
native Tkinter GUI instead of over HTTP.

This is a read-only toolkit: it parses and analyzes experiment data but
never writes to or modifies the source files.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit


# ─────────────────────────────────────────────────────────────────────────
# SPEC file detection
# ─────────────────────────────────────────────────────────────────────────

def is_likely_spec_file(file_path: str, file_size: int = -1) -> bool:
    """
    Fast SPEC-file detector. Reads only the first 512 bytes of the file and
    counts SPEC header markers. Accepts an optional pre-fetched file_size so
    the caller can avoid a redundant stat().
    """
    try:
        if file_size < 0:
            file_size = os.path.getsize(file_path)
        if file_size == 0 or file_size > 200 * 1024 * 1024:
            return False
        with open(file_path, "rb") as f:
            head = f.read(512)
        text = head.decode("utf-8", errors="ignore")
        marker_count = sum(
            1 for line in text.splitlines()
            if line.startswith(("#F", "#E", "#D", "#S", "#L", "#C"))
        )
        return marker_count >= 2
    except (OSError, PermissionError):
        return False


# ─────────────────────────────────────────────────────────────────────────
# SPEC file parsing
# ─────────────────────────────────────────────────────────────────────────

def parse_spec_data(spec_text: str):
    """
    Parse SPEC data from text with detailed per-scan information.

    Returns: (df, available_columns, metadata, scan_info)
    """
    lines = spec_text.strip().split("\n")
    all_data: List[dict] = []
    metadata: Dict[str, Any] = {}
    scan_info: Dict[int, dict] = {}

    current_scan = None
    current_scan_info: Dict[str, Any] = {}
    columns = None

    # ── Collect global motor names (#O) and mnemonics (#o) ──────────────
    global_motor_names: List[str] = []
    global_motor_mnemonics: List[str] = []

    for line in lines:
        line_s = line.strip()
        if line_s.startswith("#S "):
            break
        if line_s.startswith("#O"):
            rest = line_s.split(None, 1)
            if len(rest) > 1:
                names = re.split(r"  +", rest[1])
                global_motor_names.extend([n.strip() for n in names if n.strip()])
        elif line_s.startswith("#o"):
            rest = line_s.split(None, 1)
            if len(rest) > 1:
                global_motor_mnemonics.extend(rest[1].split())

    metadata["motor_names"] = global_motor_names
    metadata["motor_mnemonics"] = global_motor_mnemonics

    # Extract global metadata
    for line in lines:
        line = line.strip()
        if line.startswith("#F"):
            parts = line.split()
            metadata["filename"] = parts[1] if len(parts) > 1 else "unknown"
        elif line.startswith("#E"):
            parts = line.split()
            metadata["epoch"] = parts[1] if len(parts) > 1 else ""
            if len(parts) > 1:
                try:
                    epoch_time = int(parts[1])
                    metadata["start_time"] = datetime.fromtimestamp(epoch_time).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except Exception:
                    pass
        elif line.startswith("#D"):
            metadata["date"] = line[3:].strip()
        elif line.startswith("#C"):
            metadata["comment"] = line[3:].strip()

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#S"):
            if current_scan is not None and current_scan_info:
                scan_info[current_scan] = current_scan_info.copy()

            parts = line.split()
            if len(parts) > 1:
                try:
                    current_scan = int(parts[1])
                    current_scan_info = {
                        "scan_number": current_scan,
                        "command": " ".join(parts[2:]) if len(parts) > 2 else "unknown",
                        "full_command": line,
                        "comments": [],
                        "timestamps": [],
                        "motors": {},
                        "counters": {},
                        "temperature": None,
                        "other_info": {},
                    }
                except ValueError:
                    current_scan = None

        elif line.startswith("#D") and current_scan is not None:
            current_scan_info["timestamps"].append(line[3:].strip())

        elif line.startswith("#C") and current_scan is not None:
            comment = line[3:].strip()
            current_scan_info["comments"].append(comment)
            temp_match = re.search(
                r"[Tt]emperature\s+[Ss]etpoint\s+[Aa]t\s+(\d+\.?\d*)", comment
            )
            if temp_match:
                current_scan_info["temperature"] = temp_match.group(1)

        elif line.startswith("#T") and current_scan is not None:
            parts = line.split()
            if len(parts) > 1:
                current_scan_info["count_time"] = parts[1]
                if len(parts) > 2:
                    current_scan_info["count_time_desc"] = " ".join(parts[2:])

        elif line.startswith("#G") and current_scan is not None:
            parts = line.split()
            if len(parts) > 1:
                current_scan_info["geometry"] = " ".join(parts[1:])

        elif line.startswith("#P") and current_scan is not None:
            parts = line.split()
            motor_line = parts[0][2:]
            if len(parts) > 1:
                current_scan_info[f"motors_P{motor_line}"] = " ".join(parts[1:])
                current_scan_info.setdefault("motor_positions", [])
                current_scan_info["motor_positions"].extend(
                    [float(v) if v not in ("?", "-") else None for v in parts[1:]]
                )

        elif line.startswith("#O") and current_scan is not None:
            parts = line.split()
            motor_line = parts[0][2:]
            if len(parts) > 1:
                current_scan_info[f"motor_names_O{motor_line}"] = " ".join(parts[1:])

        elif line.startswith("#J") and current_scan is not None:
            parts = line.split()
            counter_line = parts[0][2:]
            if len(parts) > 1:
                current_scan_info[f"counter_names_J{counter_line}"] = " ".join(parts[1:])

        elif line.startswith("#L"):
            column_text = line[3:].strip()
            columns = column_text.split()

            fixed_columns = []
            skip_next = False
            for j, col in enumerate(columns):
                if skip_next:
                    skip_next = False
                    continue
                if col == "VBPM" and j + 1 < len(columns):
                    next_col = columns[j + 1]
                    if next_col in ("VER", "HOR"):
                        fixed_columns.append(f"VBPM_{next_col}")
                        skip_next = True
                    else:
                        fixed_columns.append(col)
                else:
                    fixed_columns.append(col)
            columns = fixed_columns
            if current_scan is not None:
                current_scan_info["data_columns"] = columns.copy()

        elif not line.startswith("#") and line and columns and current_scan is not None:
            try:
                parts = line.split()
                values = []
                for part in parts:
                    try:
                        values.append(float(part))
                    except ValueError:
                        values.append(0.0)

                if len(values) == len(columns):
                    row = dict(zip(columns, values))
                    row["scan_number"] = current_scan
                    row["source_file"] = metadata.get("filename", "unknown")
                    all_data.append(row)
                elif len(values) > 0:
                    min_len = min(len(values), len(columns))
                    row = dict(zip(columns[:min_len], values[:min_len]))
                    row["scan_number"] = current_scan
                    row["source_file"] = metadata.get("filename", "unknown")
                    all_data.append(row)
            except Exception:
                pass

        i += 1

    if current_scan is not None and current_scan_info:
        scan_info[current_scan] = current_scan_info.copy()

    if all_data:
        df = pd.DataFrame(all_data)
        available_columns = [c for c in df.columns if c not in ("scan_number", "source_file")]
        return df, available_columns, metadata, scan_info
    return pd.DataFrame(), [], metadata, scan_info


def load_spec_file(file_path: str):
    """Read a SPEC file from disk (utf-8, falling back to latin-1) and parse it."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="latin-1") as f:
            text = f.read()

    df, columns, metadata, scan_info = parse_spec_data(text)
    metadata["filename"] = os.path.basename(file_path)
    metadata["full_path"] = file_path
    return df, columns, metadata, scan_info


SAMPLE_SPEC_DATA = """#F LuNb6Sn6
#E 1740686986
#D Thu Feb 27 15:09:46 2025
#C fourc  User = chess_id4b

#S 1  flyscan phi 0 365 3650 0.1
#D Thu Jan 29 13:44:24 2026
#C Temperature Setpoint at 300
#T 0.1  (Seconds)
#L Time  Epoch  ic1  ic2  diode  load  pilwide  cesr  pilroi  sampleT  VBPM VER  VBPM HOR  flow  pilroi6  pilroi6w  p10roi  Seconds
2.28882e-05 172.464 34363 19886 16229 9 0 14412 0 300.495 0 0 0.41 0 0 550324 0.1

#S 2  ascan th 10 20 10 0.1
#D Thu Jan 29 14:15:30 2026
#C Temperature Setpoint at 350
#T 0.1  (Seconds)
#L Time  Epoch  ic1  ic2  diode  load  pilwide  cesr  pilroi  sampleT  VBPM VER  VBPM HOR  flow  pilroi6  pilroi6w  p10roi  Seconds
1.78814e-05 216.456 34342 19846 16194 9 0 14393 0 350.495 0 0 0.41 0 0 511810 0.1

#S 3  dscan phi -5 5 20 0.5
#D Thu Jan 29 15:22:15 2026
#C Temperature Setpoint at 300
#T 0.5  (Seconds)
#L Time  Epoch  ic1  ic2  diode  load  pilwide  cesr  pilroi  sampleT  VBPM VER  VBPM HOR  flow  pilroi6  pilroi6w  p10roi  Seconds
7.86781e-06 244.882 34267 19891 16262 9 0 14380 0 300.496 0 0 0.41 0 0 456935 0.5

#S 4  mesh th 5 15 20 phi 0 10 10 0.2
#D Thu Jan 29 16:45:50 2026
#C Temperature Setpoint at 400
#T 0.2  (Seconds)
#L Time  Epoch  ic1  ic2  diode  load  pilwide  cesr  pilroi  sampleT  VBPM VER  VBPM HOR  flow  pilroi6  pilroi6w  p10roi  Seconds
1.90735e-05 280.463 34225 19836 16220 9 0 14366 0 400.497 0 0 0.41 0 0 408949 0.2"""


# ─────────────────────────────────────────────────────────────────────────
# Scan info table / motor positions
# ─────────────────────────────────────────────────────────────────────────

def build_scan_table(scan_info: Dict[int, dict], df: Optional[pd.DataFrame]) -> List[dict]:
    """Build the per-scan summary table (command, timestamp, temperature, ...)."""
    rows = []
    for scan_num, info in scan_info.items():
        row = {
            "scan_number": scan_num,
            "command": info.get("command", "unknown"),
            "timestamp": info.get("timestamps", [""])[0] if info.get("timestamps") else "",
            "temperature": info.get("temperature", ""),
            "count_time": info.get("count_time", ""),
            "comments": "; ".join(info.get("comments", [])),
            "data_points": (
                int((df["scan_number"] == scan_num).sum()) if df is not None and not df.empty else 0
            ),
        }
        motor_info = [
            f"{k}: {v}" for k, v in info.items()
            if k.startswith("motors_P") or k.startswith("geometry")
        ]
        row["motor_info"] = "; ".join(motor_info)
        rows.append(row)
    rows.sort(key=lambda r: r["scan_number"])
    return rows


def build_motor_positions(metadata: dict, scan_info: Dict[int, dict]):
    """Return (motors_meta, scans_data) — motor names/mnemonics and per-scan positions."""
    motor_names = metadata.get("motor_names", [])
    motor_mnemonics = metadata.get("motor_mnemonics", [])
    n_motors = max(len(motor_names), len(motor_mnemonics))

    motors_meta = []
    for i in range(n_motors):
        motors_meta.append({
            "index": i,
            "name": motor_names[i] if i < len(motor_names) else f"Motor {i}",
            "mnemonic": motor_mnemonics[i] if i < len(motor_mnemonics) else f"m{i}",
        })

    scans_data = []
    for scan_num in sorted(scan_info.keys()):
        info = scan_info[scan_num]
        scans_data.append({
            "scan_number": scan_num,
            "command": info.get("command", ""),
            "positions": info.get("motor_positions", []),
        })

    return motors_meta, scans_data


# ─────────────────────────────────────────────────────────────────────────
# Peak fitting
# ─────────────────────────────────────────────────────────────────────────

def fit_peak(df: pd.DataFrame, x_column: str, y_column: str, scan: int, fit_type: str = "gaussian"):
    """
    Fit a single Y column for one scan with a Gaussian or Lorentzian model.

    Returns a dict: {"stats": {...}, "fit_curve": {"x": [...], "y": [...]} or None,
                      "success": bool, "error": str (if failed)}
    """
    scan_data = df[df["scan_number"] == scan].copy().sort_values(x_column)
    if scan_data.empty:
        raise ValueError("No data for this scan")

    x = scan_data[x_column].values.astype(float)
    y = scan_data[y_column].values.astype(float)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]

    if len(x) < 4:
        raise ValueError("Not enough data points to fit")

    stats = {
        "mean": float(np.mean(y)),
        "max": float(np.max(y)),
        "min": float(np.min(y)),
        "std": float(np.std(y)),
        "delta": float(np.max(y) - np.min(y)),
        "peak_x": float(x[np.argmax(y)]),
    }

    x_fit = np.linspace(x[0], x[-1], 400)
    amp0 = float(np.max(y) - np.min(y))
    cen0 = float(x[np.argmax(y)])
    wid0 = float((x[-1] - x[0]) / 4) or 1.0
    off0 = float(np.min(y))

    try:
        if fit_type == "gaussian":
            def model(x, amp, cen, sigma, offset):
                return amp * np.exp(-((x - cen) ** 2) / (2 * sigma ** 2)) + offset

            popt, pcov = curve_fit(model, x, y, p0=[amp0, cen0, wid0, off0], maxfev=8000)
            amp, cen, sigma, offset = popt
            perr = np.sqrt(np.diag(pcov))
            amp_err, cen_err, sigma_err, offset_err = perr
            fwhm = 2.3548 * abs(sigma)
            fwhm_err = 2.3548 * sigma_err
            stats.update({
                "fit_type": "Gaussian",
                "peak_position": float(cen),
                "peak_position_err": float(cen_err),
                "fwhm": float(fwhm),
                "fwhm_err": float(fwhm_err),
                "amplitude": float(amp),
                "amplitude_err": float(amp_err),
                "sigma": float(abs(sigma)),
                "sigma_err": float(sigma_err),
                "offset": float(offset),
                "offset_err": float(offset_err),
            })
        elif fit_type == "lorentzian":
            def model(x, amp, cen, gamma, offset):
                return amp * gamma ** 2 / ((x - cen) ** 2 + gamma ** 2) + offset

            popt, pcov = curve_fit(model, x, y, p0=[amp0, cen0, wid0, off0], maxfev=8000)
            amp, cen, gamma, offset = popt
            perr = np.sqrt(np.diag(pcov))
            amp_err, cen_err, gamma_err, offset_err = perr
            fwhm = 2 * abs(gamma)
            fwhm_err = 2 * gamma_err
            stats.update({
                "fit_type": "Lorentzian",
                "peak_position": float(cen),
                "peak_position_err": float(cen_err),
                "fwhm": float(fwhm),
                "fwhm_err": float(fwhm_err),
                "amplitude": float(amp),
                "amplitude_err": float(amp_err),
                "gamma": float(abs(gamma)),
                "gamma_err": float(gamma_err),
                "offset": float(offset),
                "offset_err": float(offset_err),
            })
        else:
            raise ValueError(f"Unknown fit_type: {fit_type}")

        # Goodness-of-fit: R-squared and reduced chi-square (unweighted).
        y_pred = model(x, *popt)
        residuals = y - y_pred
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        dof = len(y) - len(popt)
        reduced_chi_square = ss_res / dof if dof > 0 else float("nan")
        stats.update({
            "r_squared": float(r_squared),
            "reduced_chi_square": float(reduced_chi_square),
        })

        y_fit = model(x_fit, *popt)
        return {
            "stats": stats,
            "fit_curve": {"x": x_fit.tolist(), "y": y_fit.tolist()},
            "residuals": {"x": x.tolist(), "y": residuals.tolist()},
            "success": True,
            "raw_x": x.tolist(),
            "raw_y": y.tolist(),
        }
    except Exception as fit_err:
        return {
            "stats": stats,
            "fit_curve": None,
            "residuals": None,
            "success": False,
            "error": str(fit_err),
            "raw_x": x.tolist(),
            "raw_y": y.tolist(),
        }


def find_actual_peak(df: pd.DataFrame, x_column: str, y_column: str, scan: int) -> Dict:
    """
    Find the peak position, peak height, and FWHM directly from the actual
    (measured) scan data — no curve fitting involved. The peak is simply the
    data point with the largest Y value; FWHM is estimated by linearly
    interpolating between adjacent raw data points to find where the signal
    crosses the half-max level on each side of the peak, so the number
    reflects what's actually in the data rather than a fitted model.

    Returns a dict:
        {"success": True, "peak_x": float, "peak_y": float,
         "fwhm": float or None}
        or {"success": False, "error": str} if there's no usable data.

    If only one side of the peak has a clear half-max crossing (e.g. the
    peak sits near the edge of the scan range), FWHM is estimated as twice
    the distance from the peak to that one crossing, assuming rough
    symmetry; if neither side crosses (e.g. flat or monotonic data), FWHM
    is reported as None rather than guessed.
    """
    scan_data = df[df["scan_number"] == scan].copy().sort_values(x_column)
    if scan_data.empty:
        return {"success": False, "error": "No data for this scan"}

    x = scan_data[x_column].values.astype(float)
    y = scan_data[y_column].values.astype(float)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    if len(x) == 0:
        return {"success": False, "error": "No valid data points"}

    imax = int(np.argmax(y))
    peak_x = float(x[imax])
    peak_y = float(y[imax])
    baseline = float(np.min(y))
    half_max = baseline + (peak_y - baseline) / 2.0

    def _crossing(direction: int):
        i = imax
        n = len(x)
        while 0 <= i + direction < n:
            j = i + direction
            y0, y1 = y[i], y[j]
            if (y0 - half_max) * (y1 - half_max) <= 0 and y1 != y0:
                frac = (half_max - y0) / (y1 - y0)
                return float(x[i] + frac * (x[j] - x[i]))
            i = j
        return None

    left = _crossing(-1)
    right = _crossing(1)
    if left is not None and right is not None:
        fwhm = abs(right - left)
    elif left is not None:
        fwhm = 2.0 * abs(peak_x - left)
    elif right is not None:
        fwhm = 2.0 * abs(right - peak_x)
    else:
        fwhm = None

    return {"success": True, "peak_x": peak_x, "peak_y": peak_y, "fwhm": fwhm}


# ─────────────────────────────────────────────────────────────────────────
# Directory browsing (local filesystem — no NFS-root sandboxing needed for
# a desktop app, unlike the web dashboard which restricts to a root_path)
# ─────────────────────────────────────────────────────────────────────────

_SKIP_EXTS = {
    ".cbf", ".tif", ".tiff", ".h5", ".hdf5", ".edf", ".mar", ".img", ".sfrm",
    ".mccd", ".nxs", ".nx", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
    ".gz", ".tar", ".bz2", ".py", ".pyc", ".so", ".o", ".c", ".cpp", ".f", ".f90",
}
_SPEC_EXTS = {".dat", ".txt", ".spec", ".scan", ".log", ".fio"}
_SPEC_NAMES = {"align", "spec", "scan", "week", "day", "run", "test"}


def list_directory(path: str) -> List[dict]:
    """
    List a directory's contents, classifying entries as 'directory',
    'spec_file' or 'other', with fast SPEC-file sniffing (mirrors the
    original web dashboard's /browse and /browse_abs endpoints, merged
    into one local-filesystem-friendly function).
    """
    path = os.path.normpath(os.path.abspath(path))
    items: List[dict] = []

    dirs_list: List[dict] = []
    specs_list: List[dict] = []
    others_list: List[dict] = []

    with os.scandir(path) as it:
        entries = sorted(it, key=lambda e: e.name)

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name.lower().endswith(".mac"):
            continue
        try:
            if entry.is_dir(follow_symlinks=True):
                dirs_list.append({
                    "name": name, "type": "directory",
                    "path": entry.path, "is_spec": False,
                })
            elif entry.is_file(follow_symlinks=True):
                st = entry.stat()
                file_size = st.st_size
                mtime = st.st_mtime
                ext = os.path.splitext(name)[1].lower()

                if ext in _SKIP_EXTS:
                    others_list.append({
                        "name": name, "type": "other", "path": entry.path,
                        "size": file_size, "is_spec": False, "_mtime": mtime,
                    })
                    continue

                is_spec_ext = ext in _SPEC_EXTS
                is_spec_name = any(p in name.lower() for p in _SPEC_NAMES)
                is_no_ext = ext == ""

                is_spec_content = False
                if not is_spec_ext and not is_spec_name and is_no_ext and file_size < 50 * 1024 * 1024:
                    is_spec_content = is_likely_spec_file(entry.path, file_size)

                is_spec = is_spec_ext or is_spec_name or is_spec_content
                rec = {
                    "name": name,
                    "type": "spec_file" if is_spec else "other",
                    "path": entry.path,
                    "size": file_size,
                    "is_spec": is_spec,
                    "_mtime": mtime,
                }
                (specs_list if is_spec else others_list).append(rec)
        except OSError:
            continue

    specs_list.sort(key=lambda x: x["_mtime"], reverse=True)
    others_list.sort(key=lambda x: x["name"])
    for e in specs_list + others_list:
        e.pop("_mtime", None)

    items.extend(dirs_list)
    items.extend(specs_list)
    items.extend(others_list)
    return items


# ─────────────────────────────────────────────────────────────────────────
# Folder timeline
# ─────────────────────────────────────────────────────────────────────────

_TIMELINE_IMAGE_EXTS = {
    ".cbf", ".tif", ".tiff", ".h5", ".hdf5", ".edf", ".mar", ".img", ".sfrm",
    ".mccd", ".nxs", ".nx",
}
_TIMELINE_SPEC_EXTS = {".dat", ".txt", ".spec", ".scan", ".log", ".fio"}
_TIMELINE_SPEC_NAMES = {"align", "spec", "scan", "week", "day", "run", "test"}

_TS_FORMATS = [
    "%a %b %d %H:%M:%S %Y",
    "%a %b  %d %H:%M:%S %Y",
]


def _parse_ts(ts_str: str) -> float:
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts_str, fmt).timestamp()
        except ValueError:
            pass
    return 0.0


def _parse_spec_for_timeline(file_path: str, filename: str) -> List[dict]:
    """Lightweight header-only SPEC parser used to build the folder timeline."""
    rows: List[dict] = []
    current = None
    data_count = 0

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("#S "):
                    if current is not None:
                        current["data_points"] = data_count
                        rows.append(current)
                    parts = line.split()
                    try:
                        snum = int(parts[1]) if len(parts) > 1 else 0
                    except ValueError:
                        snum = 0
                    current = {
                        "spec_file": filename,
                        "scan_number": snum,
                        "command": " ".join(parts[2:]) if len(parts) > 2 else "",
                        "timestamp": "",
                        "timestamp_epoch": 0.0,
                        "temperature": None,
                        "count_time": None,
                        "data_points": 0,
                        "comments": "",
                    }
                    data_count = 0
                elif current is not None:
                    if line.startswith("#D "):
                        ts = line[3:].strip()
                        current["timestamp"] = ts
                        current["timestamp_epoch"] = _parse_ts(ts)
                    elif line.startswith("#T "):
                        parts = line.split()
                        if len(parts) > 1:
                            unit = parts[2] if len(parts) > 2 else ""
                            current["count_time"] = parts[1] + (" " + unit if unit else "")
                    elif line.startswith("#C "):
                        comment = line[3:].strip()
                        m = re.search(
                            r"[Tt]emperature\s+[Ss]etpoint\s+[Aa]t\s+(\d+\.?\d*)", comment
                        )
                        if m:
                            current["temperature"] = m.group(1)
                        current["comments"] = (
                            f"{current['comments']}; {comment}" if current["comments"] else comment
                        )
                    elif not line.startswith("#") and line.strip():
                        data_count += 1
        if current is not None:
            current["data_points"] = data_count
            rows.append(current)
    except (OSError, UnicodeDecodeError):
        pass

    return rows


def folder_timeline(folder_path: str) -> List[dict]:
    """Parse every SPEC file in a folder and return all scans, newest first."""
    folder_path = os.path.normpath(os.path.abspath(folder_path))
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(folder_path)

    all_rows: List[dict] = []
    for item in sorted(os.listdir(folder_path)):
        if item.startswith(".") or item.lower().endswith(".mac"):
            continue
        item_path = os.path.join(folder_path, item)
        if not os.path.isfile(item_path):
            continue
        ext = os.path.splitext(item)[1].lower()
        no_ext = ext == ""
        if ext in _TIMELINE_IMAGE_EXTS:
            continue
        is_spec_ext = ext in _TIMELINE_SPEC_EXTS
        is_spec_name = any(p in item.lower() for p in _TIMELINE_SPEC_NAMES)
        if not (is_spec_ext or no_ext or is_spec_name):
            continue
        try:
            if not is_likely_spec_file(item_path):
                continue
        except OSError:
            continue
        all_rows.extend(_parse_spec_for_timeline(item_path, item))

    all_rows.sort(key=lambda r: r.get("timestamp_epoch", 0.0), reverse=True)
    return all_rows


# ─────────────────────────────────────────────────────────────────────────
# Scan data folder finder
# ─────────────────────────────────────────────────────────────────────────

def _walk_find(start: str, target: str, max_depth: int = 4) -> Optional[str]:
    """Bounded search using os.walk; prunes depth and skips irrelevant dirs."""
    start = os.path.normpath(start)
    base_depth = start.count(os.sep)
    skip_dirs = {".", "..", "__pycache__", "lost+found", ".git", ".svn"}

    for dirpath, dirnames, _ in os.walk(start, topdown=True):
        depth = dirpath.count(os.sep) - base_depth
        if target in dirnames:
            return os.path.join(dirpath, target)
        if depth >= max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
    return None


def find_scan_data(scan_number: int, spec_file: str, spec_parent: str) -> dict:
    """
    Locate the raw-data folder for a given scan number, mirroring the
    original dashboard's layout heuristics (raw6M/, tiffs/, rawpil/, data/).
    """
    spec_base = os.path.splitext(spec_file)[0]
    spec_parent = os.path.normpath(spec_parent)
    folder_name = f"{spec_base}_{scan_number:03d}" if spec_base else f"scan_{scan_number:03d}"

    common_subdirs = [
        "raw6M", "tiffs", "rawpil", "data", "raw", "images",
        os.path.join("raw6M", spec_base),
        os.path.join("raw6M", spec_base, "standard"),
        os.path.join("tiffs", spec_base),
    ]
    for sub in common_subdirs:
        candidate = os.path.join(spec_parent, sub, folder_name)
        if os.path.isdir(candidate):
            return {"found": True, "path": candidate, "folder": folder_name, "strategy": "direct"}

    exp_root = os.path.dirname(spec_parent)
    for sub in common_subdirs:
        candidate = os.path.join(exp_root, sub, folder_name)
        if os.path.isdir(candidate):
            return {"found": True, "path": candidate, "folder": folder_name, "strategy": "direct_up"}

    found = _walk_find(spec_parent, folder_name, max_depth=4)
    if found:
        return {"found": True, "path": found, "folder": folder_name, "strategy": "walk"}

    for base in (spec_parent, exp_root):
        tiffs_dir = os.path.join(base, "tiffs")
        if os.path.isdir(tiffs_dir):
            return {"found": False, "path": tiffs_dir, "folder": folder_name, "strategy": "tiffs_dir"}

    return {"found": False, "path": spec_parent, "folder": folder_name, "strategy": "none"}


# ─────────────────────────────────────────────────────────────────────────
# Sub-sample folder discovery
# ─────────────────────────────────────────────────────────────────────────

def _scan_subfolder_detail(sub_path: str) -> dict:
    temps: List[str] = []
    scan_count = 0
    try:
        with os.scandir(sub_path) as it1:
            children = [e for e in it1 if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")]
    except OSError:
        return {"temperatures": [], "scan_count": 0}

    for child in children:
        name = child.name
        try:
            val = int(name)
            if val >= 100:
                temps.append(name)
                try:
                    with os.scandir(child.path) as it2:
                        scan_count += sum(
                            1 for e2 in it2
                            if e2.is_dir(follow_symlinks=True) and not e2.name.startswith(".")
                        )
                except OSError:
                    pass
            else:
                scan_count += 1
        except ValueError:
            pass

    if not temps:
        scan_count = sum(1 for c in children if c.name.isdigit())

    temps_sorted = sorted(temps, key=lambda t: int(t) if t.isdigit() else t)
    return {"temperatures": temps_sorted, "scan_count": scan_count}


def spec_subfolders(folder_path: str, spec_file: str) -> dict:
    """Find sub-sample folders inside a spec file's data directory."""
    folder = os.path.normpath(folder_path)
    spec_base = os.path.splitext(spec_file)[0]
    result = {"spec_file": spec_base, "subfolders": [], "data_root": None}

    search_roots = [folder, os.path.dirname(folder)]
    common_subdirs = ["raw6M", "tiffs", "rawpil", "data", "raw", "images"]

    for root in search_roots:
        for sub in common_subdirs:
            data_dir = os.path.join(root, sub, spec_base)
            if os.path.isdir(data_dir):
                result["data_root"] = data_dir
                try:
                    with os.scandir(data_dir) as it:
                        sub_entries = sorted(
                            [e for e in it if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")],
                            key=lambda e: e.name,
                        )
                    result["subfolders"] = [
                        {"name": e.name, **_scan_subfolder_detail(e.path)} for e in sub_entries
                    ]
                except OSError:
                    pass
                return result

    return result


# ─────────────────────────────────────────────────────────────────────────
# CSV export helpers
# ─────────────────────────────────────────────────────────────────────────

def export_all(df: pd.DataFrame) -> pd.DataFrame:
    return df.copy()


def export_plotted(
    plot_data: pd.DataFrame,
    x_column: str,
    y_columns: List[str],
    scans: Optional[List[int]] = None,
) -> pd.DataFrame:
    df = plot_data
    if scans:
        df = df[df["scan_number"].isin(scans)]
    cols = ["scan_number", x_column] + y_columns
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    if x_column in out.columns:
        out = out[out[x_column].notna()]
    present_y = [c for c in y_columns if c in out.columns]
    if present_y:
        out = out[out[present_y].notna().any(axis=1)]
    return out.reset_index(drop=True)


def export_selected_scans(df: pd.DataFrame, scans: List[int], columns: Optional[List[str]] = None) -> pd.DataFrame:
    export_df = df[df["scan_number"].isin(scans)].copy()
    if columns:
        cols = ["scan_number"] + [c for c in columns if c in export_df.columns]
        export_df = export_df[cols]
    return export_df
