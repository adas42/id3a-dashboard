"""The "Map Queue" tab: watch and steer the mapqueue runner.

It talks only to the runner's HTTP API, through mapqueue's
standard-library client, imported from the mapqueue checkout named in
the beamline YAML's `map_queue` section. Every request runs in a worker
thread, so the UI never waits on the network. The runner is the only
thing that changes the queue: a drag-and-drop or a button asks it to,
and the table shows the result on the next fetch.

Besides the table: a status banner (state, progress, the spec command
running now, a rough time left), a map of one sample's points in the
x-z plane with the stage limits and the beam position, a timeline of
every attempt, and a readable view of the selected point.
"""

import html
import json
import os
import queue as job_queue
import statistics
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg  # after PySide6, so it binds to PySide6

# Points that can be moved or removed; the runner refuses the others.
EDITABLE = ("pending", "blocked")
STATUSES = ("pending", "blocked", "running", "done", "failed")
COLUMNS = [
    ("id", "Point"), ("status", "Status"), ("sample", "Sample"),
    ("dataset_id", "Dataset"), ("scan", "Scan"), ("mode", "Mode"),
    ("attempts", "Attempts"), ("scan_n", "Scan #"), ("problems", "Problems"),
]
MIME_TYPE = "application/x-mapqueue-ids"

# The dashboard's dark palette (spec_dashboard_qt.THEMES["dark"]), used
# until the dashboard passes the active one (MapQueueTab.apply_theme).
DEFAULT_THEME = {
    "BG_PANEL": "#242534", "BORDER": "#3d3f57", "TEXT_PRIMARY": "#e8e8f2",
    "TEXT_SECONDARY": "#a0a3ba", "ACCENT": "#5ec8f8",
    "PRESSED_TEXT": "#0a0a0f",
    "PLOT_COLORS": ["#5ec8f8", "#ffb74d", "#81c995", "#f28b82", "#c58af9"],
}

# The runner's state -> the status color it's shown in.
STATE_COLORS = {
    "running": "running", "done": "running", "idle": "done",
    "holding": "blocked", "held": "blocked", "aborted": "aborted",
    "paused": "pending", "needs-ok": "failed", "needs-restart-ok": "failed",
    "failed": "failed", "runner not reachable": "failed",
}

# Seconds per point besides counting (moves, slits, file writing), until
# a point is done and the real overhead is known.
DEFAULT_OVERHEAD_S = 15.0


def status_colors(theme: Dict) -> Dict[str, str]:
    """One color per point status and attempt outcome, from the theme."""
    plot = theme["PLOT_COLORS"]
    return {"pending": theme["TEXT_SECONDARY"], "running": theme["ACCENT"],
            "done": plot[2], "blocked": plot[1], "aborted": plot[1],
            "failed": plot[3]}


# ----------------------------------------------------------------------
# Where a move goes (pure; "before" is a point id, or None for the end)
# ----------------------------------------------------------------------
def _editable_ids(items: List[Dict]) -> List[str]:
    return [item["id"] for item in items if item["status"] in EDITABLE]


def move_to_top(items: List[Dict], ids: List[str]):
    """(should move, before) to put ``ids`` ahead of every other waiting
    point."""
    others = [i for i in _editable_ids(items) if i not in ids]
    return (True, others[0]) if others else (False, None)


def move_up(items: List[Dict], ids: List[str]):
    """(should move, before) to move ``ids`` one waiting point earlier."""
    order = _editable_ids(items)
    first = min(order.index(i) for i in ids)
    return (True, order[first - 1]) if first > 0 else (False, None)


def move_down(items: List[Dict], ids: List[str]):
    """(should move, before) to move ``ids`` one waiting point later."""
    order = _editable_ids(items)
    last = max(order.index(i) for i in ids)
    if last + 1 >= len(order):
        return False, None
    return True, order[last + 2] if last + 2 < len(order) else None


def drop_before(rows: List[Dict], row: int, ids: List[str]) -> Optional[str]:
    """The point a drop at table row ``row`` goes in front of (None for
    the end), skipping the points being moved."""
    for item in rows[row:]:
        if item["id"] not in ids:
            return item["id"]
    return None


# ----------------------------------------------------------------------
# Time left, map geometry, timeline bars (pure)
# ----------------------------------------------------------------------
def parse_time(text) -> Optional[float]:
    """Seconds since the epoch from the runner's ISO timestamps."""
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def expected_scan_s(params: Dict) -> float:
    """Seconds a point spends counting: count_time per point, times the
    fly (and step) points; a tseries counts once."""
    seconds = params.get("count_time") or 0.0
    for name in ("fly", "step"):
        group = params.get(name)
        if isinstance(group, dict) and group.get("npts"):
            seconds *= group["npts"]
    return seconds


def overhead_s(items: List[Dict]) -> float:
    """Median seconds per done attempt beyond its counting time."""
    extra = []
    for item in items:
        for attempt in item.get("history") or []:
            start = parse_time(attempt.get("started"))
            end = parse_time(attempt.get("finished"))
            if attempt.get("outcome") == "done" and None not in (start, end):
                extra.append(max(0.0, end - start
                                 - expected_scan_s(item.get("params") or {})))
    return statistics.median(extra) if extra else DEFAULT_OVERHEAD_S


def time_left_s(items: List[Dict], now: float) -> Optional[float]:
    """Rough seconds until the pending and running points are done, or
    None if there are none. Blocked points don't count: they can't run."""
    overhead = overhead_s(items)
    total, left = 0.0, False
    for item in items:
        if item["status"] not in ("pending", "running"):
            continue
        left = True
        each = expected_scan_s(item.get("params") or {}) + overhead
        started = parse_time(item.get("started"))
        if item["status"] == "running" and started is not None:
            each = max(0.0, each - (now - started))
        total += each
    return total if left else None


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.1f} s"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 10:
        return f"{minutes} min {seconds % 60:02d} s"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def point_xz(params: Dict):
    """A point on the map: (x, z). The beam's width is along x and its
    height along z. An axis the stack doesn't have (left out) is 0."""
    position = params.get("position") or {}
    return position.get("x", 0.0), position.get("z", 0.0)


