"""Stage map: where the beam is on the sample, live.

A pyqtgraph map of the sample's x-z plane, fed by mapqueue's read-only
stage service (`python -m mapqueue.stage`, GET /stage): the XM/Z user
limits as a dashed frame, the beam's position now as a cross, and a
fading trail of where it has been. The stage service sends spec no
commands and can't queue anything, so the map works whether or not the
queue runner is running.

The Map Queue tab overlays the queue's points on it while the runner is
up (set_items); with no items it's the stage alone. Polling uses only
the standard library, so this module needs neither mapqueue nor pyspec.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg  # after PySide6, so it binds to PySide6

# The dashboard's dark palette (spec_dashboard_qt.THEMES["dark"]), used
# until the dashboard passes the active one (set_theme / apply_theme).
DEFAULT_THEME = {
    "BG_PANEL": "#242534", "BORDER": "#3d3f57", "TEXT_PRIMARY": "#e8e8f2",
    "TEXT_SECONDARY": "#a0a3ba", "ACCENT": "#5ec8f8",
    "PRESSED_TEXT": "#0a0a0f",
    "PLOT_COLORS": ["#5ec8f8", "#ffb74d", "#81c995", "#f28b82", "#c58af9"],
}


def status_colors(theme: Dict) -> Dict[str, str]:
    """One color per point status and attempt outcome, from the theme."""
    plot = theme["PLOT_COLORS"]
    return {"pending": theme["TEXT_SECONDARY"], "running": theme["ACCENT"],
            "done": plot[2], "blocked": plot[1], "aborted": plot[1],
            "failed": plot[3]}


def style_plot(plot: pg.PlotWidget, theme: Dict):
    plot.setBackground(theme["BG_PANEL"])
    for name in ("bottom", "left"):
        axis = plot.getAxis(name)
        axis.setPen(pg.mkPen(theme["TEXT_SECONDARY"]))
        axis.setTextPen(pg.mkPen(theme["TEXT_PRIMARY"]))


# ----------------------------------------------------------------------
# Map geometry (pure)
# ----------------------------------------------------------------------
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


def stage_frame(stage: Dict):
    """(x0, x1, z0, z1): the XM and Z user limits, or None if unknown."""
    roles, limits = stage.get("roles") or {}, stage.get("limits") or {}
    x, z = limits.get(roles.get("XM")), limits.get(roles.get("Z"))
    if not x or not z:
        return None
    return x[0], x[1], z[0], z[1]


def trim_trail(trail: List, now: float, keep_s: float) -> List:
    """The trail entries (t, x, z) from the last ``keep_s`` seconds."""
    return [entry for entry in trail if entry[0] >= now - keep_s]


# ----------------------------------------------------------------------
# Polling the stage service, off the UI thread
# ----------------------------------------------------------------------
class StagePoller(QtCore.QThread):
    """GETs /stage every ``poll_s``, asking only for trail entries newer
    than the last ones it saw."""

    stageChanged = QtCore.Signal(object)  # the /stage reply
    unreachable = QtCore.Signal(str)

    def __init__(self, url: str, poll_s: float = 0.25):
        super().__init__()
        self._url = url.rstrip("/")
        self._poll_s = max(0.1, poll_s)
        self._stop = threading.Event()
        self._since = 0.0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(
                        f"{self._url}/stage?since={self._since!r}",
                        timeout=2) as response:
                    stage = json.load(response)
            except (OSError, ValueError) as exc:
                self._since = 0.0  # the service may have restarted
                self.unreachable.emit(str(exc))
            else:
                if stage.get("trail"):
                    self._since = stage["trail"][-1][0]
                self.stageChanged.emit(stage)
            self._stop.wait(self._poll_s)


# ----------------------------------------------------------------------
# The map
# ----------------------------------------------------------------------
class StageMap(QtWidgets.QWidget):
    """The sample's x-z plane: stage limits, the beam now and its trail,
    and (when given) the queue's points for one sample."""

    pointClicked = QtCore.Signal(str)
    FOLLOW = "current sample"

    def __init__(self, stage_url: Optional[str] = None, trail_s: float = 120.0,
                 poll_s: float = 0.25, theme: Optional[Dict] = None,
                 parent=None):
        super().__init__(parent)
        self.trail_s = trail_s
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QtWidgets.QHBoxLayout()
        self.where = QtWidgets.QLabel("")
        self.where.setTextFormat(QtCore.Qt.RichText)
        top.addWidget(self.where)
        self.sample = QtWidgets.QComboBox()
        self.sample.addItem(self.FOLLOW)
        self.sample.currentTextChanged.connect(lambda _: self._redraw(fit=True))
        top.addWidget(self.sample)
        self.legend = QtWidgets.QLabel("")
        self.legend.setTextFormat(QtCore.Qt.RichText)
        top.addWidget(self.legend)
        top.addStretch(1)  # its own spacer: the legend is hidden without a queue
        fit = QtWidgets.QPushButton("Fit")
        fit.setToolTip("Zoom to the points (or the trail, or the stage)")
        fit.clicked.connect(lambda: self._fit())
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
        self.trail_line = pg.PlotDataItem()
        self.plot.addItem(self.trail_line)
        self.trail_dots = pg.ScatterPlotItem(size=4, pen=pg.mkPen(None))
        self.plot.addItem(self.trail_dots)
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

        self._theme = theme or DEFAULT_THEME
        self._items: Dict[str, Dict] = {}
        self._order: List[str] = []
        self._stage: Dict = {}
        self._trail: List = []
        self._current_sample: Optional[str] = None
        self._selected: Optional[str] = None
        self._shown_sample: Optional[str] = None
        self._fitted = False
        self._titles: Dict[str, str] = {}  # axis -> title last set
        style_plot(self.plot, self._theme)
        self._show_overlay_controls()

        self.poller = None
        if stage_url:
            self.poller = StagePoller(stage_url, poll_s)
            self.poller.stageChanged.connect(self._on_stage)
            self.poller.unreachable.connect(self._on_unreachable)
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.shutdown)
            self.poller.start()
        else:
            self.where.setText("no stage service configured "
                               "(map_queue.stage_url)")

    # Inputs

    def set_theme(self, theme: Dict):
        self._theme = theme
        style_plot(self.plot, theme)
        self._redraw()
        self._draw_stage()

    def set_items(self, items: List[Dict]):
        """The queue's points to overlay; [] when the runner isn't there."""
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
        self._show_overlay_controls()
        self._redraw()

    def set_current_sample(self, sample: Optional[str]):
        if sample != self._current_sample:
            self._current_sample = sample
            self._redraw(fit=True)

    def set_selected(self, item_id: Optional[str]):
        self._selected = item_id
        self._draw_footprint()

    def shutdown(self):
        if self.poller is not None:
            self.poller.stop()
            self.poller.wait(3000)

    # The stage service

    def _on_stage(self, stage: Dict):
        self._stage = stage
        now = stage.get("now") or time.time()
        self._trail = trim_trail(self._trail + (stage.get("trail") or []),
                                 now, self.trail_s)
        self._draw_stage(now)
        if not self._fitted:
            self._fit()

    def _on_unreachable(self, error: str):
        self.where.setText(f"<span style='color:"
                           f"{status_colors(self._theme)['failed']}'>"
                           f"stage service not reachable</span>")
        self.where.setToolTip(error)
        self._stage, self._trail = {}, []
        self._draw_stage()

    # Drawing

    def _show_overlay_controls(self):
        overlay = bool(self._items)
        self.sample.setVisible(overlay)
        self.legend.setVisible(overlay)

    def _sample(self) -> Optional[str]:
        chosen = self.sample.currentText()
        return self._current_sample if chosen == self.FOLLOW else chosen

    def _shown(self) -> List[Dict]:
        sample = self._sample()
        return [self._items[i] for i in self._order
                if (self._items[i].get("params") or {}).get("sample") == sample]

    def _redraw(self, fit=False):
        theme, colors = self._theme, status_colors(self._theme)
        shown = self._shown()
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
                "brush": pg.mkBrush(None) if hollow else pg.mkBrush(color),
            })
            segment = fly_segment(params)
            if segment:
                segments.extend(segment)
        self.points.setData(spots)
        if segments:
            self.lines.setData([p[0] for p in segments],
                               [p[1] for p in segments], connect="pairs",
                               pen=pg.mkPen(theme["TEXT_SECONDARY"], width=1))
        else:
            self.lines.setData([], [])
        counts = {}
        for item in shown:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        self.legend.setText("   " + "  ".join(
            f"<span style='color:{colors.get(s, theme['TEXT_PRIMARY'])}'>●</span>"
            f" {n} {s}" for s, n in sorted(counts.items())))
        self._draw_footprint()
        sample = self._sample()
        if fit or sample != self._shown_sample:
            self._shown_sample = sample
            if shown:
                self._fit()

    def _draw_stage(self, now: Optional[float] = None):
        theme, stage = self._theme, self._stage
        frame = stage_frame(stage)
        if frame:
            x0, x1, z0, z1 = frame
            self.frame.setData([x0, x1, x1, x0, x0], [z0, z0, z1, z1, z0],
                               pen=pg.mkPen(theme["TEXT_SECONDARY"], width=1,
                                            style=QtCore.Qt.DashLine))
        else:
            self.frame.setData([], [])

        # The trail fades with age: older dots are more transparent.
        now = now or time.time()
        if self._trail:
            accent = QtGui.QColor(theme["ACCENT"])
            line = QtGui.QColor(accent)
            line.setAlpha(70)
            xs = [entry[1] for entry in self._trail]
            zs = [entry[2] for entry in self._trail]
            self.trail_line.setData(xs, zs, pen=pg.mkPen(line, width=1))
            brushes = []
            for entry in self._trail:
                fade = 1.0 - min(1.0, max(0.0, (now - entry[0]) / self.trail_s))
                dot = QtGui.QColor(accent)
                dot.setAlpha(int(25 + 200 * fade))
                brushes.append(pg.mkBrush(dot))
            self.trail_dots.setData(xs, zs, brush=brushes)
        else:
            self.trail_line.setData([], [])
            self.trail_dots.setData([], [])

        beam = stage.get("beam")
        roles = stage.get("roles") or {}
        names = [roles.get(r) for r in ("XM", "Z")]
        if beam:
            self.beam.setData([beam[0]], [beam[1]],
                              pen=pg.mkPen(theme["ACCENT"], width=2),
                              brush=pg.mkBrush(theme["ACCENT"]))
            self.where.setText(
                f"<b>Stage</b> &nbsp; beam at x {beam[0]:.4g}, z {beam[1]:.4g}")
            self.where.setToolTip(", ".join(f"{role} {mne}"
                                            for role, mne in roles.items()))
        else:
            self.beam.setData([], [])
            if stage:
                self.where.setText("<b>Stage</b> &nbsp; beam position unknown")
        # Set the titles only when they change: each setLabel relayouts the
        # axis, and this runs on every poll.
        for axis, mne in (("bottom", names[0]), ("left", names[1])):
            label = "x" if axis == "bottom" else "z"
            title = f"{label} (mm)" + (f"  [{mne}]" if mne and mne != "none"
                                       else "")
            if self._titles.get(axis) != title:
                self._titles[axis] = title
                self.plot.setLabel(axis, title)

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

    def _fit(self):
        """Zoom to the overlay's points, else the trail and beam, else the
        stage limits."""
        view = self.plot.getViewBox()
        if self._shown():
            view.autoRange(items=[self.points, self.lines], padding=0.15)
        elif self._trail:
            view.autoRange(items=[self.trail_dots, self.beam], padding=0.3)
        elif stage_frame(self._stage):
            x0, x1, z0, z1 = stage_frame(self._stage)
            view.setRange(xRange=(x0, x1), yRange=(z0, z1), padding=0.05)
        else:
            return
        self._fitted = True

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