def scan_extent(params: Dict):
    """(x0, x1, z0, z1): what the beam covers for a point, i.e. the beam
    size stretched over the fly (and step) range on x or z."""
    centre = dict(zip(("x", "z"), point_xz(params)))
    beam = params.get("beam") or {}
    half = {"x": beam.get("width", 0.0) / 2, "z": beam.get("height", 0.0) / 2}
    low = {a: centre[a] - half[a] for a in centre}
    high = {a: centre[a] + half[a] for a in centre}
    for name in ("fly", "step"):
        group = params.get(name)
        if isinstance(group, dict) and group.get("axis") in centre:
            axis = group["axis"]
            ends = sorted((centre[axis] + group["start"],
                           centre[axis] + group["end"]))
            low[axis], high[axis] = ends[0] - half[axis], ends[1] + half[axis]
    return low["x"], high["x"], low["z"], high["z"]


def fly_segment(params: Dict):
    """The fly line ((x0, z0), (x1, z1)), if the point flies along x or z."""
    fly = params.get("fly")
    if not isinstance(fly, dict) or fly.get("axis") not in ("x", "z"):
        return None
    x, z = point_xz(params)
    if fly["axis"] == "x":
        return (x + fly["start"], z), (x + fly["end"], z)
    return (x, z + fly["start"]), (x, z + fly["end"])


def beam_on_map(stage: Dict):
    """Where the beam is on the map now, (x, z), from the motor positions:
    XM plus XF when XF is a separate fly-only stage (homed at 0), and Z
    plus ZF likewise. None while a position isn't known."""
    roles, positions = stage.get("roles") or {}, stage.get("positions") or {}
    coords = []
    for position_role, fly_role in (("XM", "XF"), ("Z", "ZF")):
        mne, fly = roles.get(position_role), roles.get(fly_role)
        value = 0.0
        for motor in ((mne,) if fly in (None, "none", mne) else (mne, fly)):
            if motor in (None, "none"):
                continue
            if motor not in positions or positions[motor] is None:
                return None
            value += positions[motor]
        coords.append(value)
    return tuple(coords)


def stage_frame(stage: Dict):
    """(x0, x1, z0, z1): the XM and Z user limits, or None if unknown."""
    roles, limits = stage.get("roles") or {}, stage.get("limits") or {}
    x, z = limits.get(roles.get("XM")), limits.get(roles.get("Z"))
    if not x or not z:
        return None
    return x[0], x[1], z[0], z[1]


def attempt_bars(items: List[Dict], now: float):
    """One bar per attempt, by start time: (id, sample, start, end,
    outcome). The running attempt ends now, with outcome "running"."""
    bars = []
    for item in items:
        sample = (item.get("params") or {}).get("sample")
        for attempt in item.get("history") or []:
            start = parse_time(attempt.get("started"))
            end = parse_time(attempt.get("finished"))
            if start is None:
                continue
            if end is None:
                if item["status"] != "running":
                    continue
                end, outcome = max(now, start), "running"
            else:
                outcome = attempt.get("outcome")
            bars.append((item["id"], sample, start, end, outcome))
    return sorted(bars, key=lambda bar: bar[2])


def _num(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def point_html(item: Dict, theme: Dict) -> str:
    """The selected point as HTML: what it scans, where, and its history."""
    colors = status_colors(theme)
    muted = theme["TEXT_SECONDARY"]
    params = item.get("params") or {}
    esc = html.escape

    def pill(text, key):
        return (f"<span style='color:{colors.get(key, theme['TEXT_PRIMARY'])};"
                f"font-weight:bold'>{esc(str(text))}</span>")

    rows = []

    def row(label, value):
        rows.append(f"<tr><td style='color:{muted};padding-right:14px'>"
                    f"{esc(label)}</td><td>{value}</td></tr>")

    row("Sample", esc(f"{params.get('sample')} · dataset "
                      f"{params.get('dataset_id')} · {params.get('mode')} mode"))
    position = params.get("position") or {}
    where = [f"{axis} {_num(position[axis])}" for axis in ("x", "y", "z")
             if axis in position]
    if "ome_ref" in position or "ome_meas" in position:
        ref, meas = position.get("ome_ref", 0.0), position.get("ome_meas", 0.0)
        where.append(f"ω {_num(ref)} + {_num(meas)} = {_num(ref + meas)}")
    row("Position", esc(",  ".join(where)))
    scan, count = params.get("scan"), params.get("count_time")
    fly = params.get("fly")
    if isinstance(fly, dict):
        text = (f"{scan} on {fly['axis']} {fly['start']:+g} → {fly['end']:+g} "
                f"({fly['npts']} points, {_num(count)} s each)")
    else:
        text = f"{scan}, {_num(count)} s"
    row("Scan", esc(text))
    step = params.get("step")
    if isinstance(step, dict):
        row("Step", esc(f"{step['axis']} {step['start']:+g} → "
                        f"{step['end']:+g} ({step['npts']} points)"))
    if params.get("bin_axis"):
        row("Bin axis", esc(str(params["bin_axis"])))
    row("Counting", esc(f"≈ {fmt_duration(expected_scan_s(params))}"))
    beam = params.get("beam")
    if isinstance(beam, dict):
        row("Beam", esc(f"{_num(beam.get('width'))} × "
                        f"{_num(beam.get('height'))} mm, atten "
                        f"{_num(beam.get('atten'))}"))
    row("Source", esc(f"{item.get('source')}, added {item.get('added')}"))
    meta = params.get("meta") or {}
    if meta.get("note"):
        row("Note", esc(str(meta["note"])))
    legacy = meta.get("legacy")
    if isinstance(legacy, dict):
        row("Imported from", esc(f"{legacy.get('file')} line "
                                 f"{legacy.get('line')}"))
    problems = item.get("problems") or []
    if problems:
        row("Problems", "<br>".join(pill(p, "failed") for p in problems))

    history = item.get("history") or []
    lines = []
    for attempt in history:
        start = parse_time(attempt.get("started"))
        end = parse_time(attempt.get("finished"))
        took = fmt_duration(end - start) if None not in (start, end) else "…"
        outcome = attempt.get("outcome") or "running"
        started = (datetime.fromtimestamp(start).strftime("%m-%d %H:%M:%S")
                   if start is not None else "")
        notes = "; ".join(attempt.get("problems") or [])
        lines.append(
            f"<tr><td>{attempt.get('attempt')}</td><td>{started}</td>"
            f"<td>{took}</td><td>{pill(outcome, outcome)}</td>"
            f"<td>{_num(attempt.get('scan_n')) if attempt.get('scan_n') is not None else ''}</td>"
            f"<td>{esc(notes)}</td></tr>")
    head = "".join(f"<th align='left' style='color:{muted};padding-right:12px'>"
                   f"{h}</th>" for h in ("#", "Started", "Took", "Outcome",
                                         "Scan #", "Notes"))
    history_html = (f"<table cellspacing='0' cellpadding='2'><tr>{head}</tr>"
                    + "".join(lines) + "</table>" if lines
                    else f"<p style='color:{muted}'>not run yet</p>")
    return (f"<h3 style='margin:0'>{esc(item['id'])} &nbsp;"
            f"{pill(item['status'], item['status'])}</h3>"
            f"<table cellspacing='0' cellpadding='2'>{''.join(rows)}</table>"
            f"<h4 style='margin-bottom:2px'>History</h4>{history_html}")


# ----------------------------------------------------------------------
# Worker thread: polling and actions, off the UI thread
# ----------------------------------------------------------------------
class QueueWorker(QtCore.QThread):
    """Polls /status every ``poll_s`` and fetches /queue when its version
    or the runner instance changes; runs submitted actions in between."""

    statusChanged = QtCore.Signal(object)    # the /status dict
    queueChanged = QtCore.Signal(object)     # the list of points
    unreachable = QtCore.Signal(str)
    actionDone = QtCore.Signal(str, object)  # what, the API's answer
    actionFailed = QtCore.Signal(str, object)  # what, list of reasons

    def __init__(self, client, api_error, poll_s: float):
        super().__init__()
        self._client, self._api_error = client, api_error
        self._poll_s = max(0.2, poll_s)
        self._jobs: "job_queue.Queue" = job_queue.Queue()
        self._running = True
        self._seen = None  # (instance, version) of the last fetched queue

    def submit(self, what: str, method: str, *args):
        self._jobs.put((what, method, args))

    def stop(self):
        self._running = False
        self._jobs.put(None)

    def run(self):
        while self._running:
            try:
                job = self._jobs.get(timeout=self._poll_s)
            except job_queue.Empty:
                job = None
            if job is not None:
                self._do(*job)
            if self._running:
                self._poll()

    def _do(self, what, method, args):
        try:
            result = getattr(self._client, method)(*args)
        except self._api_error as exc:
            self.actionFailed.emit(what, list(exc.errors))
            return
        except OSError as exc:
            self.actionFailed.emit(what, [f"can't reach the runner: {exc}"])
            return
        self._seen = None  # fetch the queue again right away
        self.actionDone.emit(what, result)

    def _poll(self):
        try:
            status = self._client.status()
            key = (status.get("instance"), status.get("version"))
            if key != self._seen:
                self.queueChanged.emit(self._client.points())
                self._seen = key
        except (self._api_error, OSError) as exc:
            self._seen = None
            self.unreachable.emit(str(exc))
            return
        self.statusChanged.emit(status)


# ----------------------------------------------------------------------
# Table model with drag-and-drop
# ----------------------------------------------------------------------
def _cell(item: Dict, key: str) -> str:
    params = item.get("params") or {}
    if key in ("sample", "dataset_id", "scan", "mode"):
        value = params.get(key)
    elif key == "problems":
        value = "; ".join(item.get("problems") or [])
    else:
        value = item.get(key)
    return "" if value is None else str(value)


class QueueModel(QtCore.QAbstractTableModel):
    """All points in queue order, filtered by status. Dragging waiting
    points and dropping them asks the runner to move them."""

    moveRequested = QtCore.Signal(list, object)  # ids, before id or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: List[Dict] = []
        self._rows: List[Dict] = []
        self._status: Optional[str] = None
        self._hide_done = False
        self.colors = status_colors(DEFAULT_THEME)

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._all = list(items)
        self._refilter()
        self.endResetModel()

    def set_filter(self, status: Optional[str], hide_done: bool):
        self.beginResetModel()
        self._status, self._hide_done = status, hide_done
        self._refilter()
        self.endResetModel()

    def set_colors(self, colors: Dict[str, str]):
        self.colors = colors
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1,
                                             len(COLUMNS) - 1))

    def _refilter(self):
        self._rows = [item for item in self._all
                      if (self._status is None or item["status"] == self._status)
                      and not (self._hide_done and item["status"] == "done")]

    def all_items(self) -> List[Dict]:
        return self._all

    def item(self, row: int) -> Dict:
        return self._rows[row]

    def row_of(self, item_id: str) -> int:
        for row, item in enumerate(self._rows):
            if item["id"] == item_id:
                return row
        return -1

    # Qt model interface

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if orientation == QtCore.Qt.Horizontal and role == QtCore.Qt.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        item, key = self._rows[index.row()], COLUMNS[index.column()][0]
        if role == QtCore.Qt.DisplayRole:
            return _cell(item, key)
        if role == QtCore.Qt.ToolTipRole and item.get("problems"):
            return "\n".join(item["problems"])
        # Pending rows keep the normal text color; the rest are tinted.
        if (role == QtCore.Qt.ForegroundRole and item["status"] != "pending"
                and item["status"] in self.colors):
            return QtGui.QBrush(QtGui.QColor(self.colors[item["status"]]))
        return None

    def flags(self, index):
        if not index.isValid():
            return QtCore.Qt.ItemIsDropEnabled
        flags = super().flags(index) | QtCore.Qt.ItemIsDropEnabled
        if self._rows[index.row()]["status"] in EDITABLE:
            flags |= QtCore.Qt.ItemIsDragEnabled
        return flags

    def supportedDropActions(self):
        return QtCore.Qt.MoveAction

    def supportedDragActions(self):
        return QtCore.Qt.MoveAction

    def mimeTypes(self):
        return [MIME_TYPE]

    def mimeData(self, indexes):
        rows = sorted({index.row() for index in indexes})
        ids = [self._rows[r]["id"] for r in rows
               if self._rows[r]["status"] in EDITABLE]
        data = QtCore.QMimeData()
        data.setData(MIME_TYPE, json.dumps(ids).encode())
        return data

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(MIME_TYPE)

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat(MIME_TYPE):
            return False
        ids = json.loads(bytes(data.data(MIME_TYPE)).decode())
        if not ids:
            return False
        if row == -1:  # dropped on a row (goes in front of it) or past the end
            row = parent.row() if parent.isValid() else len(self._rows)
        self.moveRequested.emit(ids, drop_before(self._rows, row, ids))
        # False: the view must not remove rows itself; the runner moves the
        # points and the next fetch shows the new order.
        return False


# ----------------------------------------------------------------------
# Status banner
# ----------------------------------------------------------------------
class StatusBanner(QtWidgets.QFrame):
    """The runner's state as a colored pill, then where it is, a progress
    bar, the spec command running now and for how long, and time left."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.state = QtWidgets.QLabel("connecting…")
        font = self.state.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.state.setFont(font)
        layout.addWidget(self.state)
        self.where = QtWidgets.QLabel("")
        layout.addWidget(self.where)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedWidth(200)
        self.progress.setFormat("%v / %m done")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.command = QtWidgets.QLabel("")
        self.command.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.command.setMinimumWidth(120)
        layout.addWidget(self.command, 1)
        self.eta = QtWidgets.QLabel("")
        layout.addWidget(self.eta)
        self._theme = DEFAULT_THEME

    def set_theme(self, theme: Dict):
        self._theme = theme

    def set_state(self, state: str, color_key: Optional[str] = None):
        theme = self._theme
        color = status_colors(theme)[color_key
                                     or STATE_COLORS.get(state, "pending")]
        self.state.setText(state)
        self.state.setStyleSheet(
            f"QLabel {{ background: {color}; color: {theme['PRESSED_TEXT']};"
            f" border-radius: 7px; padding: 3px 12px; }}")

    def update_from(self, status: Dict, items: List[Dict], now: float):
        if not status:
            self.where.setText("")
            self.command.setText("")
            self.eta.setText("")
            return
        state = status.get("state", "")
        if status.get("paused"):
            # Paused wins over whatever the last pass was.
            self.set_state(f"paused: {status.get('pause_reason') or 'paused'}",
                           "pending")
        else:
            self.set_state(state)
        self.where.setText(f"{status.get('current_sample') or '—'}  ·  "
                           f"{status.get('current_mode') or 'no mode'}  ·  "
                           f"{status.get('running') or 'nothing running'}")
        counts = status.get("counts") or {}
        total = sum(counts.values())
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(counts.get("done", 0))
        self.progress.setToolTip(", ".join(f"{n} {s}"
                                           for s, n in sorted(counts.items())))
        command, started = status.get("command"), status.get("command_started")
        if command:
            took = fmt_duration(now - started) if started else ""
            self.command.setText(f"spec ▸ {command}   {took}")
            self.command.setToolTip(command)
        else:
            self.command.setText("")
        left = time_left_s(items, now)
        blocked = counts.get("blocked", 0)
        text = f"≈ {fmt_duration(left)} left" if left is not None else "queue empty"
        if blocked:
            text += f" (+{blocked} blocked)"
        self.eta.setText(text)
        self.eta.setToolTip("Counting time of each waiting point, plus the "
                            "median overhead of the points done so far "
                            f"({fmt_duration(overhead_s(items))} per point).")


# ----------------------------------------------------------------------
# Map and timeline
# ----------------------------------------------------------------------
def _style_plot(plot: pg.PlotWidget, theme: Dict):
    plot.setBackground(theme["BG_PANEL"])
    for name in ("bottom", "left"):
        axis = plot.getAxis(name)
        axis.setPen(pg.mkPen(theme["TEXT_SECONDARY"]))
        axis.setTextPen(pg.mkPen(theme["TEXT_PRIMARY"]))


class PointMap(QtWidgets.QWidget):
    """One sample's points in the x-z plane: markers colored by status,
    fly ranges as lines, the selected point's beam footprint, the XM/Z
    limits as a dashed frame, and the beam's position now as a cross."""

    pointClicked = QtCore.Signal(str)
    FOLLOW = "current sample"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Map")
        title.setStyleSheet("font-weight: bold")
        top.addWidget(title)
        self.sample = QtWidgets.QComboBox()
        self.sample.addItem(self.FOLLOW)
        self.sample.currentTextChanged.connect(lambda _: self._redraw(fit=True))
        top.addWidget(self.sample)
        self.legend = QtWidgets.QLabel("")
        self.legend.setProperty("secondaryText", True)
        top.addWidget(self.legend, 1)
        fit = QtWidgets.QPushButton("Fit")
        fit.setToolTip("Zoom to this sample's points")
        fit.clicked.connect(lambda: self._redraw(fit=True))
        top.addWidget(fit)
        layout.addLayout(top)

        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "x (mm)")
        self.plot.setLabel("left", "z (mm)")
        layout.addWidget(self.plot, 1)
        self.frame = pg.PlotDataItem()
        self.plot.addItem(self.frame, ignoreBounds=True)
        self.lines = pg.PlotDataItem(connect="pairs")
        self.plot.addItem(self.lines)
        self.points = pg.ScatterPlotItem(hoverable=True, tip=self._tip)
        self.points.sigClicked.connect(self._clicked)
        self.plot.addItem(self.points)
        self.footprint = QtWidgets.QGraphicsRectItem()
        self.footprint.setVisible(False)
        self.plot.addItem(self.footprint)
        # A ring around the selected point: a footprint can be too small to
        # see at map scale (a 0.1 x 0.05 mm tseries).
        self.ring = pg.ScatterPlotItem(size=24, brush=pg.mkBrush(None))
        self.plot.addItem(self.ring)
        self.beam = pg.ScatterPlotItem(symbol="+", size=26)
        self.plot.addItem(self.beam)

        self._theme = DEFAULT_THEME
        self._items: Dict[str, Dict] = {}
        self._order: List[str] = []
        self._stage: Dict = {}
        self._current_sample: Optional[str] = None
        self._selected: Optional[str] = None
        self._shown_sample: Optional[str] = None

    # Inputs

    def set_theme(self, theme: Dict):
        self._theme = theme
        _style_plot(self.plot, theme)
        self._redraw()

    def set_items(self, items: List[Dict]):
        self._items = {item["id"]: item for item in items}
        self._order = [item["id"] for item in items]
        samples = sorted({(item.get("params") or {}).get("sample")
                          for item in items} - {None})
        chosen = self.sample.currentText()
        self.sample.blockSignals(True)
        self.sample.clear()
        self.sample.addItems([self.FOLLOW] + samples)
        self.sample.setCurrentText(chosen if chosen in samples else self.FOLLOW)
        self.sample.blockSignals(False)
        self._redraw()

    def set_stage(self, stage: Dict, current_sample: Optional[str]):
        self._stage = stage or {}
        changed = current_sample != self._current_sample
        self._current_sample = current_sample
        if changed:
            self._redraw(fit=True)
        else:
            self._draw_stage()

    def set_selected(self, item_id: Optional[str]):
        self._selected = item_id
        self._draw_footprint()

    # Drawing

    def _sample(self) -> Optional[str]:
        chosen = self.sample.currentText()
        return self._current_sample if chosen == self.FOLLOW else chosen

    def _redraw(self, fit=False):
        theme, colors = self._theme, status_colors(self._theme)
        sample = self._sample()
        shown = [self._items[i] for i in self._order
                 if (self._items[i].get("params") or {}).get("sample") == sample]
        spots, segments = [], []
        for item in shown:
            params = item.get("params") or {}
            color = colors.get(item["status"], theme["TEXT_PRIMARY"])
            running = item["status"] == "running"
            hollow = item["status"] == "pending"
            spots.append({
                "pos": point_xz(params), "data": item["id"],
                "size": 15 if running else 9,
                "pen": pg.mkPen(color, width=2 if hollow or running else 1),
                "brush": (pg.mkBrush(None) if hollow else pg.mkBrush(color)),
            })
            segment = fly_segment(params)
            if segment:
                segments.extend(segment)
        self.points.setData(spots)
        if segments:
            self.lines.setData([p[0] for p in segments], [p[1] for p in segments],
                               connect="pairs",
                               pen=pg.mkPen(theme["TEXT_SECONDARY"], width=1))
        else:
            self.lines.setData([], [])
        counts = {}
        for item in shown:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        self.legend.setText("   " + "  ".join(
            f"<span style='color:{colors.get(s, theme['TEXT_PRIMARY'])}'>●</span>"
            f" {n} {s}" for s, n in sorted(counts.items())))
        self.legend.setTextFormat(QtCore.Qt.RichText)
        self._draw_stage()
        self._draw_footprint()
        if fit or sample != self._shown_sample:
            self._shown_sample = sample
            if shown:
                self.plot.getViewBox().autoRange(
                    items=[self.points, self.lines], padding=0.15)

    def _draw_stage(self):
        theme = self._theme
        frame = stage_frame(self._stage)
        if frame:
            x0, x1, z0, z1 = frame
            self.frame.setData([x0, x1, x1, x0, x0], [z0, z0, z1, z1, z0],
                               pen=pg.mkPen(theme["TEXT_SECONDARY"], width=1,
                                            style=QtCore.Qt.DashLine))
        else:
            self.frame.setData([], [])
        beam = beam_on_map(self._stage)
        if beam is not None:
            self.beam.setData([beam[0]], [beam[1]],
                              pen=pg.mkPen(theme["ACCENT"], width=2),
                              brush=pg.mkBrush(theme["ACCENT"]))
            names = [self._stage.get("roles", {}).get(r) for r in ("XM", "Z")]
            self.beam.setToolTip(f"beam now: x {beam[0]:.4g}, z {beam[1]:.4g} "
                                 f"({', '.join(n for n in names if n)})")
        else:
            self.beam.setData([], [])
        roles = self._stage.get("roles") or {}
        for axis, role in (("bottom", "XM"), ("left", "Z")):
            mne = roles.get(role)
            label = "x" if axis == "bottom" else "z"
            self.plot.setLabel(axis, f"{label} (mm)" + (f"  [{mne}]" if mne
                                                         and mne != "none"
                                                         else ""))

    def _draw_footprint(self):
        item = self._items.get(self._selected) if self._selected else None
        params = (item or {}).get("params") or {}
        if not item or params.get("sample") != self._sample():
            self.footprint.setVisible(False)
            self.ring.setData([], [])
            return
        x, z = point_xz(params)
        self.ring.setData([x], [z], pen=pg.mkPen(self._theme["ACCENT"], width=2))
        x0, x1, z0, z1 = scan_extent(params)
        self.footprint.setRect(QtCore.QRectF(x0, z0, x1 - x0, z1 - z0))
        fill = QtGui.QColor(self._theme["ACCENT"])
        fill.setAlpha(50)
        self.footprint.setPen(pg.mkPen(self._theme["ACCENT"], width=2))
        self.footprint.setBrush(pg.mkBrush(fill))
        self.footprint.setVisible(True)

    # Mouse

    def _tip(self, x, y, data):
        item = self._items.get(data) or {}
        params = item.get("params") or {}
        fly = params.get("fly")
        scan = params.get("scan", "")
        if isinstance(fly, dict):
            scan += f" {fly['axis']} {fly['start']:+g}..{fly['end']:+g}"
        return (f"{data}  {item.get('status')}\n{scan}\n"
                f"x {x:.4g}, z {y:.4g}, dataset {params.get('dataset_id')}")

    def _clicked(self, _item, points, *_):
        if len(points):
            self.pointClicked.emit(points[0].data())


class AttemptTimeline(QtWidgets.QWidget):
    """Every attempt as a bar on a time axis, one lane per sample, colored
    by outcome; the running attempt grows until it ends."""

    pointClicked = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Timeline")
        title.setStyleSheet("font-weight: bold")
        top.addWidget(title)
        self.legend = QtWidgets.QLabel("")
        self.legend.setTextFormat(QtCore.Qt.RichText)
        self.legend.setProperty("secondaryText", True)
        top.addWidget(self.legend, 1)
        fit = QtWidgets.QPushButton("Fit")
        fit.setToolTip("Show every attempt (and follow new ones)")
        fit.clicked.connect(self._fit)
        top.addWidget(fit)
        layout.addLayout(top)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.showGrid(x=True, alpha=0.15)
        self.plot.hideButtons()
        layout.addWidget(self.plot, 1)
        self.plot.scene().sigMouseClicked.connect(self._on_click)
        self.plot.scene().sigMouseMoved.connect(self._on_move)
        self._theme = DEFAULT_THEME
        self._graphs: List[pg.BarGraphItem] = []
        self._bars = []
        self._lanes: List[str] = []

    def set_theme(self, theme: Dict):
        self._theme = theme
        _style_plot(self.plot, theme)

    def refresh(self, items: List[Dict], now: float):
        colors = status_colors(self._theme)
        self._bars = attempt_bars(items, now)
        self._lanes = []
        for bar in self._bars:
            if bar[1] not in self._lanes:
                self._lanes.append(bar[1])
        for graph in self._graphs:
            self.plot.removeItem(graph)
        self._graphs = []
        outcomes = []
        for outcome in ("done", "running", "aborted", "failed", None):
            mine = [b for b in self._bars if b[4] == outcome]
            if not mine:
                continue
            outcomes.append(outcome)
            color = colors.get(outcome, self._theme["TEXT_SECONDARY"])
            # A darker edge keeps back-to-back attempts apart, and still
            # shows an attempt too short to be a pixel wide.
            graph = pg.BarGraphItem(
                x0=[b[2] for b in mine], x1=[b[3] for b in mine],
                y=[self._lanes.index(b[1]) for b in mine], height=0.6,
                brush=pg.mkBrush(color),
                pen=pg.mkPen(QtGui.QColor(color).darker(170), width=1))
            self.plot.addItem(graph)
            self._graphs.append(graph)
        self.plot.getAxis("left").setTicks(
            [[(i, str(name)) for i, name in enumerate(self._lanes)]])
        self.plot.setYRange(-0.7, max(1, len(self._lanes)) - 0.3, padding=0)
        self.legend.setText("   " + "  ".join(
            f"<span style='color:{colors.get(o, self._theme['TEXT_SECONDARY'])}'>"
            f"■</span> {o or 'other'}" for o in outcomes))

    def _fit(self):
        self.plot.enableAutoRange(x=True)
        self.plot.setYRange(-0.7, max(1, len(self._lanes)) - 0.3, padding=0)

    def _bar_at(self, scene_pos):
        view = self.plot.getViewBox()
        if not view.sceneBoundingRect().contains(scene_pos):
            return None
        point = view.mapSceneToView(scene_pos)
        lane = round(point.y())
        if not 0 <= lane < len(self._lanes) or abs(point.y() - lane) > 0.35:
            return None
        slack = 3 * view.viewPixelSize()[0]  # a few pixels, for short bars
        for bar in reversed(self._bars):
            if (bar[1] == self._lanes[lane]
                    and bar[2] - slack <= point.x() <= bar[3] + slack):
                return bar
        return None

    def _on_click(self, event):
        bar = self._bar_at(event.scenePos())
        if bar:
            self.pointClicked.emit(bar[0])

    def _on_move(self, scene_pos):
        bar = self._bar_at(scene_pos)
        if bar:
            started = datetime.fromtimestamp(bar[2]).strftime("%H:%M:%S")
            QtWidgets.QToolTip.showText(
                QtGui.QCursor.pos(),
                f"{bar[0]}  {bar[4] or ''}\nsample {bar[1]}\n"
                f"started {started}, took {fmt_duration(bar[3] - bar[2])}",
                self.plot)
        else:
            QtWidgets.QToolTip.hideText()


# ----------------------------------------------------------------------
# Import dialog
# ----------------------------------------------------------------------
class ImportDialog(QtWidgets.QDialog):
    """Asks for an old 22-column mapscan file and what to import it as.
    Column 11 has no default: the file has to say what it holds."""

    def __init__(self, modes: List[str], default_mode: Optional[str],
                 sample: str, col11_choices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import an old mapscan file")
        form = QtWidgets.QFormLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.path = QtWidgets.QLineEdit()
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("File:", row)
        self.sample = QtWidgets.QLineEdit(sample or "")
        form.addRow("Sample:", self.sample)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(modes)
        if default_mode in modes:
            self.mode.setCurrentText(default_mode)
        form.addRow("Mode:", self.mode)
        self.col11 = QtWidgets.QComboBox()
        self.col11.addItems(list(col11_choices))
        self.col11.setCurrentIndex(-1)
        self.col11.setPlaceholderText("choose what column 11 holds")
        form.addRow("Column 11:", self.col11)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        for signal in (self.path.textChanged, self.sample.textChanged,
                       self.col11.currentIndexChanged):
            signal.connect(self._update_ok)
        self._update_ok()

    def _browse(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Old mapscan file", os.path.dirname(self.path.text()))
        if path:
            self.path.setText(path)

    def _update_ok(self):
        ready = (bool(self.path.text().strip()) and bool(self.sample.text().strip())
                 and self.col11.currentIndex() >= 0)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(ready)

    def values(self):
        return (os.path.abspath(self.path.text().strip()),
                self.sample.text().strip(), self.mode.currentText(),
                self.col11.currentText())


# ----------------------------------------------------------------------
# The tab
# ----------------------------------------------------------------------
class MapQueueTab(QtWidgets.QWidget):

    def __init__(self, url: str, client_path: Optional[str] = None,
                 poll_s: float = 1.0, theme: Optional[Dict] = None,
                 parent=None):
        super().__init__(parent)
        self.url = url
        self._status: Dict = {}
        self._theme = theme or DEFAULT_THEME
        outer = QtWidgets.QVBoxLayout(self)
        try:
            if client_path and client_path not in sys.path:
                sys.path.insert(0, client_path)
            from mapqueue.client import ApiError, Client
            from mapqueue.legacy import COL11_CHOICES
        except ImportError as exc:
            message = QtWidgets.QLabel(
                f"Can't load mapqueue's client from {client_path!r}: {exc}. "
                "Check map_queue.client_path in the beamline YAML.")
            message.setWordWrap(True)
            outer.addWidget(message)
            outer.addStretch(1)
            self.worker = None
            return
        self._col11_choices = COL11_CHOICES
        self._build(outer)
        self.apply_theme(self._theme)
        self.worker = QueueWorker(Client(url), ApiError, poll_s)
        self.worker.statusChanged.connect(self._on_status)
        self.worker.queueChanged.connect(self._on_queue)
        self.worker.unreachable.connect(self._on_unreachable)
        self.worker.actionDone.connect(self._on_action_done)
        self.worker.actionFailed.connect(self._on_action_failed)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self.worker.start()

    # Layout

    def _build(self, outer):
        top = QtWidgets.QHBoxLayout()
        self.banner = StatusBanner()
        top.addWidget(self.banner, 1)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setToolTip("Stop after the current point")
        self.pause_btn.clicked.connect(lambda: self._submit("pause", "pause"))
        self.resume_btn = QtWidgets.QPushButton("Resume")
        self.resume_btn.clicked.connect(lambda: self._submit("resume", "resume"))
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.setToolTip("Stop now, in the middle of the scan")
        self.abort_btn.clicked.connect(self._abort)
        for button in (self.pause_btn, self.resume_btn, self.abort_btn):
            top.addWidget(button)
        outer.addLayout(top)

        self.hold_label = QtWidgets.QLabel()
        self.hold_label.setWordWrap(True)
        outer.addWidget(self.hold_label)
        attention = QtWidgets.QHBoxLayout()
        self.attention_label = QtWidgets.QLabel()
        self.attention_label.setWordWrap(True)
        self.ok_btn = QtWidgets.QPushButton("OK…")
        self.ok_btn.clicked.connect(self._ok)
        attention.addWidget(self.attention_label, 1)
        attention.addWidget(self.ok_btn)
        outer.addLayout(attention)
        interrupted = QtWidgets.QHBoxLayout()
        self.interrupted_label = QtWidgets.QLabel()
        self.restart_btn = QtWidgets.QPushButton("Restart…")
        self.restart_btn.clicked.connect(self._restart)
        interrupted.addWidget(self.interrupted_label, 1)
        interrupted.addWidget(self.restart_btn)
        outer.addLayout(interrupted)

        # Left: filters, the table and the selected point. Right: the map
        # above the timeline.
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(QtWidgets.QLabel("Show:"))
        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.addItems(["all"] + list(STATUSES))
        self.status_filter.currentTextChanged.connect(self._apply_filter)
        filters.addWidget(self.status_filter)
        self.hide_done = QtWidgets.QCheckBox("Hide done")
        self.hide_done.toggled.connect(self._apply_filter)
        filters.addWidget(self.hide_done)
        self.counts_label = QtWidgets.QLabel("")
        self.counts_label.setProperty("secondaryText", True)
        filters.addWidget(self.counts_label)
        filters.addStretch(1)
        self.connection_label = QtWidgets.QLabel(self.url)
        self.connection_label.setProperty("secondaryText", True)
        filters.addWidget(self.connection_label)
        left_layout.addLayout(filters)

        left_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.model = QueueModel(self)
        self.model.moveRequested.connect(
            lambda ids, before: self._submit("move", "move", ids, before))
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.table.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.table.setDragDropOverwriteMode(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)  # Problems takes the rest
        self.table.selectionModel().selectionChanged.connect(self._show_details)
        left_split.addWidget(self.table)

        details = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_top = QtWidgets.QHBoxLayout()
        details_title = QtWidgets.QLabel("Point")
        details_title.setStyleSheet("font-weight: bold")
        details_top.addWidget(details_title)
        details_top.addStretch(1)
        self.raw_json = QtWidgets.QCheckBox("Raw JSON")
        self.raw_json.toggled.connect(self._show_details)
        details_top.addWidget(self.raw_json)
        details_layout.addLayout(details_top)
        self.details = QtWidgets.QTextBrowser()
        self.details.setPlaceholderText("Select a point to see its "
                                        "parameters and history.")
        details_layout.addWidget(self.details, 1)
        left_split.addWidget(details)
        left_split.setSizes([480, 260])
        left_layout.addWidget(left_split, 1)

        right_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.map = PointMap()
        self.map.pointClicked.connect(self._select_point)
        right_split.addWidget(self.map)
        self.timeline = AttemptTimeline()
        self.timeline.pointClicked.connect(self._select_point)
        right_split.addWidget(self.timeline)
        right_split.setSizes([520, 220])

        main = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main.addWidget(left)
        main.addWidget(right_split)
        main.setSizes([800, 600])
        outer.addWidget(main, 1)

        buttons = QtWidgets.QHBoxLayout()
        import_btn = QtWidgets.QPushButton("Import old file…")
        import_btn.clicked.connect(self._import)
        buttons.addWidget(import_btn)
        buttons.addStretch(1)
        for label, mover in (("Move to top", move_to_top),
                             ("Move up", move_up), ("Move down", move_down)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda _=False, m=mover: self._move(m))
            buttons.addWidget(button)
        remove_btn = QtWidgets.QPushButton("Remove")
        remove_btn.clicked.connect(self._remove)
        buttons.addWidget(remove_btn)
        outer.addLayout(buttons)
        self._message = QtWidgets.QLabel("")
        self._message.setProperty("secondaryText", True)
        outer.addWidget(self._message)
        self._on_status({})

    def apply_theme(self, theme: Dict):
        """Re-color everything after the dashboard switches themes."""
        self._theme = theme
        if not hasattr(self, "banner"):
            return
        self.banner.set_theme(theme)
        self.model.set_colors(status_colors(theme))
        self.map.set_theme(theme)
        self.timeline.set_theme(theme)
        self._on_status(self._status)
        self.timeline.refresh(self.model.all_items(), time.time())
        self._show_details()

    # Worker results

    def _on_status(self, status: Dict):
        self._status = status
        now = time.time()
        self.banner.update_from(status, self.model.all_items(), now)
        if not status:
            self.banner.set_state("connecting…")
        else:
            self.connection_label.setText(self.url)
            self.map.set_stage(status.get("stage") or {},
                               status.get("current_sample"))
            if status.get("running"):
                self.timeline.refresh(self.model.all_items(), now)
        counts = status.get("counts") or {}
        self.counts_label.setText(
            "   " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
        holding = status.get("holding")
        self.hold_label.setText("⚠ holding: " + "; ".join(holding)
                                if holding else "")
        self.hold_label.setVisible(bool(holding))
        attention = status.get("attention")
        self.attention_label.setText(
            f"⚠ needs OK: {attention['message']}" if attention else "")
        for widget in (self.attention_label, self.ok_btn):
            widget.setVisible(bool(attention))
        interrupted = status.get("interrupted") or []
        self.interrupted_label.setText(
            "⚠ interrupted, needs an OK to run again: " + ", ".join(interrupted)
            if interrupted else "")
        for widget in (self.interrupted_label, self.restart_btn):
            widget.setVisible(bool(interrupted))
        connected = bool(status)
        self.pause_btn.setEnabled(connected and not status.get("paused"))
        self.resume_btn.setEnabled(connected and bool(status.get("paused")))
        self.abort_btn.setEnabled(connected)

    def _on_queue(self, items: List[Dict]):
        selected = self._selected_ids()
        self.model.set_items(items)
        self._reselect(selected)
        self.map.set_items(items)
        self.timeline.refresh(items, time.time())
        self.banner.update_from(self._status, items, time.time())
        self._show_details()

    def _on_unreachable(self, error: str):
        self.connection_label.setText(f"{self.url}: {error}")
        self._on_status({})
        self.banner.set_state("runner not reachable")

    def _on_action_done(self, what: str, result):
        if what == "import":
            self._message.setText(f"imported {len(result)} points: "
                                  f"{result[0]['id']} .. {result[-1]['id']}")
        else:
            self._message.setText(f"{what}: done")

    def _on_action_failed(self, what: str, errors: List[str]):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle(f"{what} refused")
        shown = errors[:10]
        more = f"\n… and {len(errors) - 10} more (see details)" if len(errors) > 10 else ""
        box.setText(f"The runner refused {what}:\n\n" + "\n".join(shown) + more)
        if len(errors) > 10:
            box.setDetailedText("\n".join(errors))
        box.exec()

    # Actions

    def _submit(self, what: str, method: str, *args):
        if self.worker is not None:
            self.worker.submit(what, method, *args)

    def _confirm(self, title: str, text: str) -> bool:
        answer = QtWidgets.QMessageBox.question(
            self, title, text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def _abort(self):
        running = self._status.get("running")
        text = (f"Abort now? The running point {running} is stopped in the "
                "middle of its scan and goes back in the queue."
                if running else "Abort now?")
        if self._confirm("Abort", text):
            self._submit("abort", "abort")

    def _ok(self):
        attention = self._status.get("attention")
        if not attention:
            return
        text = attention["message"]
        for error in attention.get("cleanup_errors") or []:
            text += f"\ncleanup error: {error}"
        if self._confirm("Needs OK", text + "\n\nCarry on with the queue?"):
            self._submit("ok", "ok")

    def _restart(self):
        ids = self._status.get("interrupted") or []
        items = {item["id"]: item for item in self.model.all_items()}
        lines = []
        for item_id in ids:
            item = items.get(item_id, {})
            attempt = (item.get("history") or [{}])[-1]
            lines.append(f"restarting interrupted scan {item_id}: attempt "
                         f"{attempt.get('attempt')} started "
                         f"{attempt.get('started')}, sample "
                         f"{(item.get('params') or {}).get('sample')}")
        if ids and self._confirm("Restart interrupted scans",
                                 "\n".join(lines) + "\n\nRun them again from "
                                 "the start?"):
            self._submit("restart", "restart_ok", ids)

    def _import(self):
        sample = self._status.get("current_sample") or ""
        dialog = ImportDialog(self._status.get("modes") or [],
                              self._status.get("default_mode"), sample,
                              self._col11_choices, self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self._submit("import", "import_file", *dialog.values())

    def _move(self, mover):
        ids = [i for i in self._selected_ids()
               if self.model.item(self.model.row_of(i))["status"] in EDITABLE]
        if not ids:
            return
        should, before = mover(self.model.all_items(), ids)
        if should:
            self._submit("move", "move", ids, before)

    def _remove(self):
        ids = [i for i in self._selected_ids()
               if self.model.item(self.model.row_of(i))["status"] in EDITABLE]
        if ids and self._confirm("Remove", f"Remove {len(ids)} waiting "
                                 f"point(s)?\n" + ", ".join(ids[:20])):
            self._submit("remove", "remove", ids)

    # Selection, filter and details

    def _apply_filter(self, *_):
        selected = self._selected_ids()
        status = self.status_filter.currentText()
        self.model.set_filter(None if status == "all" else status,
                              self.hide_done.isChecked())
        self._reselect(selected)

    def _selected_ids(self) -> List[str]:
        rows = sorted({index.row() for index in
                       self.table.selectionModel().selectedRows()})
        return [self.model.item(r)["id"] for r in rows]

    def _reselect(self, ids: List[str]):
        selection = QtCore.QItemSelection()
        for item_id in ids:
            row = self.model.row_of(item_id)
            if row >= 0:
                selection.select(self.model.index(row, 0),
                                 self.model.index(row, len(COLUMNS) - 1))
        self.table.selectionModel().select(
            selection, QtCore.QItemSelectionModel.ClearAndSelect)

    def _select_point(self, item_id: str):
        """A click on the map or the timeline: select that row, showing
        every point if the filter hides it."""
        if self.model.row_of(item_id) < 0:
            self.status_filter.setCurrentText("all")
            self.hide_done.setChecked(False)
        self._reselect([item_id])
        row = self.model.row_of(item_id)
        if row >= 0:
            self.table.scrollTo(self.model.index(row, 0))

    def _show_details(self, *_):
        ids = self._selected_ids()
        self.map.set_selected(ids[0] if len(ids) == 1 else None)
        if len(ids) != 1:
            self.details.setPlainText("" if not ids else
                                      f"{len(ids)} points selected")
            return
        item = self.model.item(self.model.row_of(ids[0]))
        if self.raw_json.isChecked():
            shown = {key: item.get(key) for key in
                     ("id", "status", "attempts", "scan_n", "problems", "source",
                      "added", "started", "finished", "params", "history")}
            self.details.setPlainText(json.dumps(shown, indent=1))
        else:
            self.details.setHtml(point_html(item, self._theme))

    def shutdown(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
