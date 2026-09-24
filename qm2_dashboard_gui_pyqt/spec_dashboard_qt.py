"""
SPEC Dashboard — PySide6 (Qt 6) native GUI.

A native desktop application (PySide6 + pyqtgraph) for browsing and
analyzing SPEC data files from synchrotron beamline experiments. Uses the
same Qt binding as hexrd/hexrdgui, so hexrd's instrument-based detector
rendering can run in the same process.

All parsing/analysis logic lives in spec_core.py (toolkit-agnostic, reused
unchanged from the Tkinter build). This file only handles presentation.
Everything beamline-specific (titles, tabs, live-signal channels, data
layout, ...) comes from a YAML config in configs/, loaded by
beamline_config.py.

Run with:
    python spec_dashboard_qt.py                          # configs/id3a.yaml
    python spec_dashboard_qt.py --config configs/qm2.yaml

Requirements:
    pip install -r requirements.txt
"""

import argparse
import json
import os
import re
import shutil
import smtplib
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
# Imported before pyqtgraph so pyqtgraph binds to PySide6 too.
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
import pyqtgraph.exporters
from PIL import Image as PILImage
from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import beamline_config as bcfg
import spec_core as sc
import chess_signals as csig

# fabio is only needed for the "Live Image (Pilatus)" tab, which reads
# .cbf detector frames — the same optional dependency pilatus_live_viewer.py
# uses. Importing it lazily/optionally here means the rest of the dashboard
# (SPEC-file browsing, plotting, etc.) keeps working even on a machine where
# fabio isn't installed; that tab just shows an explanatory message instead.
try:
    import fabio
except ImportError:
    fabio = None

# ── "Send by Email" feature ──────────────────────────────────────────────
# Provider presets so a user who doesn't know their own SMTP host/port can
# just pick their email provider by name and have the connection details
# filled in automatically. "Custom / Other" leaves the fields blank/editable
# for anything not covered here.
EMAIL_PROVIDER_PRESETS = {
    "Gmail": ("smtp.gmail.com", 587, True),
    "Outlook / Office 365": ("smtp.office365.com", 587, True),
    "Yahoo Mail": ("smtp.mail.yahoo.com", 587, True),
    "iCloud Mail": ("smtp.mail.me.com", 587, True),
    "Custom / Other": ("", 587, True),
}

# Where the "Remember these settings" checkbox in the email dialog persists
# its values between runs. Kept in the user's home directory (not the repo)
# since it may contain a plaintext password if the user opts into that.
# Named from the config's app.settings_prefix by _apply_config().
EMAIL_SETTINGS_PATH = ""

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── "Slack Beam Alerts" feature ──────────────────────────────────────────
# Posts a message to a Slack channel via a user-supplied Bot Token, using
# Slack's chat.postMessage Web API method directly
# (https://api.slack.com/methods/chat.postMessage) over urllib.request --
# standard library only, same "no extra dependency" approach the Email
# feature above takes with smtplib, rather than pulling in the slack_sdk
# package. The token is never hardcoded anywhere in this file: it's typed
# into the dedicated "Slack Alerts" tab at runtime and, only if
# the user opts in via "Remember token on this computer", persisted to
# SLACK_SETTINGS_PATH in the user's home directory (chmod 600, same as
# EMAIL_SETTINGS_PATH's password-remembering behavior above). Both set from
# the config by _apply_config().
SLACK_SETTINGS_PATH = ""
SLACK_DEFAULT_CHANNEL = ""
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# ── Theme system (Dark / Light) ──────────────────────────────────────────
# The QApplication stylesheet, pyqtgraph's own background/foreground config,
# and the plot color cycle are all drawn from the SAME palette so everything
# reads as one coherent look. THEMES holds both full palettes; the module-
# level names below (BG_WINDOW, TEXT_PRIMARY, PLOT_COLORS, ...) always hold
# the *currently active* theme's values and are reassigned by
# _apply_theme_globals() when the user switches themes at runtime.
THEMES = {
    "dark": {
        "BG_WINDOW": "#1b1c26",
        "BG_PANEL": "#242534",
        "BG_INPUT": "#2b2d3f",
        "BG_INPUT_HOVER": "#333650",
        "BORDER": "#3d3f57",
        "TEXT_PRIMARY": "#e8e8f2",
        "TEXT_SECONDARY": "#a0a3ba",
        "ACCENT": "#5ec8f8",
        "ACCENT_HOVER": "#7fd4fa",
        "ACCENT_PRESSED": "#3fa8d8",
        "SELECTION_BG": "#3a4a78",
        "PRESSED_TEXT": "#0a0a0f",
        "FIT_COLOR": "#ffd54f",
        "RESIDUAL_COLOR": "#f28b82",
        "CALIB_COLOR": "#d1d5db",
        "SAMPLE_COLOR": "#3b82f6",
        "GHOST_COLOR": "#9aa0b8",
        "REFERENCE_COLOR": "#c58af9",
        "PLOT_COLORS": [
            "#5ec8f8", "#ffb74d", "#81c995", "#f28b82", "#c58af9",
            "#d7a86e", "#f48fb1", "#b8bbd0", "#dce775", "#4dd0e1",
        ],
    },
    "light": {
        "BG_WINDOW": "#f4f5f9",
        "BG_PANEL": "#ffffff",
        "BG_INPUT": "#ffffff",
        "BG_INPUT_HOVER": "#eef1fb",
        "BORDER": "#d4d7e3",
        "TEXT_PRIMARY": "#20222e",
        "TEXT_SECONDARY": "#5b5e73",
        "ACCENT": "#0f7fb8",
        "ACCENT_HOVER": "#1391d1",
        "ACCENT_PRESSED": "#0a5f8c",
        "SELECTION_BG": "#cfe0fb",
        "PRESSED_TEXT": "#ffffff",
        "FIT_COLOR": "#b45309",
        "RESIDUAL_COLOR": "#dc2626",
        "CALIB_COLOR": "#9ca3af",
        "SAMPLE_COLOR": "#1f5fbf",
        "GHOST_COLOR": "#6b7280",
        "REFERENCE_COLOR": "#7c3aed",
        "PLOT_COLORS": [
            "#1f77b4", "#d97706", "#15803d", "#dc2626", "#7c3aed",
            "#92400e", "#db2777", "#475569", "#65a30d", "#0891b2",
        ],
    },
}


def _apply_theme_globals(name: str):
    """Reassign the module-level color constants to the given theme's
    palette. Anything that reads these names *at call time* (QSS built via
    _build_qss(), _color_for(), chart-building code that runs on demand)
    will pick up the new colors immediately; anything that baked a color
    into a fixed string at construction time needs to be explicitly
    restyled (see SpecDashboardApp.set_theme)."""
    global BG_WINDOW, BG_PANEL, BG_INPUT, BG_INPUT_HOVER, BORDER
    global TEXT_PRIMARY, TEXT_SECONDARY, ACCENT, ACCENT_HOVER, ACCENT_PRESSED
    global SELECTION_BG, PRESSED_TEXT, FIT_COLOR, RESIDUAL_COLOR
    global CALIB_COLOR, SAMPLE_COLOR, PLOT_COLORS
    global GHOST_COLOR, REFERENCE_COLOR
    theme = THEMES[name]
    BG_WINDOW = theme["BG_WINDOW"]
    BG_PANEL = theme["BG_PANEL"]
    BG_INPUT = theme["BG_INPUT"]
    BG_INPUT_HOVER = theme["BG_INPUT_HOVER"]
    BORDER = theme["BORDER"]
    TEXT_PRIMARY = theme["TEXT_PRIMARY"]
    TEXT_SECONDARY = theme["TEXT_SECONDARY"]
    ACCENT = theme["ACCENT"]
    ACCENT_HOVER = theme["ACCENT_HOVER"]
    ACCENT_PRESSED = theme["ACCENT_PRESSED"]
    SELECTION_BG = theme["SELECTION_BG"]
    PRESSED_TEXT = theme["PRESSED_TEXT"]
    FIT_COLOR = theme["FIT_COLOR"]
    RESIDUAL_COLOR = theme["RESIDUAL_COLOR"]
    CALIB_COLOR = theme["CALIB_COLOR"]
    SAMPLE_COLOR = theme["SAMPLE_COLOR"]
    GHOST_COLOR = theme["GHOST_COLOR"]
    REFERENCE_COLOR = theme["REFERENCE_COLOR"]
    PLOT_COLORS = theme["PLOT_COLORS"]


def _build_qss() -> str:
    """Build the QApplication stylesheet from the *current* module-level
    color constants. Called once at startup and again every time the user
    switches themes."""
    return f"""
QWidget {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
    selection-background-color: {SELECTION_BG};
    selection-color: {TEXT_PRIMARY};
}}
QMainWindow, QDialog {{
    background-color: {BG_WINDOW};
}}
QLabel[secondaryText="true"] {{
    color: {TEXT_SECONDARY};
}}
QToolTip {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 3px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {BG_WINDOW};
    top: -1px;
}}
QTabBar::tab {{
    background: {BG_PANEL};
    color: {TEXT_SECONDARY};
    padding: 6px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {BG_WINDOW};
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{
    color: {TEXT_PRIMARY};
}}
QPushButton {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    background-color: {BG_INPUT_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    color: {PRESSED_TEXT};
}}
QPushButton:disabled {{
    color: {TEXT_SECONDARY};
    background-color: {BG_PANEL};
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: {TEXT_SECONDARY};
    padding: 2px 6px;
}}
QToolButton:hover {{
    color: {ACCENT};
    border-color: {BORDER};
    background: {BG_INPUT};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 6px;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    selection-background-color: {SELECTION_BG};
}}
QListWidget, QTableWidget, QTreeWidget {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    alternate-background-color: {BG_PANEL};
    gridline-color: {BORDER};
}}
QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QTableWidget::item:selected, QListWidget::item:selected {{
    background-color: {SELECTION_BG};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 8px;
    color: {TEXT_SECONDARY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {ACCENT};
}}
QCheckBox, QRadioButton {{
    color: {TEXT_PRIMARY};
    spacing: 6px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER};
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {BG_WINDOW};
    border: none;
}}
QScrollBar::handle {{
    background: {BG_INPUT_HOVER};
    border-radius: 4px;
}}
QScrollBar::handle:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    border: none;
    background: none;
}}
QSplitter::handle {{
    background: {BORDER};
}}
QMenuBar {{
    background-color: {BG_PANEL};
    color: {TEXT_PRIMARY};
}}
QMenuBar::item:selected {{
    background-color: {SELECTION_BG};
}}
QMenu {{
    background-color: {BG_PANEL};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background-color: {SELECTION_BG};
}}
QStatusBar {{
    background-color: {BG_PANEL};
    color: {TEXT_SECONDARY};
}}
QScrollArea {{
    border: none;
}}
"""


_apply_theme_globals("dark")
# imageAxisOrder="row-major" matches pilatus_live_viewer.py: it tells
# pyqtgraph to treat 2D arrays as data[row, col] (numpy's own, and fabio's,
# native layout) instead of pyqtgraph's historical default of
# data[col, row]. Without this, ImageView/ImageItem draws every Pilatus
# frame in the Live Image tab transposed relative to the actual detector
# orientation. This is a process-wide pyqtgraph setting, so it's set once
# here at import time -- exactly like the standalone viewer does -- rather
# than only where the image is displayed.
pg.setConfigOptions(antialias=True, background=BG_PANEL, foreground=TEXT_PRIMARY,
                     imageAxisOrder="row-major")

# ── Beamline config ──────────────────────────────────────────────────────
# The loaded beamline YAML config (see beamline_config.py / configs/). The
# module-level names below are filled from it by _apply_config() at startup,
# before any widget is built.
CONFIG: Dict = {}
# APP_TITLE: the short name used for the window title bar, the taskbar/app
# name (QApplication.setApplicationName), and the About dialog.
# HOME_PAGE_TITLE: the longer heading shown at the top of the Home tab.
APP_TITLE = ""
HOME_PAGE_TITLE = ""


def _apply_config(cfg: Dict):
    """Make `cfg` (from bcfg.load_config) the active beamline config: set
    the module-level names that come from it and configure spec_core and
    chess_signals."""
    global CONFIG, APP_TITLE, HOME_PAGE_TITLE
    global EMAIL_SETTINGS_PATH, SLACK_SETTINGS_PATH, SLACK_DEFAULT_CHANNEL
    global _DEFAULT_Y_PRIORITY, _X_FALLBACK_COLUMNS, _CALIBRATION_FILES
    global LIVE_FRAME_EXT, _LIVE_FRAME_NO_RE
    CONFIG = cfg
    app = cfg["app"]
    APP_TITLE = app.get("title") or "SPEC Dashboard"
    HOME_PAGE_TITLE = app.get("home_title") or APP_TITLE

    prefix = app.get("settings_prefix") or "spec_dashboard"
    home = os.path.expanduser("~")
    EMAIL_SETTINGS_PATH = os.path.join(home, f".{prefix}_email_settings.json")
    SLACK_SETTINGS_PATH = os.path.join(home, f".{prefix}_slack_settings.json")
    SLACK_DEFAULT_CHANNEL = cfg["slack"].get("default_channel") or ""

    _DEFAULT_Y_PRIORITY = list(cfg["plot"].get("default_y_priority") or [])
    _X_FALLBACK_COLUMNS = list(cfg["plot"].get("x_fallback_columns") or [])
    _CALIBRATION_FILES = [n.lower() for n in cfg["timeline"].get("calibration_names") or []]

    LIVE_FRAME_EXT = cfg["live_image"].get("frame_extension") or ".cbf"
    _LIVE_FRAME_NO_RE = re.compile(r"(\d+)" + re.escape(LIVE_FRAME_EXT) + "$")

    sc.configure(cfg["data_layout"], cfg["spec_parsing"])
    csig.configure(cfg["signals"])


def _color_for(i: int) -> str:
    return PLOT_COLORS[i % len(PLOT_COLORS)]


class PlotPanel(QtWidgets.QWidget):
    """Wraps a pyqtgraph PlotWidget with a legend, zoom/reset toolbar, and a
    simple empty-state message."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addStretch()
        self.zoom_in_btn = QtWidgets.QToolButton()
        self.zoom_in_btn.setText("🔍+")
        self.zoom_in_btn.setToolTip("Zoom in")
        self.zoom_in_btn.setAutoRaise(True)
        self.zoom_in_btn.clicked.connect(lambda: self._zoom(0.8))
        self.zoom_out_btn = QtWidgets.QToolButton()
        self.zoom_out_btn.setText("🔍−")
        self.zoom_out_btn.setToolTip("Zoom out")
        self.zoom_out_btn.setAutoRaise(True)
        self.zoom_out_btn.clicked.connect(lambda: self._zoom(1.25))
        self.reset_view_btn = QtWidgets.QToolButton()
        self.reset_view_btn.setText("⟲ Reset")
        self.reset_view_btn.setToolTip("Reset view / fit to data")
        self.reset_view_btn.setAutoRaise(True)
        self.reset_view_btn.clicked.connect(self._reset_view)
        for b in (self.zoom_in_btn, self.zoom_out_btn, self.reset_view_btn):
            toolbar.addWidget(b)
        layout.addLayout(toolbar)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.legend = self.plot_widget.addLegend(offset=(10, 10))
        self._style_legend()
        # Draw a full box border around the plotting area — by default
        # pyqtgraph only draws the bottom/left axis lines, which reads as
        # "no box" around the data. A ViewBox border adds all four sides.
        self.plot_widget.getViewBox().setBorder(pg.mkPen(TEXT_PRIMARY, width=1))
        layout.addWidget(self.plot_widget)
        self._empty_label = QtWidgets.QLabel("", self.plot_widget)
        self._empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._empty_label.setProperty("secondaryText", True)
        self._empty_label.setStyleSheet("font-size: 13px;")
        self._empty_label.hide()

        # Hover crosshair + coordinate readout: a dashed vertical/horizontal
        # line pair plus a small text label showing the data-space (x, y)
        # under the mouse. Re-added after every clear() since
        # PlotWidget.clear() removes all items, including these.
        self._crosshair_v = pg.InfiniteLine(angle=90, movable=False)
        self._crosshair_h = pg.InfiniteLine(angle=0, movable=False)
        self._coord_label = pg.TextItem(anchor=(0, 1))
        # Peak/FWHM readouts are shown in the left sidebar (see
        # plot_actual_results_label / plot_fit_results_label), not drawn on
        # the chart itself — on-plot text annotations were hard to see and
        # had poor contrast in the Light theme, so they were removed.
        self._install_crosshair()
        self._mouse_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved
        )

    def _install_crosshair(self):
        """(Re)add the crosshair line/label items and style them from the
        current theme, then hide them until the mouse moves over the plot."""
        self._crosshair_v.setPen(pg.mkPen(TEXT_SECONDARY, width=1, style=QtCore.Qt.DashLine))
        self._crosshair_h.setPen(pg.mkPen(TEXT_SECONDARY, width=1, style=QtCore.Qt.DashLine))
        self._coord_label.setColor(TEXT_PRIMARY)
        self.plot_widget.addItem(self._crosshair_v, ignoreBounds=True)
        self.plot_widget.addItem(self._crosshair_h, ignoreBounds=True)
        self.plot_widget.addItem(self._coord_label, ignoreBounds=True)
        self._crosshair_v.setVisible(False)
        self._crosshair_h.setVisible(False)
        self._coord_label.setVisible(False)

    def _on_mouse_moved(self, evt):
        pos = evt[0] if isinstance(evt, (tuple, list)) else evt
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._crosshair_v.setVisible(False)
            self._crosshair_h.setVisible(False)
            self._coord_label.setVisible(False)
            return
        vb = self.plot_widget.getViewBox()
        view_point = vb.mapSceneToView(pos)
        vx, vy = view_point.x(), view_point.y()
        # setLogMode() plots log10(data) but keeps the ViewBox's own
        # coordinate space linear in that transformed space, so translate
        # back to the real data value for display when log mode is active.
        x_log = bool(getattr(self.plot_widget.getAxis("bottom"), "logMode", False))
        y_log = bool(getattr(self.plot_widget.getAxis("left"), "logMode", False))
        disp_x = 10 ** vx if x_log else vx
        disp_y = 10 ** vy if y_log else vy
        self._crosshair_v.setPos(vx)
        self._crosshair_h.setPos(vy)
        self._crosshair_v.setVisible(True)
        self._crosshair_h.setVisible(True)
        self._coord_label.setPos(vx, vy)
        self._coord_label.setText(f"x = {disp_x:.4g}\ny = {disp_y:.4g}")
        self._coord_label.setVisible(True)

    def apply_theme(self):
        """Re-style an already-built plot (background, axis colors,
        crosshair) after the user switches between dark/light themes."""
        self.plot_widget.setBackground(BG_PANEL)
        for axis_name in ("bottom", "left", "right", "top"):
            axis = self.plot_widget.getAxis(axis_name)
            if axis is not None:
                axis.setPen(pg.mkPen(TEXT_PRIMARY))
                axis.setTextPen(pg.mkPen(TEXT_PRIMARY))
        self._crosshair_v.setPen(pg.mkPen(TEXT_SECONDARY, width=1, style=QtCore.Qt.DashLine))
        self._crosshair_h.setPen(pg.mkPen(TEXT_SECONDARY, width=1, style=QtCore.Qt.DashLine))
        self._coord_label.setColor(TEXT_PRIMARY)
        self.plot_widget.getViewBox().setBorder(pg.mkPen(TEXT_PRIMARY, width=1))
        self._style_legend()

    def _style_legend(self):
        """Give the legend a solid, theme-matched background and border so
        the scan-color key stays clearly readable against any data behind
        it (rather than pyqtgraph's default near-transparent look, which
        could make it hard to tell apart from the chart or seem to
        disappear depending on what's plotted underneath), and lock it in
        place so an accidental click-drag on the chart can't carry the
        legend along with it."""
        if self.legend is None:
            return
        try:
            self.legend.setBrush(pg.mkBrush(BG_PANEL))
        except Exception:
            pass
        try:
            self.legend.setPen(pg.mkPen(TEXT_PRIMARY, width=1))
        except Exception:
            pass
        try:
            self.legend.setLabelTextColor(TEXT_PRIMARY)
        except Exception:
            pass
        try:
            self.legend.setLabelTextSize("10pt")
        except Exception:
            pass
        try:
            self.legend.setMovable(False)
        except Exception:
            pass

    def _zoom(self, factor: float):
        """Zoom the view in (factor < 1) or out (factor > 1) around its
        current center, in addition to the mouse-wheel/drag zoom pyqtgraph
        already supports."""
        self.plot_widget.getViewBox().scaleBy((factor, factor))

    def _reset_view(self):
        """Reset/auto-range the view to fit all plotted data."""
        self.plot_widget.getViewBox().autoRange()

    def clear(self):
        self.plot_widget.clear()
        # Re-create legend since .clear() removes plot items but leaves the
        # LegendItem itself in place. This is the actual root cause of the
        # legend appearing to vanish: pyqtgraph's addLegend() ONLY builds a
        # new LegendItem when plotItem.legend is still None -- if it's
        # anything else (per pyqtgraph's own docstring: "If a LegendItem has
        # already been created using this method, that item will be
        # returned rather than creating a new one"), addLegend() just hands
        # that same object back untouched. We removed the old legend from
        # the scene on the line below, but previously never reset
        # plotItem.legend to None afterward -- so the very next addLegend()
        # call kept returning that same, now-scene-less legend instead of
        # creating a fresh, properly re-parented one. The legend object
        # still existed and kept receiving newly-plotted items, which is
        # why earlier tests checking "legend is not None" and "legend has
        # items" passed -- but it could never actually be seen on screen
        # again. Since do_plot() calls this clear() before every single
        # render, that meant the legend was invisible on essentially every
        # real plot, not just some rare edge case.
        try:
            self.plot_widget.plotItem.legend.scene().removeItem(
                self.plot_widget.plotItem.legend
            )
        except Exception:
            pass
        self.plot_widget.plotItem.legend = None
        self.legend = self.plot_widget.addLegend(offset=(10, 10))
        self._style_legend()
        self.plot_widget.getViewBox().setBorder(pg.mkPen(TEXT_PRIMARY, width=1))
        self._install_crosshair()
        self._empty_label.hide()

    def show_empty(self, message: str):
        self.clear()
        self._empty_label.setText(message)
        self._empty_label.resize(self.plot_widget.size())
        self._empty_label.move(0, 0)
        self._empty_label.show()

    def set_log_x(self, on: bool):
        self.plot_widget.setLogMode(x=on, y=None)

    def set_log_y(self, on: bool):
        self.plot_widget.setLogMode(x=None, y=on)


def _multiselect_values(list_widget: QtWidgets.QListWidget) -> List[str]:
    return [item.text() for item in list_widget.selectedItems()]


def _populate_list(list_widget: QtWidgets.QListWidget, values: List[str], select_first=False):
    list_widget.clear()
    for v in values:
        list_widget.addItem(str(v))
    if select_first and list_widget.count() > 0:
        list_widget.setCurrentRow(0)


def _populate_combo(combo: QtWidgets.QComboBox, values: List[str]):
    combo.blockSignals(True)
    combo.clear()
    combo.addItems([str(v) for v in values])
    combo.blockSignals(False)


def _select_combo_text(combo: QtWidgets.QComboBox, text: str):
    idx = combo.findText(text)
    if idx >= 0:
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)


def _select_list_items(list_widget: QtWidgets.QListWidget, texts: List[str]):
    texts_set = set(texts)
    list_widget.blockSignals(True)
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        item.setSelected(item.text() in texts_set)
    list_widget.blockSignals(False)


def _populate_scan_list(list_widget: QtWidgets.QListWidget, scan_numbers: List[str], command_for_scan) -> None:
    """Populate a scan-selection list showing each scan's own SPEC command
    next to its number (e.g. "12: ascan th 0 8 40 0.1"), so it's easy to see
    which scan is which without cross-referencing the Scan Info tab. The
    real scan number — not the composite "N: command" display text — is
    stored in each item's Qt.UserRole data, which is what the selection
    helpers below read back, so nothing downstream needs to parse it out of
    the display text itself.

    Each entry is kept to a single line (word-wrap off) to save vertical
    space in the sidebar — a long command is elided with "…" instead of
    wrapping onto two or three lines, with the full, un-elided text always
    available as a tooltip on hover."""
    list_widget.setWordWrap(False)
    list_widget.setTextElideMode(QtCore.Qt.ElideRight)
    list_widget.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    list_widget.clear()
    for scan_str in scan_numbers:
        command = (command_for_scan(scan_str) or "").strip()
        display = f"{scan_str}: {command}" if command else str(scan_str)
        item = QtWidgets.QListWidgetItem(display)
        item.setData(QtCore.Qt.UserRole, str(scan_str))
        if command:
            item.setToolTip(f"{scan_str}: {command}")
        list_widget.addItem(item)


def _scan_list_selected_values(list_widget: QtWidgets.QListWidget) -> List[str]:
    """Like _multiselect_values(), but reads the real scan number back from
    each item's Qt.UserRole data instead of its command-annotated display
    text — for lists populated with _populate_scan_list()."""
    values = []
    for item in list_widget.selectedItems():
        value = item.data(QtCore.Qt.UserRole)
        values.append(value if value is not None else item.text())
    return values


def _select_scan_list_items(list_widget: QtWidgets.QListWidget, scan_strs: List[str]) -> None:
    """Like _select_list_items(), but matches against each item's stored
    scan number (Qt.UserRole) rather than its command-annotated display
    text — for lists populated with _populate_scan_list()."""
    wanted = set(scan_strs)
    list_widget.blockSignals(True)
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        value = item.data(QtCore.Qt.UserRole)
        if value is None:
            value = item.text()
        item.setSelected(value in wanted)
    list_widget.blockSignals(False)


# Default-selection logic, ported from the web dashboard's JS
# (extractMotorFromCommand / setXAxisFromScanNum / updatePlotControls) so the
# native GUI pre-fills the same sensible defaults when a file is loaded.
# Both lists come from the config's `plot:` section (set by _apply_config()).
_DEFAULT_Y_PRIORITY: List[str] = []
_X_FALLBACK_COLUMNS: List[str] = []


def _extract_motor_from_command(command: str, available_cols: List[str]) -> Optional[str]:
    """Parse a SPEC scan command (e.g. 'ascan mond 6.8 6.9 160 0.1') and return
    the swept motor name if it matches one of the available columns."""
    if not command:
        return None
    tokens = command.split()
    if len(tokens) < 2:
        return None
    motor = tokens[1]
    if motor in available_cols:
        return motor
    lower_map = {c.lower(): c for c in available_cols}
    return lower_map.get(motor.lower())


def _default_x_column(command: str, available_cols: List[str]) -> str:
    motor = _extract_motor_from_command(command, available_cols)
    if motor:
        return motor
    for fallback in _X_FALLBACK_COLUMNS:
        if fallback in available_cols:
            return fallback
    return available_cols[0] if available_cols else ""


def _default_y_columns(available_cols: List[str]) -> List[str]:
    for name in _DEFAULT_Y_PRIORITY:
        if name in available_cols:
            return [name]
    return [available_cols[0]] if available_cols else []


# Experiment Summary (Folder Timeline tab), ported from the web dashboard's
# isCalibration()/buildTimelineSummary()/renderTimelineSummary() logic.
# From the config's timeline.calibration_names (set by _apply_config()).
_CALIBRATION_FILES: List[str] = []


def _is_calibration(spec_file: str) -> bool:
    """Substring match (case-insensitive) against a spec filename to classify
    it as a calibration/reference file, excluded from "sample" counts."""
    lc = (spec_file or "").lower()
    return any(c in lc for c in _CALIBRATION_FILES)


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _short_label(spec_file: str, max_len: int = 14) -> str:
    """Shorten a SPEC filename for use as a chart tick label: drop the
    ".spec" suffix and, if still too long to read at a glance, truncate
    with an ellipsis (the full name remains available via the detail
    table and tooltips elsewhere on the tab)."""
    name = spec_file or ""
    if name.lower().endswith(".spec"):
        name = name[: -len(".spec")]
    if len(name) > max_len:
        name = name[: max_len - 1] + "…"
    return name


def _style_summary_chart(panel: "PlotPanel", title: str, y_label: str = ""):
    """Apply a consistent, higher-clarity look to an Experiment Summary
    chart: a bigger bold title, bigger axis-label/tick fonts, and a
    minimum size so the 3-across chart row doesn't squeeze them illegibly
    small."""
    panel.setMinimumSize(360, 300)
    panel.plot_widget.setTitle(title, size="13pt", bold=True)
    if y_label:
        panel.plot_widget.setLabel("left", y_label, **{"font-size": "11pt"})
    tick_font = QtGui.QFont()
    tick_font.setPointSize(10)
    for axis_name in ("bottom", "left"):
        axis = panel.plot_widget.getAxis(axis_name)
        if axis is not None:
            axis.setStyle(tickFont=tick_font)
    if panel.legend is not None:
        try:
            panel.legend.setLabelTextSize("10pt")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Live Image (Pilatus) tab — a folded-in, read-only .cbf detector-frame
# viewer, ported from the standalone pilatus_live_viewer.py desktop app so
# it lives alongside SPEC-file browsing/plotting in one window instead of
# needing a second program running side by side. The file-scanning helpers,
# background loader thread, and ROI-monitor logic below are the same
# algorithms as that standalone app; the UI is rebuilt as a plain QWidget
# (rather than a QMainWindow) so it can be embedded as one tab here, with
# its own status line standing in for the standalone app's statusBar().
#
# SAFETY — READ ONLY: exactly like the standalone app, this only ever reads
# .cbf files (each opened read-only via fabio and closed immediately). It
# never writes, renames, moves, or deletes anything in the watched folder.
# ═══════════════════════════════════════════════════════════════════════
LIVE_COLORMAPS = ["viridis", "inferno", "magma", "plasma",
                   "cividis", "turbo", "gray", "jet"]

# Detector frame extension to watch, and the regex that pulls the frame
# number out of a frame's filename -- both from the config's
# live_image.frame_extension (set by _apply_config()).
LIVE_FRAME_EXT = ".cbf"
_LIVE_FRAME_NO_RE = re.compile(r"(\d+)\.cbf$")


class _LiveFlowLayout(QtWidgets.QLayout):
    """A simple wrapping (flow) layout so the Live Image tab's control bar
    reflows onto more lines when the sidebar/window is narrow, instead of
    getting clipped or forcing the window wide. Identical logic to
    pilatus_live_viewer.py's FlowLayout."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x, y, line_height = rect.x(), rect.y(), 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


def _live_find_newest_cbf_fast(folder):
    """Newest .cbf in one folder, by FILENAME (Pilatus frames are
    zero-padded, so the highest name is the newest). Read-only."""
    best = None
    try:
        with os.scandir(folder) as it:
            for e in it:
                n = e.name
                if n.endswith(LIVE_FRAME_EXT) and (best is None or n > best):
                    best = n
    except OSError:
        return None, -1.0
    if best is None:
        return None, -1.0
    p = os.path.join(folder, best)
    try:
        m = os.path.getmtime(p)
    except OSError:
        m = -1.0
    return p, m


def _live_find_active_scan_dir(folder):
    """Locate the scan folder currently being written, by directory mtime.
    Read-only."""
    best_dir, best_mtime = None, -1.0
    try:
        for root, _dirs, files in os.walk(folder, followlinks=True):
            if not any(f.endswith(LIVE_FRAME_EXT) for f in files):
                continue
            try:
                m = os.path.getmtime(root)
            except OSError:
                continue
            if m > best_mtime:
                best_mtime, best_dir = m, root
    except OSError:
        return None
    return best_dir


def _live_size_stable(path, wait=0.02):
    """True if file size is unchanged across a tiny pause (avoids mid-write
    frames). Read-only."""
    try:
        s1 = os.path.getsize(path)
        if s1 <= 0:
            return False
        time.sleep(wait)
        s2 = os.path.getsize(path)
    except OSError:
        return False
    return s1 == s2


def _live_maxpool(a, f):
    """Downsample by max over f x f blocks (preserves sharp Bragg peaks)."""
    if f <= 1:
        return a
    h, w = a.shape
    h2, w2 = (h // f) * f, (w // f) * f
    if h2 == 0 or w2 == 0:
        return a
    a = a[:h2, :w2]
    return a.reshape(h2 // f, f, w2 // f, f).max(axis=(1, 3))


def _live_frame_number_from_path(path):
    """Extract the trailing zero-padded frame number from a Pilatus filename."""
    m = _LIVE_FRAME_NO_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else None


class LiveImageLoader(QtCore.QThread):
    """Background polling thread that finds and loads the newest .cbf frame
    in a watched folder — identical algorithm to pilatus_live_viewer.py's
    Loader, kept as its own QThread subclass so image decoding never blocks
    the main UI thread."""

    newImage = QtCore.Signal(object, str, float, object)  # data, path, mtime, active
    status = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.folder = None
        self.recurse = False
        self.interval = 0.1
        self.discover_interval = 2.0
        self.paused = False
        self._running = True
        self._active_dir = None
        self._active_last_path = None
        self._active_last_change = 0.0
        self._loaded_path = None
        self._loaded_mtime = -1.0

    def configure(self, **kw):
        if "folder" in kw and kw["folder"] is not None:
            self.folder = kw["folder"]
            self._active_dir = None
            self._loaded_path = None
            self._loaded_mtime = -1.0
        if "recurse" in kw and kw["recurse"] is not None:
            self.recurse = bool(kw["recurse"])
            self._active_dir = None
        if "interval" in kw and kw["interval"]:
            self.interval = max(0.03, float(kw["interval"]))
        if "discover_interval" in kw and kw["discover_interval"]:
            self.discover_interval = float(kw["discover_interval"])
        if "paused" in kw and kw["paused"] is not None:
            self.paused = bool(kw["paused"])

    def stop(self):
        self._running = False

    def _locate_recursive(self, folder):
        now = time.time()
        ad = self._active_dir
        if ad and os.path.isdir(ad):
            path, mtime = _live_find_newest_cbf_fast(ad)
            if path is not None:
                if path != self._active_last_path:
                    self._active_last_path = path
                    self._active_last_change = now
                if (now - self._active_last_change) < self.discover_interval:
                    return path, mtime
        active = _live_find_active_scan_dir(folder)
        self._active_dir = active
        if active:
            path, mtime = _live_find_newest_cbf_fast(active)
            self._active_last_path = path
            self._active_last_change = now
            return path, mtime
        return None, -1.0

    def run(self):
        while self._running:
            interval = self.interval
            if self.paused or not self.folder:
                time.sleep(interval)
                continue

            folder = self.folder
            if not os.path.isdir(folder):
                self.status.emit("Folder not found: %s" % folder)
                time.sleep(max(0.5, interval))
                continue

            if self.recurse:
                path, mtime = self._locate_recursive(folder)
            else:
                path, mtime = _live_find_newest_cbf_fast(folder)
                if path is None:
                    path, mtime = self._locate_recursive(folder)

            if path is None:
                self.status.emit("Searching for %s files under %s ..." % (LIVE_FRAME_EXT, folder))
                time.sleep(max(0.3, interval))
                continue

            if path == self._loaded_path and mtime == self._loaded_mtime:
                time.sleep(interval)
                continue

            if not _live_size_stable(path):
                time.sleep(interval)
                continue

            try:
                data = np.asarray(fabio.open(path).data)   # read-only open
            except Exception as exc:                        # noqa: BLE001
                self.status.emit("Load skipped (%s): %s"
                                 % (os.path.basename(path), exc))
                time.sleep(interval)
                continue

            self._loaded_path = path
            self._loaded_mtime = mtime
            self.newImage.emit(data, path, mtime, self._active_dir)
            time.sleep(interval)


class LiveImageTab(QtWidgets.QWidget):
    """The "Live Image (Pilatus)" tab: point it at a folder of .cbf frames
    and it continuously shows the newest one, with an optional ROI monitor
    plotting integrated intensity vs frame number as frames arrive — the
    same feature set as the standalone pilatus_live_viewer.py desktop app,
    folded in as one tab of this dashboard instead of a separate program."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = None
        self._disp_counts = None
        self._ds_factor = 1
        self._first = True
        self._fps_t0 = time.time()
        self._fps_n = 0
        self._last_fps = 0.0
        self._roi_x = deque(maxlen=200000)
        self._roi_y = deque(maxlen=200000)
        self._roi_counter = 0
        self._cur_active = None

        # Other views (currently just the Summary tab's mirrored image
        # panel) that want to show the exact same watched folder/frame as
        # this tab, without spinning up a second LiveImageLoader thread of
        # their own. Registered via register_mirror(); every frame this
        # tab renders through _show() is also pushed to each one, using
        # the same colormap/log/downsample/contrast settings, so both
        # views are always in sync.
        self._mirror_views: List[pg.ImageView] = []

        self._build_ui()

        # Only spin up the background polling thread when fabio is actually
        # available -- otherwise _build_ui() returns early (no folder/watch
        # controls at all) and there's nothing for the loader to do; skipping
        # it here matches the same "if fabio is None: bail out" guard already
        # used by apply_theme() and stop() below.
        self.loader = None
        if fabio is not None:
            self.loader = LiveImageLoader()
            self.loader.newImage.connect(self.on_new_image)
            self.loader.status.connect(self._set_status)
            self.loader.start()

    def _build_ui(self):
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        if fabio is None:
            warn = QtWidgets.QLabel(
                "The 'fabio' package isn't installed, so this tab can't "
                f"read {LIVE_FRAME_EXT} detector frames. Install it with:\n\n"
                "    pip install fabio\n\n"
                "then restart the dashboard to use the Live Image tab."
            )
            warn.setWordWrap(True)
            warn.setAlignment(QtCore.Qt.AlignCenter)
            v.addWidget(warn, 1)
            return

        r1 = QtWidgets.QHBoxLayout()
        r1.addWidget(QtWidgets.QLabel("Folder:"))
        self.path_edit = QtWidgets.QLineEdit()
        example = f", e.g. .../{sc.RAW_DATA_SUBDIRS[0]}" if sc.RAW_DATA_SUBDIRS else ""
        self.path_edit.setPlaceholderText(
            f"Paste a folder of {LIVE_FRAME_EXT} frames{example}")
        self.path_edit.setMinimumWidth(120)
        self.path_edit.returnPressed.connect(self.apply_folder)
        r1.addWidget(self.path_edit, 1)
        b_browse = QtWidgets.QPushButton("Browse…")
        b_browse.clicked.connect(self.browse_folder)
        r1.addWidget(b_browse)
        self.watch_btn = QtWidgets.QPushButton("Watch")
        self.watch_btn.clicked.connect(self.apply_folder)
        r1.addWidget(self.watch_btn)
        v.addLayout(r1)

        opts = QtWidgets.QWidget()
        flow = _LiveFlowLayout(opts, margin=0, spacing=6)

        self.recurse_cb = QtWidgets.QCheckBox("auto-search subfolders")
        self.recurse_cb.setToolTip(
            f"Always dive into subfolders to find the newest {LIVE_FRAME_EXT}. "
            f"(Even when off, watching a folder with no {LIVE_FRAME_EXT} will "
            "auto-search.)")
        self.recurse_cb.toggled.connect(lambda val: self.loader.configure(recurse=val))
        flow.addWidget(self.recurse_cb)

        self.log_cb = QtWidgets.QCheckBox("log")
        self.log_cb.setChecked(True)
        self.log_cb.toggled.connect(self.redraw_current)
        flow.addWidget(self.log_cb)

        self.auto_cb = QtWidgets.QCheckBox("auto-contrast")
        self.auto_cb.setChecked(True)
        flow.addWidget(self.auto_cb)

        flow.addWidget(QtWidgets.QLabel("cmap"))
        self.cmap_combo = QtWidgets.QComboBox()
        self.cmap_combo.addItems(LIVE_COLORMAPS)
        self.cmap_combo.currentTextChanged.connect(self.apply_colormap)
        flow.addWidget(self.cmap_combo)

        flow.addWidget(QtWidgets.QLabel("downsample"))
        self.ds_spin = QtWidgets.QSpinBox()
        self.ds_spin.setRange(1, 8)
        self.ds_spin.setValue(1)
        self.ds_spin.valueChanged.connect(self.redraw_current)
        flow.addWidget(self.ds_spin)

        flow.addWidget(QtWidgets.QLabel("refresh(s)"))
        self.int_spin = QtWidgets.QDoubleSpinBox()
        self.int_spin.setRange(0.03, 5.0)
        self.int_spin.setSingleStep(0.05)
        self.int_spin.setDecimals(2)
        self.int_spin.setValue(0.1)
        self.int_spin.valueChanged.connect(lambda val: self.loader.configure(interval=val))
        flow.addWidget(self.int_spin)

        self.pause_btn = QtWidgets.QPushButton("Freeze")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self.toggle_pause)
        flow.addWidget(self.pause_btn)

        self.roi_cb = QtWidgets.QCheckBox("ROI monitor")
        self.roi_cb.setToolTip(
            "Show a box on the image and plot its integrated intensity "
            "vs frame number as frames arrive.")
        self.roi_cb.toggled.connect(self.toggle_roi)
        flow.addWidget(self.roi_cb)

        self.roi_reset_btn = QtWidgets.QPushButton("reset plot")
        self.roi_reset_btn.clicked.connect(self.reset_roi_history)
        flow.addWidget(self.roi_reset_btn)

        v.addWidget(opts)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.imv = pg.ImageView()
        self.imv.ui.roiBtn.hide()
        self.imv.ui.menuBtn.hide()
        self.imv.view.invertY(True)
        self.splitter.addWidget(self.imv)

        self.roi_plot = pg.PlotWidget()
        self.roi_plot.setLabel("bottom", "frame number")
        self.roi_plot.setLabel("left", "ROI integrated intensity")
        self.roi_plot.showGrid(x=True, y=True, alpha=0.3)
        self.roi_curve = self.roi_plot.plot(pen=pg.mkPen("y", width=1),
                                            symbol="o", symbolSize=3,
                                            symbolBrush="y")
        self.roi_plot.hide()
        self.splitter.addWidget(self.roi_plot)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        v.addWidget(self.splitter, 1)

        self.roi = pg.RectROI([100, 100], [300, 300], pen=pg.mkPen("r", width=2))
        self.roi.addScaleHandle([1, 1], [0, 0])
        self.roi.addScaleHandle([0, 0], [1, 1])
        self.roi.sigRegionChanged.connect(self._roi_moved)

        self.apply_colormap(self.cmap_combo.currentText())

        # Status/cursor readout row — this tab is a QWidget (not a
        # QMainWindow), so there's no built-in statusBar(); a plain label
        # row at the bottom stands in for it.
        status_row = QtWidgets.QHBoxLayout()
        self.status_bar_label = QtWidgets.QLabel(
            "Read-only viewer ready. Enter a folder and click Watch.")
        self.status_bar_label.setProperty("secondaryText", True)
        status_row.addWidget(self.status_bar_label, 1)
        self.readout = QtWidgets.QLabel("cursor: -")
        status_row.addWidget(self.readout)
        v.addLayout(status_row)
        self.imv.getView().scene().sigMouseMoved.connect(self._on_mouse_moved)

    def _set_status(self, text):
        if hasattr(self, "status_bar_label"):
            self.status_bar_label.setText(text)

    def register_mirror(self, view: "pg.ImageView"):
        """Register another pg.ImageView (the Summary tab's, currently) to
        receive every frame this tab displays -- same data, same
        colormap/log/downsample/contrast -- without watching the folder a
        second time. Immediately syncs the mirror to whatever's on screen
        right now (colormap, and the current frame if one's already
        loaded) so it doesn't start out blank/stale if registered after
        this tab has already started watching."""
        if view in self._mirror_views:
            return
        self._mirror_views.append(view)
        if fabio is None:
            return
        try:
            view.view.invertY(True)
            view.ui.roiBtn.hide()
            view.ui.menuBtn.hide()
        except Exception:
            pass
        if hasattr(self, "cmap_combo"):
            self._apply_colormap_to(view, self.cmap_combo.currentText())
        if self._raw is not None:
            self._push_to_mirror(view, reset_range=True)

    def _apply_colormap_to(self, view: "pg.ImageView", name: str):
        cmap = None
        for source in (None, "matplotlib"):
            try:
                cmap = pg.colormap.get(name) if source is None \
                    else pg.colormap.get(name, source=source)
                if cmap is not None:
                    break
            except Exception:                  # noqa: BLE001
                continue
        if cmap is not None:
            try:
                view.setColorMap(cmap)
            except Exception:
                pass

    def _push_to_mirror(self, view: "pg.ImageView", reset_range: bool):
        if self._disp_counts is None:
            return
        disp = np.log10(self._disp_counts + 1.0) if self.log_cb.isChecked() else self._disp_counts
        auto = self.auto_cb.isChecked()
        try:
            view.setImage(disp, autoLevels=auto, autoRange=reset_range,
                          autoHistogramRange=auto)
        except Exception:
            pass

    # -- actions ------------------------------------------------------------
    def browse_folder(self):
        start = self.path_edit.text().strip() or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select folder to watch", start)
        if chosen:
            self.path_edit.setText(chosen)
            self.apply_folder()

    def apply_folder(self):
        folder = self.path_edit.text().strip()
        if not folder:
            return
        self._first = True
        self.loader.configure(folder=folder)
        self._set_status("Watching: %s" % folder)

    def apply_colormap(self, name):
        cmap = None
        for source in (None, "matplotlib"):
            try:
                cmap = pg.colormap.get(name) if source is None \
                    else pg.colormap.get(name, source=source)
                if cmap is not None:
                    break
            except Exception:                  # noqa: BLE001
                continue
        if cmap is not None:
            self.imv.setColorMap(cmap)
        for view in self._mirror_views:
            self._apply_colormap_to(view, name)

    def toggle_pause(self, checked):
        self.loader.configure(paused=checked)
        self.pause_btn.setText("Frozen - click to resume" if checked else "Freeze")

    # -- ROI monitor --------------------------------------------------------
    def toggle_roi(self, on):
        view = self.imv.getView()
        if on:
            view.addItem(self.roi)
            self.roi_plot.show()
            self.splitter.setSizes([max(self.height() - 240, 100), 220])
            self._update_roi_point(append=False)
        else:
            view.removeItem(self.roi)
            self.roi_plot.hide()

    def reset_roi_history(self):
        self._roi_x.clear()
        self._roi_y.clear()
        self.roi_curve.setData([], [])

    def _roi_sum(self):
        if self._disp_counts is None:
            return None
        try:
            region = self.roi.getArrayRegion(self._disp_counts, self.imv.getImageItem())
        except Exception:                      # noqa: BLE001
            return None
        if region is None or region.size == 0:
            return None
        return float(np.nansum(region))

    def _roi_moved(self):
        if self.roi_cb.isChecked():
            self._update_roi_point(append=False)

    def _update_roi_point(self, append, frame_no=None):
        val = self._roi_sum()
        if val is None:
            return
        if append:
            self._roi_x.append(frame_no if frame_no is not None else self._roi_counter)
            self._roi_y.append(val)
        elif self._roi_y:
            self._roi_y[-1] = val
        if self._roi_x:
            self.roi_curve.setData(list(self._roi_x), list(self._roi_y))

    # -- display ------------------------------------------------------------
    def redraw_current(self, *_):
        if self._raw is not None:
            self._show(self._raw, reset_range=False)

    def _show(self, data, reset_range):
        counts = np.asarray(data, dtype=np.float32)
        counts = np.where(counts < 0, 0.0, counts)
        f = self.ds_spin.value()
        if f > 1:
            counts = _live_maxpool(counts, f)
        self._disp_counts = counts
        self._ds_factor = f
        disp = np.log10(counts + 1.0) if self.log_cb.isChecked() else counts
        auto = self.auto_cb.isChecked()
        self.imv.setImage(disp, autoLevels=auto, autoRange=reset_range,
                          autoHistogramRange=auto)
        for view in self._mirror_views:
            self._push_to_mirror(view, reset_range=reset_range)

    def _on_mouse_moved(self, pos):
        if self._disp_counts is None:
            return
        img_item = self.imv.getImageItem()
        vb = self.imv.getView()
        if not vb.sceneBoundingRect().contains(pos):
            self.readout.setText("cursor: -")
            return
        mp = img_item.mapFromScene(pos)
        col = int(mp.x())
        row = int(mp.y())
        h, w = self._disp_counts.shape
        if 0 <= row < h and 0 <= col < w:
            cnt = self._disp_counts[row, col]
            f = self._ds_factor
            det_r, det_c = row * f, col * f
            self.readout.setText(
                "pixel (row=%d, col=%d)   counts=%d%s"
                % (det_r, det_c, int(cnt), ("  [%dx binned]" % f) if f > 1 else ""))
        else:
            self.readout.setText("cursor: -")

    def on_new_image(self, data, path, mtime, active_dir):
        self._raw = data
        self._show(data, reset_range=self._first)
        self._first = False

        frame_no = _live_frame_number_from_path(path)
        self._roi_counter += 1
        new_scan = (active_dir != self._cur_active) or (
            frame_no is not None and self._roi_x and frame_no < self._roi_x[-1])
        if new_scan:
            self._cur_active = active_dir
            self.reset_roi_history()
        if self.roi_cb.isChecked():
            self._update_roi_point(append=True, frame_no=frame_no)

        self._fps_n += 1
        now = time.time()
        dt = now - self._fps_t0
        if dt >= 1.0:
            self._last_fps = self._fps_n / dt
            self._fps_t0 = now
            self._fps_n = 0

        try:
            mx = int(np.max(data))
        except ValueError:
            mx = 0
        following = ""
        watched = self.path_edit.text().strip()
        if active_dir and watched and os.path.abspath(active_dir) != os.path.abspath(watched):
            following = "  following: %s" % os.path.basename(active_dir.rstrip("/"))
        self._set_status(
            "%s   (%d x %d)   max=%d cts   age=%.1fs   %.1f fps%s"
            % (os.path.basename(path), data.shape[0], data.shape[1], mx,
               now - mtime, self._last_fps, following))

    def apply_theme(self):
        """Re-skin the ROI plot after a theme switch (the image view itself
        uses its own colormap, independent of the app theme)."""
        if fabio is None or not hasattr(self, "roi_plot"):
            return
        self.roi_plot.setBackground(BG_PANEL)
        for axis_name in ("bottom", "left"):
            axis = self.roi_plot.getAxis(axis_name)
            if axis is not None:
                axis.setPen(pg.mkPen(TEXT_PRIMARY))
                axis.setTextPen(pg.mkPen(TEXT_PRIMARY))

    def stop(self):
        """Stop the background loader thread — called from the main
        window's closeEvent so the app can exit cleanly instead of leaving
        a polling thread running."""
        if fabio is None:
            return
        try:
            self.loader.stop()
            self.loader.wait(2000)
        except Exception:
            pass


class EmailSendDialog(QtWidgets.QDialog):
    """"Send by Email" dialog for the Plot tab. Lets the user pick their
    email provider (auto-filling SMTP host/port/TLS), enter their own
    address + password, a recipient, subject and body, and choose whether
    to attach the currently-plotted data (CSV) and/or the plot image (PNG,
    already including any Notes caption) — both handed in ready-made by
    open_email_dialog() so this dialog never has to know how the chart or
    CSV are actually produced.

    Sending uses the user's own SMTP credentials (smtplib) rather than any
    third-party mail service, matching what the user asked for. Connection
    settings (and, only if separately opted into, the password) can be
    remembered locally in EMAIL_SETTINGS_PATH for next time."""

    def __init__(self, parent, *, csv_bytes: Optional[bytes], png_bytes: Optional[bytes],
                 default_subject: str):
        super().__init__(parent)
        self.setWindowTitle("Email Plot Data")
        self.setMinimumWidth(420)
        self._csv_bytes = csv_bytes
        self._png_bytes = png_bytes
        self._build_ui(default_subject)
        self._load_remembered_settings()

    def _build_ui(self, default_subject: str):
        layout = QtWidgets.QVBoxLayout(self)

        conn_box = QtWidgets.QGroupBox("Your email account (SMTP)")
        form = QtWidgets.QFormLayout(conn_box)

        self.provider_combo = QtWidgets.QComboBox()
        self.provider_combo.addItems(list(EMAIL_PROVIDER_PRESETS.keys()))
        self.provider_combo.currentTextChanged.connect(self._apply_provider_preset)
        form.addRow("Provider:", self.provider_combo)

        self.host_edit = QtWidgets.QLineEdit()
        form.addRow("SMTP server:", self.host_edit)

        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(587)
        form.addRow("Port:", self.port_spin)

        self.tls_chk = QtWidgets.QCheckBox("Use TLS (recommended)")
        self.tls_chk.setChecked(True)
        form.addRow("", self.tls_chk)

        self.from_edit = QtWidgets.QLineEdit()
        self.from_edit.setPlaceholderText("you@example.com")
        form.addRow("Your address:", self.from_edit)

        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_edit.setToolTip(
            "For Gmail/Outlook/Yahoo/iCloud this is usually an app-specific "
            "password, not your normal login password — check your "
            "provider's account security settings if the send fails."
        )
        form.addRow("Password:", self.password_edit)

        self.remember_chk = QtWidgets.QCheckBox("Remember these settings on this computer")
        form.addRow("", self.remember_chk)

        self.remember_password_chk = QtWidgets.QCheckBox(
            "Also remember my password (stored in plain text)"
        )
        self.remember_password_chk.setEnabled(False)
        self.remember_chk.toggled.connect(self.remember_password_chk.setEnabled)
        self.remember_chk.toggled.connect(
            lambda checked: (not checked) and self.remember_password_chk.setChecked(False)
        )
        form.addRow("", self.remember_password_chk)

        layout.addWidget(conn_box)

        msg_box = QtWidgets.QGroupBox("Message")
        mform = QtWidgets.QFormLayout(msg_box)
        self.to_edit = QtWidgets.QLineEdit()
        self.to_edit.setPlaceholderText("recipient@example.com")
        mform.addRow("To:", self.to_edit)
        self.subject_edit = QtWidgets.QLineEdit(default_subject)
        mform.addRow("Subject:", self.subject_edit)
        self.body_edit = QtWidgets.QPlainTextEdit()
        self.body_edit.setPlaceholderText("Optional message…")
        self.body_edit.setFixedHeight(80)
        mform.addRow("Message:", self.body_edit)
        layout.addWidget(msg_box)

        attach_box = QtWidgets.QGroupBox("Attachments")
        aform = QtWidgets.QVBoxLayout(attach_box)
        self.attach_csv_chk = QtWidgets.QCheckBox("Attach plotted data (CSV)")
        self.attach_csv_chk.setChecked(self._csv_bytes is not None)
        self.attach_csv_chk.setEnabled(self._csv_bytes is not None)
        if self._csv_bytes is None:
            self.attach_csv_chk.setToolTip("No plotted data is currently available.")
        aform.addWidget(self.attach_csv_chk)
        self.attach_png_chk = QtWidgets.QCheckBox("Attach plot image (PNG, with Notes)")
        self.attach_png_chk.setChecked(self._png_bytes is not None)
        self.attach_png_chk.setEnabled(self._png_bytes is not None)
        if self._png_bytes is None:
            self.attach_png_chk.setToolTip("No plot image could be rendered.")
        aform.addWidget(self.attach_png_chk)
        layout.addWidget(attach_box)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._on_send_clicked)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.send_btn)
        layout.addLayout(btn_row)

        self._apply_provider_preset(self.provider_combo.currentText())

    def _apply_provider_preset(self, provider_name: str):
        host, port, use_tls = EMAIL_PROVIDER_PRESETS.get(provider_name, ("", 587, True))
        self.host_edit.setText(host)
        self.port_spin.setValue(port)
        self.tls_chk.setChecked(use_tls)

    def _load_remembered_settings(self):
        try:
            with open(EMAIL_SETTINGS_PATH, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return
        provider = data.get("provider")
        if provider in EMAIL_PROVIDER_PRESETS:
            self.provider_combo.setCurrentText(provider)
        if "host" in data:
            self.host_edit.setText(data.get("host", ""))
        if "port" in data:
            self.port_spin.setValue(int(data.get("port", 587)))
        if "use_tls" in data:
            self.tls_chk.setChecked(bool(data.get("use_tls", True)))
        self.from_edit.setText(data.get("from_address", ""))
        if data.get("password"):
            self.password_edit.setText(data.get("password", ""))
        self.remember_chk.setChecked(True)
        self.remember_password_chk.setChecked(bool(data.get("password")))

    def _save_remembered_settings(self):
        data = {
            "provider": self.provider_combo.currentText(),
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "use_tls": self.tls_chk.isChecked(),
            "from_address": self.from_edit.text().strip(),
        }
        if self.remember_password_chk.isChecked():
            data["password"] = self.password_edit.text()
        try:
            with open(EMAIL_SETTINGS_PATH, "w") as f:
                json.dump(data, f)
            try:
                os.chmod(EMAIL_SETTINGS_PATH, 0o600)
            except OSError:
                pass
        except OSError as exc:
            print("Could not save remembered email settings:", exc)

    def _on_send_clicked(self):
        host = self.host_edit.text().strip()
        port = self.port_spin.value()
        from_addr = self.from_edit.text().strip()
        password = self.password_edit.text()
        to_addr = self.to_edit.text().strip()

        if not host:
            QtWidgets.QMessageBox.warning(self, "Email Plot Data", "Enter an SMTP server address.")
            return
        if not _EMAIL_RE.match(from_addr):
            QtWidgets.QMessageBox.warning(self, "Email Plot Data", "Enter a valid 'from' address.")
            return
        if not _EMAIL_RE.match(to_addr):
            QtWidgets.QMessageBox.warning(self, "Email Plot Data", "Enter a valid recipient address.")
            return
        if not password:
            QtWidgets.QMessageBox.warning(self, "Email Plot Data", "Enter your password.")
            return

        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = self.subject_edit.text().strip() or "SPEC Dashboard plot"
        msg.attach(MIMEText(self.body_edit.toPlainText(), "plain"))

        if self.attach_csv_chk.isChecked() and self._csv_bytes is not None:
            part = MIMEBase("text", "csv")
            part.set_payload(self._csv_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename="plot_data.csv")
            msg.attach(part)

        if self.attach_png_chk.isChecked() and self._png_bytes is not None:
            image_part = MIMEImage(self._png_bytes, _subtype="png")
            image_part.add_header("Content-Disposition", "attachment", filename="plot.png")
            msg.attach(image_part)

        try:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if self.tls_chk.isChecked():
                    server.starttls()
                server.login(from_addr, password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Sending Email", str(exc))
            return

        if self.remember_chk.isChecked():
            self._save_remembered_settings()

        QtWidgets.QMessageBox.information(self, "Email Plot Data", f"Email sent to {to_addr}.")
        self.accept()


class SpecDashboardApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 900)

        # State
        self.root_path = os.path.expanduser(CONFIG["app"].get("default_root") or "~")
        if not os.path.isdir(self.root_path):
            self.root_path = os.path.expanduser("~")
        self.current_browse_path = self.root_path
        self.df = None
        self.columns: List[str] = []
        self.metadata: Dict = {}
        self.scan_info: Dict[int, Dict] = {}
        self.last_plot_info: Optional[Dict] = None
        self._suspend_auto_plot = False

        # Optional "reference file" data, loaded independently of the main
        # file and overlaid as an extra curve on the Plot tab (folded in
        # from the old standalone Compare Plot tab).
        self.df2 = None
        self.columns2: List[str] = []
        self.metadata2: Dict = {}
        self.scan_info2: Dict[int, Dict] = {}
        self._reference_file_path: Optional[str] = None

        # Snapshot of the curve(s) on screen right before an Auto-Refresh
        # tick redraws the plot, so the previous curve can be re-drawn as a
        # faint dashed "ghost" overlay alongside the newly refreshed one.
        self._auto_refresh_prev_curves: List[Dict] = []
        self._is_auto_refresh_tick = False

        self._tree_items: Dict[str, tuple] = {}
        # Tab key (see bcfg.TAB_LABELS) -> its page widget, filled by _add_tab().
        self._tab_widgets: Dict[str, QtWidgets.QWidget] = {}
        self._timeline_rows: List[Dict] = []
        self._timeline_summary_visible = False
        self._timeline_folder_path: Optional[str] = None
        self._timeline_summary_data: Optional[Dict] = None
        self._timeline_summary_panels: Optional[Dict] = None
        self._loaded_file_path: Optional[str] = None
        self.current_theme = "dark"

        # Slack Beam Alerts: tracks the no-beam state as of the *previous*
        # _beam_signals_tick() so _maybe_alert_beam_slack() can tell a
        # transition (beam just lost / beam just restored) apart from an
        # unchanged, already-alerted-on state -- one alert per transition,
        # not one alert per second for however long the no-beam period
        # lasts. None means "no reading yet" (app just started), which is
        # deliberately treated as "no transition" on the very first tick.
        self._last_no_beam_state: Optional[bool] = None

        self._watch_timer = QtCore.QTimer(self)
        self._watch_timer.setInterval(3000)
        self._watch_timer.timeout.connect(self._check_file_changed)
        self._watch_mtime: Optional[float] = None

        self._autorefresh_timer = QtCore.QTimer(self)
        self._autorefresh_timer.timeout.connect(self._auto_refresh_tick)

        # Summary tab: a always-live, watch-only view (side-by-side latest-
        # scan plot + mirrored Live Image feed) that refreshes on its own
        # 0.1s timer, independent of the Plot tab's own Auto-Refresh
        # checkbox/interval and its own file-change-tracking mtime — kept
        # separate so the two features can never race each other the way
        # the plain file-change watcher and Auto-Refresh once did (see
        # _start_file_watch's comment). Only runs while the Summary tab is
        # actually the visible tab (started/stopped in _on_tab_changed) so
        # it isn't silently reparsing the SPEC file in the background 10x/
        # second while the user is looking at a different tab.
        self._summary_timer = QtCore.QTimer(self)
        self._summary_timer.setInterval(100)
        self._summary_timer.timeout.connect(self._summary_refresh_tick)
        self._summary_watch_mtime: Optional[float] = None

        # Summary tab: the config's live channels, sourced via
        # chess_signals.get_live_values(), refreshed once a second —
        # deliberately a separate, slower timer from the 0.1s plot-refresh
        # timer above, since these numbers don't need to move nearly as
        # fast and there's no reason to re-run the column-matching /
        # (optional) network round-trip 10x/second.
        #
        # Runs continuously for the whole lifetime of the app, NOT just
        # while the Summary tab is visible (unlike _summary_timer above) --
        # started once below, right after _build_central(), and never
        # stopped except in closeEvent(). This is what lets Slack Alerts
        # (now its own separate tab; see _build_slack_alerts_tab()) keep
        # detecting no-beam transitions and posting to Slack in the
        # background even while the user is looking at some other tab,
        # not just while Overall Summary happens to be the visible one.
        # The Overall Summary tab's own readout labels/no_beam_banner still
        # only update visibly while that tab is on screen, same as always
        # (Qt just doesn't bother repainting a hidden widget) -- only the
        # underlying fetch-and-check now runs regardless of which tab is
        # showing.
        self._beam_signals_timer = QtCore.QTimer(self)
        self._beam_signals_timer.setInterval(1000)
        self._beam_signals_timer.timeout.connect(self._beam_signals_tick)

        self._build_menu()
        self._build_central()
        self._build_statusbar()

        self._beam_signals_tick()
        self._beam_signals_timer.start()

        self.status_label.setText("Ready.")
        self._refresh_file_tree()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")

        act_root = QtGui.QAction("Set Root Folder…", self)
        act_root.setShortcut("Ctrl+O")
        act_root.triggered.connect(self.set_root_folder)
        file_menu.addAction(act_root)

        act_load = QtGui.QAction("Load SPEC File…", self)
        act_load.triggered.connect(self.load_file_dialog)
        file_menu.addAction(act_load)

        act_sample = QtGui.QAction("Load Sample Data", self)
        act_sample.triggered.connect(self.load_sample_data)
        file_menu.addAction(act_sample)

        act_reload = QtGui.QAction("Reload Current File", self)
        act_reload.triggered.connect(self.reload_file)
        file_menu.addAction(act_reload)

        file_menu.addSeparator()
        act_exit = QtGui.QAction("Exit", self)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        view_menu = menubar.addMenu("&View")
        theme_menu = view_menu.addMenu("Theme")
        theme_group = QtGui.QActionGroup(self)
        theme_group.setExclusive(True)

        self.act_theme_dark = QtGui.QAction("Dark", self, checkable=True)
        self.act_theme_dark.setChecked(True)
        self.act_theme_dark.triggered.connect(lambda: self.set_theme("dark"))
        theme_group.addAction(self.act_theme_dark)
        theme_menu.addAction(self.act_theme_dark)

        self.act_theme_light = QtGui.QAction("Light", self, checkable=True)
        self.act_theme_light.triggered.connect(lambda: self.set_theme("light"))
        theme_group.addAction(self.act_theme_light)
        theme_menu.addAction(self.act_theme_light)

        help_menu = menubar.addMenu("&Help")
        act_shortcuts = QtGui.QAction("Keyboard Shortcuts", self)
        act_shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(act_shortcuts)

        act_about = QtGui.QAction("About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

        # Global shortcuts to jump to tabs
        sc_plot = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+P"), self)
        sc_plot.activated.connect(lambda: self._goto_tab("plot"))
        sc_scaninfo = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+T"), self)
        sc_scaninfo.activated.connect(lambda: self._goto_tab("scan_info"))
        sc_export = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+E"), self)
        sc_export.activated.connect(lambda: self._goto_tab("export"))

    def _show_shortcuts(self):
        QtWidgets.QMessageBox.information(
            self, "Keyboard Shortcuts",
            "Ctrl+O — Set root / browse\n"
            f"Ctrl+P — Jump to {self._tab_label('plot')} tab\n"
            f"Ctrl+T — Jump to {self._tab_label('scan_info')} tab\n"
            f"Ctrl+E — Jump to {self._tab_label('export')} tab",
        )

    def _show_about(self):
        QtWidgets.QMessageBox.information(
            self, "About",
            f"{APP_TITLE}\n\n{CONFIG['app'].get('about_text', '')}\n\n"
            f"Config: {CONFIG.get('_path', '')}",
        )

    @staticmethod
    def _tab_label(key: str) -> str:
        for entry in CONFIG["tabs"]:
            if entry["key"] == key:
                return entry["label"]
        return bcfg.TAB_LABELS[key]

    def _add_tab(self, key: str, widget: QtWidgets.QWidget):
        self._tab_widgets[key] = widget
        self.tabs.addTab(widget, self._tab_label(key))

    def _goto_tab(self, key: str):
        widget = self._tab_widgets.get(key)
        if widget is not None and self.tabs.indexOf(widget) != -1:
            self.tabs.setCurrentWidget(widget)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def set_theme(self, name: str):
        if name not in THEMES or name == self.current_theme:
            return
        _apply_theme_globals(name)
        self.current_theme = name

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(_build_qss())
        pg.setConfigOptions(antialias=True, background=BG_PANEL, foreground=TEXT_PRIMARY,
                             imageAxisOrder="row-major")

        # Keep the menu checkmarks in sync (relevant if set_theme is ever
        # called from somewhere other than the menu actions themselves).
        self.act_theme_dark.setChecked(name == "dark")
        self.act_theme_light.setChecked(name == "light")

        # Re-skin already-constructed plot panels (pg.setConfigOptions only
        # affects newly created PlotWidgets, not ones already on screen).
        for panel in (self.plot_panel, self.plot_fit_residuals_panel, self.summary_plot_panel):
            panel.apply_theme()
        if hasattr(self, "live_image_tab"):
            self.live_image_tab.apply_theme()

        # Re-render whatever is currently on screen so colors that depend on
        # theme (fit overlay, reference/ghost overlays, residuals, plotted
        # curves) pick up the new palette.
        self.do_plot()
        self._render_summary_plot()
        if self._timeline_summary_visible and self._timeline_summary_data:
            try:
                self._build_timeline_summary()
            except Exception:
                pass

        self.status_label.setText(f"Theme switched to {name.capitalize()}.")

    # ------------------------------------------------------------------
    # Central layout
    # ------------------------------------------------------------------
    def _build_central(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)

        # Top bar
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addWidget(QtWidgets.QLabel("Root:"))
        self.root_edit = QtWidgets.QLineEdit(self.root_path)
        top_bar.addWidget(self.root_edit, 1)
        btn_go = QtWidgets.QPushButton("Go")
        btn_go.clicked.connect(self._on_root_edit_go)
        top_bar.addWidget(btn_go)
        btn_browse = QtWidgets.QPushButton("Browse Folder…")
        btn_browse.clicked.connect(self.set_root_folder)
        top_bar.addWidget(btn_browse)
        btn_sample = QtWidgets.QPushButton("Load Sample Data")
        btn_sample.clicked.connect(self.load_sample_data)
        top_bar.addWidget(btn_sample)
        self.loaded_file_label = QtWidgets.QLabel("No file loaded.")
        self.loaded_file_label.setProperty("secondaryText", True)
        top_bar.addWidget(self.loaded_file_label, 1)

        top_bar.addWidget(QtWidgets.QLabel("Auto-Refresh:"))
        self.autorefresh_chk = QtWidgets.QCheckBox()
        self.autorefresh_chk.setToolTip(
            "Poll the loaded file for changes and automatically reload + "
            "re-plot the latest scan (matches the web dashboard's Auto-Refresh)."
        )
        self.autorefresh_chk.toggled.connect(self._on_autorefresh_toggled)
        top_bar.addWidget(self.autorefresh_chk)
        self.autorefresh_interval_combo = QtWidgets.QComboBox()
        # "0.1s" is the fastest interval offered — QTimer can technically go
        # lower, but each tick re-reads and re-parses the whole file from
        # disk, so anything faster risks flooding the UI thread on larger
        # SPEC files without any real benefit.
        self.autorefresh_interval_combo.addItems(["0.1s", "1s", "5s", "10s", "30s", "60s"])
        self.autorefresh_interval_combo.setCurrentText("10s")
        self.autorefresh_interval_combo.setToolTip(
            "How often to check the file for changes. 0.1s is the fastest "
            "option available."
        )
        self.autorefresh_interval_combo.currentTextChanged.connect(self._on_autorefresh_interval_changed)
        top_bar.addWidget(self.autorefresh_interval_combo)

        self.compare_prev_chk = QtWidgets.QCheckBox("Compare previous vs current")
        self.compare_prev_chk.setToolTip(
            "While Auto-Refresh is on, keep the plot from right before each "
            "refresh visible as a faint dashed ghost overlay, so you can "
            "directly compare the previous plot against the newly refreshed "
            "one without needing a separate tab."
        )
        top_bar.addWidget(self.compare_prev_chk)
        outer.addLayout(top_bar)

        # Splitter: file tree (left) / tabs (right)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(splitter, 1)

        self.file_tree = QtWidgets.QTreeWidget()
        self.file_tree.setHeaderLabels(["Name", "Type", "Size"])
        self.file_tree.setColumnWidth(0, 260)
        self.file_tree.itemDoubleClicked.connect(self._on_tree_double_click)
        splitter.addWidget(self.file_tree)

        self.tabs = QtWidgets.QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1080])

        self._build_home_tab()
        self._build_plot_tab()
        self._build_timeline_tab()
        self._build_scan_info_tab()
        self._build_motor_positions_tab()
        self._build_live_image_tab()
        self._build_summary_tab()
        self._build_slack_alerts_tab()
        self._build_export_tab()

        # Re-order the tabs on-screen to match the config's `tabs:` list,
        # WITHOUT touching the _build_X_tab() call order above -- that order
        # has real dependencies (e.g. _build_summary_tab() reads
        # self.live_image_tab, so _build_live_image_tab() must still run
        # first) that are independent of what order the tabs should visually
        # appear in. Tabs with `show: false` are still built (other code
        # refers to their widgets) but taken off the tab bar.
        target_index = 0
        for entry in CONFIG["tabs"]:
            widget = self._tab_widgets[entry["key"]]
            current_index = self.tabs.indexOf(widget)
            if not entry["show"]:
                self.tabs.removeTab(current_index)
                continue
            if current_index != target_index:
                self.tabs.removeTab(current_index)
                self.tabs.insertTab(target_index, widget, entry["label"])
            target_index += 1

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_live_image_tab(self):
        """"Live Image (Pilatus)" tab: watch a folder of .cbf detector
        frames and show the newest one in real time, with an optional ROI
        monitor — folded in from the standalone pilatus_live_viewer.py app
        (see LiveImageTab above) so both live views are available in one
        window."""
        self.live_image_tab = LiveImageTab()
        self._add_tab("live_image", self.live_image_tab)

    def _build_summary_tab(self):
        """"Summary" tab: a minimal, watch-only view with the latest-scan
        plot and the Live Image feed side by side, both always refreshing
        on their own — nothing to configure here, no buttons to click. The
        plot side re-reads the currently loaded SPEC file every 0.1s and
        re-draws whatever scan is newest, using the same X/Y columns as
        the last plot made on the Plot tab (or sensible defaults if none
        has been made yet); the image side simply mirrors whatever the
        Live Image (Pilatus) tab is currently watching (same folder, same
        colormap/log/contrast/downsample settings) via
        LiveImageTab.register_mirror(), so there's only ever one folder-
        watching thread even though the frame shows up in two places."""
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)

        self.summary_status_label = QtWidgets.QLabel(
            "Always-live view — updates automatically, nothing to click."
        )
        self.summary_status_label.setProperty("secondaryText", True)
        layout.addWidget(self.summary_status_label)

        # One titled row of small boxed "cards" per `summary.groups` entry
        # in the config, each showing one channel from `signals.channels`
        # (name + big value), refreshed once a second by
        # _beam_signals_tick() via chess_signals.py. A group with no
        # channels still shows its title, so every configured section is
        # visible even when it's blank.
        self.signal_labels: Dict[str, List[QtWidgets.QLabel]] = {}
        self.beam_network_checkbox = QtWidgets.QCheckBox(
            "Try live network fetch (on-site/CHESS network only)"
        )
        self.beam_network_checkbox.setChecked(
            bool(CONFIG["signals"].get("network_fetch_default", True))
        )
        # Network-first priority: when checked, a channel's live reading
        # overrides the loaded SPEC file's value rather than just filling
        # gaps, since the SPEC file's last row is a static snapshot that
        # generally never changes again (see csig.get_live_values()).
        self.beam_network_checkbox.setToolTip(
            "Only finds anything on-site/the CHESS network, so uncheck "
            "this if you're off-site. When checked, every channel with a "
            f"PV in the beamline config is read live from {csig.BASE_URL} "
            "and that live value overrides the loaded SPEC file's value "
            "(falling back to the SPEC file's value only if the network "
            "request for that channel fails or is unreachable). Channels "
            "without a PV always come from the SPEC file."
        )

        groups = CONFIG["summary"]["groups"]
        for i, group in enumerate(groups):
            header = QtWidgets.QLabel(group.get("title") or "")
            header.setStyleSheet("font-size: 11pt; font-weight: bold;")
            layout.addWidget(header)
            row = QtWidgets.QHBoxLayout()
            for canonical in group.get("channels") or []:
                row.addWidget(self._make_signal_card(canonical))
            row.addStretch(1)
            if i == 0:
                # Governs every channel, not just this group's; it sits at
                # the end of the first row.
                row.addWidget(self.beam_network_checkbox)
            layout.addLayout(row)
        if not groups:
            layout.addWidget(self.beam_network_checkbox)

        # "No Beam" banner -- hidden by default, shown/hidden every second
        # by _beam_signals_tick() based on csig.is_no_beam() of the
        # config's signals.no_beam.channel. Deliberately a persistent,
        # self-clearing banner rather than a popup/dialog, which would pop
        # up every second for however long there's no beam.
        no_beam_channel = CONFIG["signals"]["no_beam"].get("channel")
        no_beam_label = csig.CHANNELS[no_beam_channel]["label"] if no_beam_channel else ""
        self.no_beam_banner = QtWidgets.QLabel(f"⚠ No Beam — {no_beam_label} reading ≈ 0")
        self.no_beam_banner.setStyleSheet(
            "background-color: #8b0000; color: white; font-weight: bold; "
            "font-size: 10pt; padding: 8px 12px; border-radius: 4px;"
        )
        self.no_beam_banner.setVisible(False)
        layout.addWidget(self.no_beam_banner)

        # Slack Alerts configuration now lives in its own tab --
        # see _build_slack_alerts_tab() -- rather than in a box here, so
        # it's reachable/visible regardless of whether Overall Summary
        # happens to be the currently-open tab. The No Beam banner above
        # stays here since it's specifically about this tab's live CESR
        # readout; Slack Alerts is its own self-contained feature that
        # just happens to watch the same no-beam state.

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        self.summary_plot_panel = PlotPanel()
        splitter.addWidget(self.summary_plot_panel)

        self.summary_imv = pg.ImageView()
        self.summary_imv.ui.roiBtn.hide()
        self.summary_imv.ui.menuBtn.hide()
        self.summary_imv.view.invertY(True)
        splitter.addWidget(self.summary_imv)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.live_image_tab.register_mirror(self.summary_imv)

        self.summary_tab_widget = w
        self._add_tab("summary", w)

    def _make_signal_card(self, canonical: str) -> QtWidgets.QFrame:
        """One Summary-tab readout card: the channel's label and a big
        value, registered in self.signal_labels for _beam_signals_tick()."""
        box = QtWidgets.QFrame()
        box.setFrameShape(QtWidgets.QFrame.StyledPanel)
        box_layout = QtWidgets.QVBoxLayout(box)
        box_layout.setContentsMargins(10, 6, 10, 6)
        name_lbl = QtWidgets.QLabel(csig.CHANNELS[canonical]["label"])
        name_lbl.setProperty("secondaryText", True)
        value_lbl = QtWidgets.QLabel("—")
        value_lbl.setStyleSheet("font-size: 12pt; font-weight: bold;")
        box_layout.addWidget(name_lbl)
        box_layout.addWidget(value_lbl)
        self.signal_labels.setdefault(canonical, []).append(value_lbl)
        return box

    def _build_slack_alerts_tab(self):
        """Its own tab (moved out of Overall Summary per the user's
        request) for configuring and testing Slack Alerts -- posting a
        message via a user-supplied Slack Bot Token (chat:write /
        chat:write.public scopes) whenever the No Beam banner over on
        Overall Summary turns on (beam lost) or off again (beam restored).
        Modeled on the Email dialog's own "remember settings, never
        hardcode the secret" approach -- nothing here is ever written into
        this source file.

        Being a separate tab (rather than living inside Overall Summary)
        doesn't stop alerts from firing while some other tab is open --
        the underlying 1s _beam_signals_timer that drives the no-beam
        detection runs continuously regardless of which tab is visible
        (see its setup in __init__ and _on_tab_changed's docstring); this
        tab only holds the configuration UI and the last-send status
        line, not the polling logic itself."""
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)

        intro = QtWidgets.QLabel(
            f"Posts a Slack message whenever the {self._tab_label('summary')} tab's "
            "\"No Beam\" banner turns on (beam lost) or off again (beam "
            "restored) -- one alert per transition, not one per second."
        )
        intro.setWordWrap(True)
        intro.setProperty("secondaryText", True)
        layout.addWidget(intro)

        slack_box = QtWidgets.QGroupBox("Slack Alerts")
        slack_form = QtWidgets.QFormLayout(slack_box)

        self.slack_token_edit = QtWidgets.QLineEdit()
        self.slack_token_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.slack_token_edit.setPlaceholderText("xoxb-…")
        self.slack_token_edit.setToolTip(
            "Bot User OAuth Token from your Slack App's \"OAuth & "
            "Permissions\" page (needs the chat:write and "
            "chat:write.public bot scopes, and the app must be installed "
            "to your workspace). Never saved into this file -- only into "
            "SLACK_SETTINGS_PATH on this computer, and only if \"Remember "
            "token\" below is checked."
        )
        slack_form.addRow("Bot token:", self.slack_token_edit)

        self.slack_channel_edit = QtWidgets.QLineEdit(SLACK_DEFAULT_CHANNEL)
        self.slack_channel_edit.setToolTip(
            "Channel name (e.g. #beamline-status) or channel ID (e.g. "
            "C0C4L3UQC2C). The bot must be a member of the channel -- "
            "invite it with /invite @YourBotName in Slack -- unless the "
            "app also has the chat:write.public scope."
        )
        slack_form.addRow("Channel:", self.slack_channel_edit)

        self.slack_enable_chk = QtWidgets.QCheckBox(
            "Post to Slack automatically when beam is lost / restored"
        )
        slack_form.addRow("", self.slack_enable_chk)

        self.slack_remember_chk = QtWidgets.QCheckBox(
            "Remember token and channel on this computer"
        )
        slack_form.addRow("", self.slack_remember_chk)

        slack_btn_row = QtWidgets.QHBoxLayout()
        self.slack_test_btn = QtWidgets.QPushButton("Send Test Message")
        self.slack_test_btn.clicked.connect(self._on_slack_test_clicked)
        slack_btn_row.addWidget(self.slack_test_btn)
        slack_btn_row.addStretch(1)
        slack_form.addRow("", slack_btn_row)

        self.slack_status_label = QtWidgets.QLabel("")
        self.slack_status_label.setProperty("secondaryText", True)
        self.slack_status_label.setWordWrap(True)
        slack_form.addRow("", self.slack_status_label)

        layout.addWidget(slack_box)
        layout.addStretch(1)
        self._load_remembered_slack_settings()

        self.slack_alerts_tab_widget = w
        self._add_tab("slack_alerts", w)

    def _on_tab_changed(self, _index: int):
        """Start/stop the Summary tab's 0.1s plot-refresh timer as it
        becomes the visible tab / stops being the visible tab, so the SPEC
        file isn't being silently re-parsed in the background 10x/second
        while the user is looking at some other tab.

        The 1s beam-signals timer (_beam_signals_timer) is deliberately
        NOT started/stopped here -- it runs continuously for the whole
        life of the app (started once in __init__, right after
        _build_central()) regardless of which tab is visible, so Slack
        Alerts (its own separate tab; see _build_slack_alerts_tab()) keeps
        detecting no-beam transitions and posting to Slack in the
        background even while the user is looking at, say, the SPEC Plot
        tab rather than either Overall Summary or Slack Alerts."""
        if self.tabs.currentWidget() is getattr(self, "summary_tab_widget", None):
            self._render_summary_plot()
            self._summary_timer.start()
        else:
            self._summary_timer.stop()

    def _build_statusbar(self):
        self.status_label = QtWidgets.QLabel("")
        self.statusBar().addWidget(self.status_label, 1)

    # ------------------------------------------------------------------
    # File browser
    # ------------------------------------------------------------------
    def set_root_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Set Root Folder", self.current_browse_path)
        if path:
            self.root_path = path
            self.current_browse_path = path
            self.root_edit.setText(path)
            self._refresh_file_tree()

    def _on_root_edit_go(self):
        path = self.root_edit.text().strip()
        if path and os.path.isdir(path):
            self.root_path = path
            self.current_browse_path = path
            self._refresh_file_tree()
        else:
            QtWidgets.QMessageBox.warning(self, "Invalid Path", f"Not a valid directory:\n{path}")

    def _refresh_file_tree(self):
        self.file_tree.clear()
        self._tree_items = {}
        try:
            items = sc.list_directory(self.current_browse_path)
        except Exception as exc:
            self.status_label.setText(f"Error listing directory: {exc}")
            return

        # ".." entry to go up
        parent = os.path.dirname(self.current_browse_path.rstrip(os.sep))
        if parent and parent != self.current_browse_path:
            up_item = QtWidgets.QTreeWidgetItem(["📁 ..", "dir", ""])
            self.file_tree.addTopLevelItem(up_item)
            self._tree_items[id(up_item)] = ("dir", parent)

        for item in items:
            kind = item.get("type", "other")
            if kind == "dir":
                icon, kind_label = "📁", "dir"
            elif kind == "spec_file":
                icon, kind_label = "📄", "spec_file"
            else:
                icon, kind_label = "  ", "other"
            size_str = ""
            if item.get("size") is not None and kind != "dir":
                size_str = self._format_size(item["size"])
            tree_item = QtWidgets.QTreeWidgetItem([f"{icon} {item['name']}", kind_label, size_str])
            self.file_tree.addTopLevelItem(tree_item)
            self._tree_items[id(tree_item)] = (kind, item["path"])

        self.status_label.setText(f"Browsing: {self.current_browse_path}")

    @staticmethod
    def _format_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _on_tree_double_click(self, tree_item, _column):
        info = self._tree_items.get(id(tree_item))
        if not info:
            return
        kind, path = info
        if kind == "dir":
            self.current_browse_path = path
            self._refresh_file_tree()
        else:
            self.load_file(path)

    # ------------------------------------------------------------------
    # Loading files
    # ------------------------------------------------------------------
    def load_file_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load SPEC File", self.current_browse_path)
        if path:
            self.load_file(path)

    def load_sample_data(self):
        df, columns, metadata, scan_info = sc.parse_spec_data(sc.SAMPLE_SPEC_DATA)
        metadata["filename"] = "sample_data.spec"
        metadata["full_path"] = "(built-in sample data)"
        self._apply_loaded_data(df, columns, metadata, scan_info, "sample_data.spec (built-in sample)")
        self._loaded_file_path = None
        self._watch_timer.stop()
        self._autorefresh_timer.stop()
        if self.autorefresh_chk.isChecked():
            self.autorefresh_chk.blockSignals(True)
            self.autorefresh_chk.setChecked(False)
            self.autorefresh_chk.blockSignals(False)

    def reload_file(self):
        if not self._loaded_file_path:
            QtWidgets.QMessageBox.information(self, "Reload", "No file currently loaded.")
            return
        self.load_file(self._loaded_file_path)

    def load_file(self, path: str):
        try:
            df, columns, metadata, scan_info = sc.load_spec_file(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Loading File", str(exc))
            return
        self._apply_loaded_data(df, columns, metadata, scan_info, path)
        self._loaded_file_path = path
        self._start_file_watch(path)
        # Reset the Summary tab's own change-tracking mtime too, so
        # switching to a different file doesn't get compared against the
        # previous file's mtime on the next Summary refresh tick.
        self._summary_watch_mtime = None
        # Point the Live Image (Pilatus) tab -- and, via its mirror, the
        # Summary tab -- at this SPEC file's own image folder automatically.
        self._auto_link_live_image_folder(path)

    def _auto_link_live_image_folder(self, spec_path: str):
        """Automatically point the Live Image tab's watched folder at the
        folder of detector frames that goes with the SPEC file just loaded,
        so there's no need to Browse for it by hand -- the images always
        live alongside the SPEC file for a given experiment. The config's
        data_layout.raw_data_subdirs (e.g. "raw6M" for QM2's Pilatus) are
        tried in order -- the same list sc.find_scan_data /
        sc.spec_subfolders search -- and the SPEC file's own directory is
        the last-resort fallback so something is always watched even if
        none of those exist. Runs on every file
        load (not just the first), so switching to a different
        experiment's SPEC file re-points the watch at that experiment's
        own images instead of leaving it on the previous one. Also force-
        enables "auto-search subfolders", since the actual scan currently
        being written is nearly always one or more levels beneath
        whichever of these folders is found (e.g. raw6M/<sample>/<scan>/).
        A no-op if fabio isn't installed -- the Live Image tab has no
        folder/recurse controls at all in that case (see its fabio-is-None
        guard in _build_ui)."""
        tab = getattr(self, "live_image_tab", None)
        if tab is None or fabio is None or not hasattr(tab, "path_edit"):
            return
        spec_parent = os.path.dirname(os.path.abspath(spec_path))
        folder = None
        for name in sc.RAW_DATA_SUBDIRS:
            candidate = os.path.join(spec_parent, name)
            if os.path.isdir(candidate):
                folder = candidate
                break
        if folder is None:
            folder = spec_parent
        tab.path_edit.setText(folder)
        if not tab.recurse_cb.isChecked():
            tab.recurse_cb.setChecked(True)
        tab.loader.configure(recurse=True)  # belt-and-braces: force-applied
                                             # even if the checkbox above was
                                             # already checked (no toggled
                                             # signal fires in that case)
        tab.apply_folder()

    def _apply_loaded_data(self, df, columns, metadata, scan_info, label):
        self.df = df
        self.columns = columns
        self.metadata = metadata
        self.scan_info = scan_info
        self.loaded_file_label.setText(f"Loaded: {label}  ({len(scan_info)} scans)")
        self.home_status_label.setText(f"Loaded: {label}  ({len(scan_info)} scans)")
        self.status_label.setText(f"Loaded {label}")
        self._refresh_scan_info_table()
        self._refresh_motor_positions_table()
        self._refresh_plot_controls()
        self._refresh_export_controls()

    def _start_file_watch(self, path: str):
        self._watch_timer.stop()
        try:
            self._watch_mtime = os.stat(path).st_mtime
        except OSError:
            self._watch_mtime = None
            return
        # If Auto-Refresh is already on, let its own timer be the only
        # thing polling this file. Previously both timers polled the same
        # _watch_mtime marker concurrently, and since the passive watcher
        # here runs on a fixed 3s interval (typically shorter than the
        # Auto-Refresh interval), it would usually detect a change first
        # and mark that mtime as "seen" — so by the time Auto-Refresh's own
        # tick ran, it saw no change and silently skipped its reload/replot.
        # That race is exactly why Auto-Refresh could appear to do nothing,
        # requiring a manual reload/reset to see new data.
        if not (self.autorefresh_chk.isChecked() and self._autorefresh_timer.isActive()):
            self._watch_timer.start()

    def _check_file_changed(self):
        if not self._loaded_file_path:
            return
        # Defense-in-depth: even though _on_autorefresh_toggled() stops this
        # timer while Auto-Refresh is on, guard here too so that a direct or
        # queued call can never "consume" self._watch_mtime out from under
        # _auto_refresh_tick(). Auto-Refresh owns change detection while active.
        if self.autorefresh_chk.isChecked() and self._autorefresh_timer.isActive():
            return
        try:
            mtime = os.stat(self._loaded_file_path).st_mtime
        except OSError:
            return
        if self._watch_mtime is not None and mtime > self._watch_mtime:
            self.status_label.setText(
                f"File changed on disk — use File > Reload Current File to refresh "
                f"({os.path.basename(self._loaded_file_path)})"
            )
            self._watch_mtime = mtime

    # ------------------------------------------------------------------
    # Auto-Refresh (matches the web dashboard's Auto-Refresh feature:
    # interval polling, automatic reload, and automatic re-plot of the
    # latest scan — not just a passive "file changed" notice)
    # ------------------------------------------------------------------
    def _on_autorefresh_toggled(self, checked: bool):
        if checked:
            if not self._loaded_file_path:
                QtWidgets.QMessageBox.information(
                    self, "Auto-Refresh",
                    "Load a SPEC file from disk first (Auto-Refresh doesn't "
                    "apply to the built-in sample data).",
                )
                self.autorefresh_chk.blockSignals(True)
                self.autorefresh_chk.setChecked(False)
                self.autorefresh_chk.blockSignals(False)
                return
            # Stop the passive "file changed" watcher so it can't race with
            # Auto-Refresh's own polling (see _start_file_watch for why that
            # race made Auto-Refresh silently skip its reload/replot).
            # Auto-Refresh's tick fully takes over change detection from here.
            self._watch_timer.stop()
            self._set_autorefresh_interval()
            self._autorefresh_timer.start()
            # Turning on Auto-Refresh means the whole point is to watch the
            # latest scan live, so automatically turn on the peak-fit overlay
            # too (if it wasn't already) — that way Peak/FWHM values start
            # tracking the newest scan immediately, without an extra manual
            # step. do_plot() already keeps the fit's target scan pinned to
            # whatever scan is newest on every reload.
            if not self.plot_fit_chk.isChecked():
                self.plot_fit_chk.setChecked(True)
            # Also auto-enable the previous-vs-current ghost overlay, so the
            # user can immediately see how the newest scan compares to the
            # one before it without an extra manual step.
            if not self.compare_prev_chk.isChecked():
                self.compare_prev_chk.setChecked(True)
            self.status_label.setText(
                "Auto-refresh enabled — peak fit overlay and previous-vs-current "
                "comparison turned on automatically."
            )
        else:
            self._autorefresh_timer.stop()
            # Hand polling back to the passive watcher now that Auto-Refresh
            # is off, so a "file changed on disk" notice still appears.
            if self._loaded_file_path:
                self._watch_timer.start()
            self.status_label.setText("Auto-refresh disabled.")

    def _on_autorefresh_interval_changed(self, _text: str):
        if self._autorefresh_timer.isActive():
            self._set_autorefresh_interval()

    def _set_autorefresh_interval(self):
        text = self.autorefresh_interval_combo.currentText()
        try:
            seconds = float(text.rstrip("s"))
        except ValueError:
            seconds = 10.0
        # Fractional intervals (e.g. "0.1s") need float parsing, not int();
        # the 100ms floor matches the fastest option in the combo above.
        ms = max(100, int(round(seconds * 1000)))
        self._autorefresh_timer.setInterval(ms)

    def _auto_refresh_tick(self):
        if not self._loaded_file_path:
            return
        try:
            mtime = os.stat(self._loaded_file_path).st_mtime
        except OSError:
            return
        if self._watch_mtime is not None and mtime <= self._watch_mtime:
            return
        try:
            df, columns, metadata, scan_info = sc.load_spec_file(self._loaded_file_path)
        except Exception as exc:
            self.status_label.setText(f"Auto-refresh: failed to reload file ({exc})")
            return
        label = os.path.basename(self._loaded_file_path)
        self._apply_loaded_data(df, columns, metadata, scan_info, label)
        self._watch_mtime = mtime
        self._replot_latest_scan()
        self.status_label.setText(f"Auto-refresh: reloaded {label} and re-plotted the latest scan.")

    def _replot_latest_scan(self):
        """Jump to the SPEC Plot tab and render only the most recent scan,
        mirroring the web dashboard's auto-refresh behavior."""
        scan_numbers = self._scan_numbers_for(self.df)
        if not scan_numbers:
            return
        self._goto_tab("plot")
        # Snapshot whatever curve(s) are still on screen from *before* this
        # refresh — self.df has already been swapped to the new data by
        # _apply_loaded_data() above, but do_plot() hasn't redrawn the chart
        # yet, so the plot widget still shows the pre-refresh curve here.
        if self.compare_prev_chk.isChecked():
            self._auto_refresh_prev_curves = self._capture_prev_curves()
        else:
            self._auto_refresh_prev_curves = []
        self._is_auto_refresh_tick = True
        try:
            self.do_plot()
        finally:
            self._is_auto_refresh_tick = False

    def _capture_prev_curves(self) -> List[Dict]:
        """Snapshot the curve(s) currently drawn on the Plot tab's chart, to
        be re-drawn as a faint dashed "ghost" overlay after the next
        Auto-Refresh redraw. Skips the Fit overlay, the Reference-file
        overlay, and any earlier ghost overlay itself, so only the plain
        scan-data curve(s) get carried forward as "previous"."""
        prev: List[Dict] = []
        try:
            data_items = self.plot_panel.plot_widget.plotItem.listDataItems()
        except Exception:
            return prev
        for item in data_items:
            try:
                name = item.name() or ""
            except Exception:
                name = ""
            if name.startswith("Fit") or name.startswith("Reference:") or name.startswith("Previous:"):
                continue
            try:
                xdata, ydata = item.getData()
            except Exception:
                continue
            if xdata is None or ydata is None or len(xdata) == 0:
                continue
            prev.append({
                "x": np.array(xdata, dtype=float),
                "y": np.array(ydata, dtype=float),
                "name": f"Previous: {name}" if name else "Previous",
                "color": GHOST_COLOR,
                "style": QtCore.Qt.DashLine,
                "width": 2,
            })
        return prev

    # ------------------------------------------------------------------
    # Home tab
    # ------------------------------------------------------------------
    def _build_home_tab(self):
        w = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(w)
        outer.setContentsMargins(24, 24, 24, 24)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setSpacing(14)

        title = QtWidgets.QLabel(HOME_PAGE_TITLE)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(CONFIG["app"].get("home_subtitle") or "")
        subtitle.setWordWrap(True)
        subtitle.setProperty("secondaryText", True)
        subtitle.setStyleSheet("font-size: 13px;")
        layout.addWidget(subtitle)

        layout.addSpacing(6)
        actions_label = QtWidgets.QLabel("Get started")
        actions_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(actions_label)

        actions_row = QtWidgets.QHBoxLayout()
        btn_browse = QtWidgets.QPushButton("Browse Folder…")
        btn_browse.clicked.connect(self.set_root_folder)
        actions_row.addWidget(btn_browse)
        btn_load = QtWidgets.QPushButton("Load SPEC File…")
        btn_load.clicked.connect(self.load_file_dialog)
        actions_row.addWidget(btn_load)
        btn_sample = QtWidgets.QPushButton("Load Sample Data")
        btn_sample.clicked.connect(self.load_sample_data)
        actions_row.addWidget(btn_sample)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        self.home_status_label = QtWidgets.QLabel(
            "No file loaded yet — use the buttons above, or double-click a "
            "file in the browser on the left."
        )
        self.home_status_label.setWordWrap(True)
        self.home_status_label.setProperty("secondaryText", True)
        self.home_status_label.setStyleSheet("font-style: italic;")
        layout.addWidget(self.home_status_label)

        layout.addSpacing(10)
        features_label = QtWidgets.QLabel("What you can do here")
        features_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(features_label)

        features_grid = QtWidgets.QGridLayout()
        features_grid.setHorizontalSpacing(20)
        features_grid.setVerticalSpacing(8)
        feature_items = [
            (entry["label"], entry["description"])
            for entry in CONFIG["tabs"]
            if entry["key"] != "home" and entry["show"]
        ]
        for i, (name, desc) in enumerate(feature_items):
            name_lbl = QtWidgets.QLabel(f"● {name}")
            name_lbl.setStyleSheet("font-weight: bold;")
            desc_lbl = QtWidgets.QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setProperty("secondaryText", True)
            features_grid.addWidget(name_lbl, i, 0, QtCore.Qt.AlignTop)
            features_grid.addWidget(desc_lbl, i, 1)
        features_grid.setColumnStretch(1, 1)
        layout.addLayout(features_grid)

        layout.addSpacing(10)
        shortcuts_label = QtWidgets.QLabel(
            "Keyboard shortcuts:   Ctrl+O — set root / browse    •    "
            f"Ctrl+P — jump to {self._tab_label('plot')}    •    "
            f"Ctrl+T — jump to {self._tab_label('scan_info')}    •    "
            f"Ctrl+E — jump to {self._tab_label('export')}"
        )
        shortcuts_label.setWordWrap(True)
        shortcuts_label.setProperty("secondaryText", True)
        shortcuts_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(shortcuts_label)

        layout.addStretch(1)
        self._add_tab("home", w)

    # ------------------------------------------------------------------
    # Scan Info tab
    # ------------------------------------------------------------------
    def _build_scan_info_tab(self):
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        self.scan_info_table = QtWidgets.QTableWidget(0, 7)
        self.scan_info_table.setHorizontalHeaderLabels(
            ["Scan #", "Command", "Timestamp", "Temperature", "Count Time", "Points", "Comments"]
        )
        self.scan_info_table.horizontalHeader().setStretchLastSection(True)
        self.scan_info_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.scan_info_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        layout.addWidget(self.scan_info_table)
        self._add_tab("scan_info", w)

    def _refresh_scan_info_table(self):
        rows = sc.build_scan_table(self.scan_info, self.df)
        self.scan_info_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [
                row.get("scan_number", ""),
                row.get("command", ""),
                row.get("timestamp", ""),
                row.get("temperature", ""),
                row.get("count_time", ""),
                row.get("data_points", ""),
                row.get("comments", ""),
            ]
            for c, v in enumerate(vals):
                self.scan_info_table.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))
        self.scan_info_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Motor Positions tab
    # ------------------------------------------------------------------
    def _build_motor_positions_tab(self):
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        self.motor_table = QtWidgets.QTableWidget(0, 0)
        self.motor_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.motor_table)
        self._add_tab("motor_positions", w)

    def _refresh_motor_positions_table(self):
        motors_meta, scans_data = sc.build_motor_positions(self.metadata, self.scan_info)
        col_labels = [m["mnemonic"] or m["name"] for m in motors_meta]
        headers = ["Scan #", "Command"] + col_labels
        self.motor_table.setColumnCount(len(headers))
        self.motor_table.setHorizontalHeaderLabels(headers)
        self.motor_table.setRowCount(len(scans_data))
        for r, row in enumerate(scans_data):
            scan_num = row.get("scan_number", "")
            command = row.get("command", "")
            self.motor_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(scan_num)))
            self.motor_table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(command)))
            positions = row.get("positions", []) or []
            for i in range(len(motors_meta)):
                val = positions[i] if i < len(positions) and positions[i] is not None else ""
                try:
                    val_str = f"{float(val):.4g}"
                except (TypeError, ValueError):
                    val_str = str(val)
                self.motor_table.setItem(r, i + 2, QtWidgets.QTableWidgetItem(val_str))
        self.motor_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Plot tab
    # ------------------------------------------------------------------
    def _build_plot_tab(self):
        w = QtWidgets.QWidget()
        main_layout = QtWidgets.QHBoxLayout(w)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Sidebar and chart are placed in a splitter (rather than a plain
        # fixed-proportion QHBoxLayout) so the user can drag the divider to
        # give the chart more room, in addition to the "Enlarge Plot" button
        # below, which hides the sidebar outright.
        self.plot_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main_layout.addWidget(self.plot_splitter)

        # The sidebar is wrapped in a scroll area since it now holds quite a
        # few sections (scan list, Peak/FWHM readouts, reference-file
        # overlay); a fixed-height sidebar would otherwise squeeze the scan
        # list back down to the cramped size that prompted this redesign.
        self.plot_controls_scroll = QtWidgets.QScrollArea()
        controls_scroll = self.plot_controls_scroll
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setMinimumWidth(260)
        controls_scroll.setMaximumWidth(480)
        controls_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        controls = QtWidgets.QWidget()
        cl = QtWidgets.QFormLayout(controls)
        controls_scroll.setWidget(controls)

        self.plot_x_combo = QtWidgets.QComboBox()
        self.plot_x_combo.currentIndexChanged.connect(self._auto_replot)
        cl.addRow("X:", self.plot_x_combo)

        self.plot_y_list = QtWidgets.QListWidget()
        self.plot_y_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.plot_y_list.setMaximumHeight(100)
        self.plot_y_list.itemSelectionChanged.connect(self._auto_replot)
        cl.addRow("Y:", self.plot_y_list)

        # Scan(s) list: wide enough that most commands fit on their own
        # single line (see _populate_scan_list, which also sets word-wrap
        # off and elide-with-"…" so a long command never spills onto a
        # second/third line and eats vertical space) — the full text is
        # always available as a tooltip on hover. Given an Expanding size
        # policy (rather than a small fixed minimum) so it grows to use
        # whatever extra vertical room the sidebar has, instead of staying
        # cramped down at a tiny fixed height.
        self.plot_scans_list = QtWidgets.QListWidget()
        self.plot_scans_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.plot_scans_list.setMinimumHeight(220)
        self.plot_scans_list.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.plot_scans_list.setToolTip(
            "Each row shows \"scan number: command\" so it's easy to see "
            "which scan is which. Ctrl-click (Cmd-click on Mac) or "
            "Shift-click to select multiple scans — e.g. 1, 2 and 3 — to "
            "overlay them on the same chart."
        )
        self.plot_scans_list.itemSelectionChanged.connect(self._on_plot_scan_selection_changed)
        self.plot_scans_list.itemSelectionChanged.connect(self._auto_replot)
        cl.addRow("Scans:", self.plot_scans_list)

        self.plot_type_combo = QtWidgets.QComboBox()
        self.plot_type_combo.addItems(["line+scatter", "line", "scatter", "bar"])
        self.plot_type_combo.setCurrentText("line+scatter")
        self.plot_type_combo.currentIndexChanged.connect(self._auto_replot)
        cl.addRow("Plot type:", self.plot_type_combo)

        self.plot_normalize_chk = QtWidgets.QCheckBox("Normalize (0-1)")
        self.plot_normalize_chk.toggled.connect(self._auto_replot)
        cl.addRow(self.plot_normalize_chk)

        self.plot_logx_chk = QtWidgets.QCheckBox("Log X")
        self.plot_logx_chk.toggled.connect(lambda v: self.plot_panel.set_log_x(v))
        cl.addRow(self.plot_logx_chk)

        self.plot_logy_chk = QtWidgets.QCheckBox("Log Y")
        self.plot_logy_chk.toggled.connect(lambda v: self.plot_panel.set_log_y(v))
        cl.addRow(self.plot_logy_chk)

        self.plot_grid_chk = QtWidgets.QCheckBox("Include grid lines")
        self.plot_grid_chk.setChecked(True)
        self.plot_grid_chk.setToolTip(
            "Shows or hides grid lines on this chart immediately, and also "
            "controls whether they're included when you later save or copy "
            "an image of this plot."
        )
        self.plot_grid_chk.toggled.connect(self._on_plot_grid_toggled)
        cl.addRow(self.plot_grid_chk)

        # ── Curve-fit readout. The "Actual Data" (raw, measured, no-fit)
        # readout used to live here too, but it's now shown as its own bar
        # directly above the chart itself (see right_col below) instead of
        # tucked away in the sidebar — easier to spot at a glance, and with
        # a larger font to match. This box just keeps the optional
        # curve-fit result.
        peak_group = QtWidgets.QGroupBox("Curve fit result")
        peak_layout = QtWidgets.QVBoxLayout(peak_group)
        fit_caption = QtWidgets.QLabel("Curve fit (optional, below):")
        fit_caption.setProperty("secondaryText", True)
        peak_layout.addWidget(fit_caption)
        self.plot_fit_results_label = QtWidgets.QLabel("")
        self.plot_fit_results_label.setWordWrap(True)
        # NOTE: deliberately no hardcoded "color: ..." here (see the longer
        # explanation that used to sit above the now-relocated Actual Data
        # label, still applicable to this label: baking in a literal color
        # value at construction time freezes it forever at whatever theme
        # was active when the label was built, so it goes stale and
        # unreadable after a theme switch even though the text itself keeps
        # updating correctly). Only font-size is set here; color inherits
        # from the app-wide stylesheet, which IS rebuilt with the right
        # color on every theme switch.
        self.plot_fit_results_label.setStyleSheet("font-size: 11px;")
        peak_layout.addWidget(self.plot_fit_results_label)
        cl.addRow(peak_group)

        notes_label = QtWidgets.QLabel("Notes (added under the image when saved/copied):")
        notes_label.setProperty("secondaryText", True)
        cl.addRow(notes_label)
        self.plot_notes_edit = QtWidgets.QPlainTextEdit()
        self.plot_notes_edit.setPlaceholderText(
            "Optional notes to include as a caption under the saved/copied "
            "plot image…"
        )
        self.plot_notes_edit.setMaximumHeight(70)
        self.plot_notes_edit.setToolTip(
            "Anything typed here is drawn as a text caption underneath the "
            "chart when you use \"Save Plot Image\" or \"Copy Plot Image to "
            "Clipboard\" below. It does not appear on the live, on-screen "
            "chart itself — only in the saved/copied image."
        )
        cl.addRow(self.plot_notes_edit)

        # ── Reference file (optional) — folded in from the old standalone
        # "Compare Plot" tab, which only ever offered one extra thing beyond
        # this tab's own multi-scan overlay: plotting a scan from a second,
        # independently-loaded SPEC file. That capability now lives here as
        # a compact overlay option instead of a whole separate tab.
        ref_group = QtWidgets.QGroupBox("Reference file (optional)")
        ref_layout = QtWidgets.QVBoxLayout(ref_group)
        btn_load_ref = QtWidgets.QPushButton("Load Reference File…")
        btn_load_ref.setToolTip(
            "Load a second SPEC file and overlay one of its scans on this "
            "chart for comparison."
        )
        btn_load_ref.clicked.connect(self.load_reference_file)
        ref_layout.addWidget(btn_load_ref)
        self.reference_file_label = QtWidgets.QLabel("No reference file loaded.")
        self.reference_file_label.setWordWrap(True)
        self.reference_file_label.setProperty("secondaryText", True)
        self.reference_file_label.setStyleSheet("font-size: 11px;")
        ref_layout.addWidget(self.reference_file_label)
        ref_form = QtWidgets.QFormLayout()
        self.reference_scan_combo = QtWidgets.QComboBox()
        self.reference_scan_combo.currentIndexChanged.connect(self._auto_replot)
        ref_form.addRow("Scan:", self.reference_scan_combo)
        self.reference_y_combo = QtWidgets.QComboBox()
        self.reference_y_combo.currentIndexChanged.connect(self._auto_replot)
        ref_form.addRow("Y column:", self.reference_y_combo)
        ref_layout.addLayout(ref_form)
        self.reference_overlay_chk = QtWidgets.QCheckBox("Overlay reference scan")
        self.reference_overlay_chk.toggled.connect(self._auto_replot)
        ref_layout.addWidget(self.reference_overlay_chk)
        cl.addRow(ref_group)

        btn_download = QtWidgets.QPushButton("⬇ Download Plotted Data (CSV)")
        btn_download.setToolTip(
            "Save the data currently shown on this chart to a CSV file. "
            "This is separate from the full export options on the Export tab."
        )
        btn_download.clicked.connect(self.export_plotted)
        cl.addRow(btn_download)

        btn_save_image = QtWidgets.QPushButton("💾 Save Plot Image (PNG)…")
        btn_save_image.setToolTip(
            "Save a high-resolution image of this chart, with or without "
            "grid lines (see the checkbox above)."
        )
        btn_save_image.clicked.connect(self.save_plot_image)
        cl.addRow(btn_save_image)

        btn_copy_image = QtWidgets.QPushButton("📋 Copy Plot Image to Clipboard")
        btn_copy_image.setToolTip(
            "Copy a high-resolution image of this chart to the clipboard, "
            "ready to paste elsewhere — with or without grid lines (see "
            "the checkbox above)."
        )
        btn_copy_image.clicked.connect(self.copy_plot_image)
        cl.addRow(btn_copy_image)

        btn_email = QtWidgets.QPushButton("✉ Email Plot Data…")
        btn_email.setToolTip(
            "Email the plotted data (CSV) and/or the plot image (PNG, "
            "including any Notes) as attachments to an address you enter."
        )
        btn_email.clicked.connect(self.open_email_dialog)
        cl.addRow(btn_email)

        btn_plot = QtWidgets.QPushButton("Plot")
        btn_plot.clicked.connect(self.do_plot)
        cl.addRow(btn_plot)

        self.plot_splitter.addWidget(controls_scroll)

        right_widget = QtWidgets.QWidget()
        right_col = QtWidgets.QVBoxLayout(right_widget)
        right_col.setContentsMargins(0, 0, 0, 0)

        # "Enlarge Plot" toggle: hides the sidebar so the chart fills the
        # whole tab — a quicker, bigger jump in plot size than dragging the
        # splitter handle, for when the user just wants to see the chart as
        # large as possible for a moment.
        enlarge_bar = QtWidgets.QHBoxLayout()
        enlarge_bar.addStretch(1)
        self.plot_maximize_btn = QtWidgets.QToolButton()
        self.plot_maximize_btn.setText("⛶ Enlarge Plot")
        self.plot_maximize_btn.setCheckable(True)
        self.plot_maximize_btn.setAutoRaise(True)
        self.plot_maximize_btn.setToolTip(
            "Hide the sidebar so the chart fills the whole tab. Click again "
            "(or use the splitter handle) to bring the sidebar back."
        )
        self.plot_maximize_btn.toggled.connect(self._on_plot_maximize_toggled)
        enlarge_bar.addWidget(self.plot_maximize_btn)
        right_col.addLayout(enlarge_bar)

        # "Actual Data" (raw, measured, no-fit) Peak/FWHM readout — placed
        # directly above the chart itself, in a slightly larger/bolder font,
        # so it's immediately visible right where you're looking rather than
        # tucked away in the sidebar below other controls.
        actual_data_bar = QtWidgets.QWidget()
        actual_data_layout = QtWidgets.QVBoxLayout(actual_data_bar)
        actual_data_layout.setContentsMargins(4, 0, 4, 4)
        actual_data_layout.setSpacing(1)
        actual_caption = QtWidgets.QLabel("Actual data (no fit):")
        actual_caption.setProperty("secondaryText", True)
        actual_data_layout.addWidget(actual_caption)
        self.plot_actual_results_label = QtWidgets.QLabel("")
        self.plot_actual_results_label.setWordWrap(True)
        # NOTE: deliberately no hardcoded "color: ..." here — see the note
        # on plot_fit_results_label's stylesheet for why: a baked-in color
        # goes stale after a theme switch, so only font-size/weight are set
        # and color inherits from the app-wide stylesheet, which IS rebuilt
        # correctly on every theme switch.
        self.plot_actual_results_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        actual_data_layout.addWidget(self.plot_actual_results_label)
        right_col.addWidget(actual_data_bar)

        self.plot_panel = PlotPanel()
        right_col.addWidget(self.plot_panel, 3)

        # ── Peak fit, as an option on this tab (not a separate tab) ────────
        # Placed below the main chart (full tab width) rather than squeezed
        # into the narrow left sidebar, so the controls and stats table have
        # room to breathe. The quick text summary now lives in the sidebar's
        # "Peak / FWHM" group above; this box keeps the fuller controls plus
        # the detailed stats table.
        fit_group = QtWidgets.QGroupBox("Peak fit (optional)")
        fit_group.setMaximumHeight(200)
        fit_outer = QtWidgets.QHBoxLayout(fit_group)

        fit_left = QtWidgets.QWidget()
        fit_form = QtWidgets.QFormLayout(fit_left)

        self.plot_fit_chk = QtWidgets.QCheckBox("Overlay peak fit on the plot")
        self.plot_fit_chk.toggled.connect(self._on_plot_fit_toggled)
        fit_form.addRow(self.plot_fit_chk)

        self.plot_fit_scan_combo = QtWidgets.QComboBox()
        self.plot_fit_scan_combo.setToolTip(
            "Which scan to analyze for Peak/FWHM — used for both the Actual "
            "Data readout above and this optional curve fit."
        )
        self.plot_fit_scan_combo.currentIndexChanged.connect(self._auto_replot)
        fit_form.addRow("Scan:", self.plot_fit_scan_combo)

        self.plot_fit_y_combo = QtWidgets.QComboBox()
        self.plot_fit_y_combo.setToolTip(
            "Which Y column to analyze for Peak/FWHM — used for both the "
            "Actual Data readout above and this optional curve fit."
        )
        self.plot_fit_y_combo.currentIndexChanged.connect(self._auto_replot)
        fit_form.addRow("Y column:", self.plot_fit_y_combo)

        self.plot_fit_type_group = QtWidgets.QButtonGroup(self)
        rb_gauss = QtWidgets.QRadioButton("Gaussian")
        rb_gauss.setChecked(True)
        rb_lorentz = QtWidgets.QRadioButton("Lorentzian")
        self.plot_fit_type_group.addButton(rb_gauss, 0)
        self.plot_fit_type_group.addButton(rb_lorentz, 1)
        self.plot_fit_type_group.buttonClicked.connect(self._auto_replot)
        fit_type_box = QtWidgets.QHBoxLayout()
        fit_type_box.addWidget(rb_gauss)
        fit_type_box.addWidget(rb_lorentz)
        fit_form.addRow("Model:", fit_type_box)

        fit_outer.addWidget(fit_left, 1)

        self.plot_fit_stats_table = QtWidgets.QTableWidget(0, 2)
        self.plot_fit_stats_table.setHorizontalHeaderLabels(["Stat", "Value"])
        self.plot_fit_stats_table.horizontalHeader().setStretchLastSection(True)
        self.plot_fit_stats_table.verticalHeader().setVisible(False)
        self.plot_fit_stats_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.plot_fit_stats_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        fit_outer.addWidget(self.plot_fit_stats_table, 1)

        # Residuals mini-plot is placed ABOVE the "Peak fit (optional)"
        # controls box (rather than below it) so it reads top-to-bottom as
        # "main chart -> how well the fit matches (residuals) -> the fit
        # controls/stats that produced it" -- keeping both plots stacked
        # together before the form controls underneath.
        self.plot_fit_residuals_panel = PlotPanel()
        self.plot_fit_residuals_panel.plot_widget.setMaximumHeight(160)
        self.plot_fit_residuals_panel.setVisible(False)
        right_col.addWidget(self.plot_fit_residuals_panel, 1)

        right_col.addWidget(fit_group)

        self.plot_splitter.addWidget(right_widget)
        self.plot_splitter.setStretchFactor(0, 0)
        self.plot_splitter.setStretchFactor(1, 1)
        self.plot_splitter.setSizes([340, 900])

        self._add_tab("plot", w)

    def _refresh_plot_controls(self):
        self._suspend_auto_plot = True
        _populate_combo(self.plot_x_combo, self.columns)
        _populate_list(self.plot_y_list, self.columns)
        scan_numbers = self._scan_numbers_for(self.df)
        _populate_scan_list(self.plot_scans_list, scan_numbers, self._command_for_scan)
        _populate_combo(self.plot_fit_scan_combo, scan_numbers)
        _populate_combo(self.plot_fit_y_combo, self.columns)
        self._clear_fit_results()
        self._clear_actual_peak_results()
        self._apply_plot_defaults(scan_numbers)
        self._suspend_auto_plot = False
        if self.df is not None:
            self.do_plot()

    def _auto_replot(self, *_args):
        """Re-plot automatically whenever a Plot-tab control changes, instead
        of requiring an explicit click on the "Plot" button. Suppressed while
        controls are being repopulated (e.g. on file load) to avoid firing on
        every intermediate, incomplete selection state."""
        if getattr(self, "_suspend_auto_plot", False):
            return
        if self.df is not None:
            self.do_plot()

    def _clear_fit_results(self):
        self.plot_fit_results_label.setText("")
        self.plot_fit_stats_table.setRowCount(0)
        self.plot_fit_residuals_panel.setVisible(False)

    def _clear_actual_peak_results(self):
        self.plot_actual_results_label.setText("")

    def _apply_plot_defaults(self, scan_numbers: List[str]):
        """Pre-select sensible defaults, matching the web dashboard: only the
        last scan is selected, Y defaults to a priority counter column, and X
        is parsed from the last scan's command (its swept motor)."""
        if not scan_numbers or not self.columns:
            return
        last_scan_str = scan_numbers[-1]
        _select_scan_list_items(self.plot_scans_list, [last_scan_str])
        default_y = _default_y_columns(self.columns)
        _select_list_items(self.plot_y_list, default_y)
        command = self._command_for_scan(last_scan_str)
        x_col = _default_x_column(command, self.columns)
        if x_col:
            _select_combo_text(self.plot_x_combo, x_col)
        # Fit target defaults mirror the plot defaults (last scan, same Y).
        _select_combo_text(self.plot_fit_scan_combo, last_scan_str)
        if default_y:
            _select_combo_text(self.plot_fit_y_combo, default_y[0])

    def _command_for_scan(self, scan_str: str) -> str:
        try:
            scan_num = int(scan_str)
        except (TypeError, ValueError):
            return ""
        info = self.scan_info.get(scan_num) if isinstance(self.scan_info, dict) else None
        return info.get("command", "") if info else ""

    def _on_plot_scan_selection_changed(self):
        """When the user changes which scans are selected, auto-update the
        X-axis based on the last selected scan's motor (mirrors the web
        dashboard's onScanSelectionChange)."""
        selected = _scan_list_selected_values(self.plot_scans_list)
        if not selected or not self.columns:
            return
        command = self._command_for_scan(selected[-1])
        x_col = _default_x_column(command, self.columns)
        if x_col:
            _select_combo_text(self.plot_x_combo, x_col)
        _select_combo_text(self.plot_fit_scan_combo, selected[-1])

    def _on_plot_fit_toggled(self, _checked: bool):
        if self.df is not None:
            self.do_plot()
        else:
            self._clear_fit_results()

    def _on_plot_maximize_toggled(self, checked: bool):
        """Hide/show the Plot tab's sidebar so the chart can fill the whole
        tab on demand, instead of only being resizable by a few tens of
        pixels via the splitter handle."""
        self.plot_controls_scroll.setVisible(not checked)
        self.plot_maximize_btn.setText("↙ Show Sidebar" if checked else "⛶ Enlarge Plot")

    def _on_plot_grid_toggled(self, checked: bool):
        """Show/hide the grid lines on the LIVE, on-screen chart immediately.
        Previously this checkbox only affected images saved/copied via
        _export_plot_image() and had no visible effect on the chart itself
        when clicked — see that method for the matching fix so an export no
        longer forces the live grid back on afterward."""
        self._apply_grid_visibility(self.plot_panel, checked)
        if self.plot_fit_residuals_panel.isVisible():
            self._apply_grid_visibility(self.plot_fit_residuals_panel, checked)

    @staticmethod
    def _apply_grid_visibility(panel: "PlotPanel", visible: bool):
        try:
            panel.plot_widget.showGrid(x=visible, y=visible, alpha=0.3)
        except Exception:
            pass

    @staticmethod
    def _scan_numbers_for(df) -> List[str]:
        if df is None or "scan_number" not in df.columns:
            return []
        return [str(s) for s in sorted(df["scan_number"].unique().tolist())]

    def do_plot(self):
        if self.df is None:
            self.plot_panel.show_empty("Load a SPEC file first.")
            return
        x_col = self.plot_x_combo.currentText()
        y_cols = _multiselect_values(self.plot_y_list)
        scans = [int(s) for s in _scan_list_selected_values(self.plot_scans_list)]
        if not x_col or not y_cols:
            self.plot_panel.show_empty("Choose an X column and at least one Y column.")
            return
        plot_type = self.plot_type_combo.currentText()
        normalize = self.plot_normalize_chk.isChecked()

        extra_curves: List[Dict] = []

        if self.plot_fit_chk.isChecked():
            fit_curve = self._compute_plot_fit_curve(x_col)
            if fit_curve is not None:
                fx, fy = fit_curve
                extra_curves.append({
                    "x": fx, "y": fy, "name": "Fit", "color": FIT_COLOR,
                    "style": QtCore.Qt.DashLine, "width": 2,
                })
        else:
            self._clear_fit_results()

        # The actual-(raw)-data Peak/FWHM readout is always computed, no
        # matter whether the optional curve fit above is on — it doesn't
        # depend on a successful fit at all, just on a scan/Y column being
        # selected in the (now shared) Scan/Y column combos. The result is
        # shown in the left-sidebar "Actual Data" panel (see
        # plot_actual_results_label), not drawn on the chart itself.
        self._compute_actual_peak(x_col)

        ref_curve = self._reference_overlay_curve(x_col)
        if ref_curve is not None:
            rx, ry, rname = ref_curve
            extra_curves.append({
                "x": rx, "y": ry, "name": rname, "color": REFERENCE_COLOR,
                "style": QtCore.Qt.DotLine, "width": 2,
            })

        # During an Auto-Refresh tick, if "Compare previous vs current" is
        # enabled, _replot_latest_scan() has already snapshotted the curves
        # that were on screen right before this redraw into
        # self._auto_refresh_prev_curves — draw them alongside the fresh
        # data as faint dashed "ghost" curves.
        if self._is_auto_refresh_tick:
            extra_curves.extend(self._auto_refresh_prev_curves)

        self._render_plot(
            self.plot_panel, self.df, x_col, y_cols, scans, plot_type, normalize,
            extra_curves=extra_curves,
        )
        self.last_plot_info = {"x_column": x_col, "y_columns": y_cols, "scans": scans}

    @staticmethod
    def _peak_height(stats: Dict) -> Optional[float]:
        """The fitted curve's own value at its peak position — amplitude
        plus baseline offset, since spec_core.fit_peak's Gaussian/Lorentzian
        models are both of the form amplitude * shape(x) + offset, and
        shape(peak_position) == 1 for both. Used to place/report the peak's
        actual (x, y) point rather than just its x position."""
        amp = stats.get("amplitude")
        offset = stats.get("offset")
        if amp is None:
            return None
        return amp + (offset or 0.0)

    def _format_peak_fwhm_text(self, stats: Dict, x_col: str, y_col: str) -> str:
        """Build the "Peak @ X is Y" / "FWHM @ X is Y" readout used both in
        the sidebar results label and as the on-plot annotation."""
        peak_x = stats.get("peak_position")
        fwhm = stats.get("fwhm")
        peak_y = self._peak_height(stats)
        lines = []
        if peak_x is not None and peak_y is not None:
            lines.append(f"Peak @ {x_col}={peak_x:.4g} is {y_col}={peak_y:.4g}")
        if peak_x is not None and fwhm is not None:
            lines.append(f"FWHM @ {x_col}={peak_x:.4g} is {fwhm:.4g}")
        return "\n".join(lines)

    def _compute_actual_peak(self, x_col: str) -> None:
        """Compute the Peak/FWHM readout directly from the actual (raw,
        measured) scan data via spec_core.find_actual_peak — independent of
        whether the optional curve fit below is turned on or has succeeded.
        Uses the same Scan/Y column combos as the Peak fit box, since that's
        already the UI for choosing which scan/column to analyze. Updates
        the Actual Data panel's label in the left sidebar."""
        y_col = self.plot_fit_y_combo.currentText()
        scan_text = self.plot_fit_scan_combo.currentText()
        if not x_col or not y_col or not scan_text or self.df is None:
            self.plot_actual_results_label.setText("")
            return
        try:
            scan = int(scan_text)
        except ValueError:
            self.plot_actual_results_label.setText("")
            return
        try:
            result = sc.find_actual_peak(self.df, x_col, y_col, scan)
        except Exception as exc:
            self.plot_actual_results_label.setText(f"Actual-data peak unavailable: {exc}")
            return
        if not result.get("success"):
            self.plot_actual_results_label.setText(
                f"Actual-data peak unavailable: {result.get('error', 'unknown error')}"
            )
            return
        peak_x = result["peak_x"]
        peak_y = result["peak_y"]
        # Reuse the same "Peak @ X is Y" / "FWHM @ X is Y" formatter as the
        # fit readout by building a stats-shaped dict from the actual-data
        # result (amplitude/offset stand in for peak height since
        # _peak_height computes amplitude + offset — offset=0 makes that
        # equal peak_y directly).
        pseudo_stats = {
            "peak_position": peak_x,
            "fwhm": result.get("fwhm"),
            "amplitude": peak_y,
            "offset": 0.0,
        }
        text = self._format_peak_fwhm_text(pseudo_stats, x_col, y_col)
        self.plot_actual_results_label.setText(text)

    def _compute_plot_fit_curve(self, x_col: str):
        """Fit a Gaussian/Lorentzian peak (via spec_core.fit_peak) to the
        chosen fit scan/Y column and return an (x, y) curve to overlay on the
        Plot tab's chart, or None if the fit can't be computed. Also updates
        the one-line status label, the detailed stats table (with parameter
        uncertainties and goodness-of-fit), and the residuals mini-plot."""
        self.plot_fit_stats_table.setRowCount(0)
        self.plot_fit_residuals_panel.setVisible(False)
        y_col = self.plot_fit_y_combo.currentText()
        scan_text = self.plot_fit_scan_combo.currentText()
        if not x_col or not y_col or not scan_text:
            self.plot_fit_results_label.setText("Choose a fit scan and Y column.")
            return None
        try:
            scan = int(scan_text)
        except ValueError:
            self.plot_fit_results_label.setText("Invalid scan number for fit.")
            return None
        fit_type = "gaussian" if self.plot_fit_type_group.checkedId() == 0 else "lorentzian"
        try:
            result = sc.fit_peak(self.df, x_col, y_col, scan, fit_type=fit_type)
        except Exception as exc:
            self.plot_fit_results_label.setText(f"Fit failed: {exc}")
            return None
        if not result.get("success"):
            self.plot_fit_results_label.setText(f"Fit failed: {result.get('error', 'unknown error')}")
            return None
        stats = result.get("stats", {})
        r2 = stats.get("r_squared")
        r2_text = f", R²={r2:.4g}" if isinstance(r2, (int, float)) else ""
        peak_fwhm_text = self._format_peak_fwhm_text(stats, x_col, y_col)
        summary_line = f"{fit_type.capitalize()} fit succeeded — scan {scan}, {y_col}{r2_text}"
        self.plot_fit_results_label.setText(
            f"{summary_line}\n{peak_fwhm_text}" if peak_fwhm_text else summary_line
        )
        self._update_fit_results_table(stats, fit_type, scan, y_col)
        residuals = result.get("residuals")
        if residuals:
            self._render_fit_residuals(residuals, x_col)
        fit_curve = result.get("fit_curve")
        if fit_curve:
            return (fit_curve["x"], fit_curve["y"])
        return None

    def _update_fit_results_table(self, stats: Dict, fit_type: str, scan: int, y_col: str):
        """Populate the Plot tab's fit-results table with a clean, labeled
        breakdown of the fit: model, fitted parameters with their
        uncertainties, and goodness-of-fit (R², reduced chi-square)."""
        table = self.plot_fit_stats_table
        table.setRowCount(0)

        def add_row(label: str, value: str):
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QtWidgets.QTableWidgetItem(label))
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(value))

        def fmt(key: str, err_key: Optional[str] = None, digits: int = 4) -> Optional[str]:
            val = stats.get(key)
            if val is None:
                return None
            try:
                text = f"{val:.{digits}g}"
            except (TypeError, ValueError):
                text = str(val)
            if err_key is not None and stats.get(err_key) is not None:
                try:
                    text += f" ± {stats[err_key]:.{digits}g}"
                except (TypeError, ValueError):
                    pass
            return text

        add_row("Model", stats.get("fit_type", fit_type.capitalize()))
        add_row("Scan / Y column", f"{scan} / {y_col}")
        for label, key, err_key in [
            ("Peak position", "peak_position", "peak_position_err"),
            ("FWHM", "fwhm", "fwhm_err"),
            ("Amplitude", "amplitude", "amplitude_err"),
            ("Sigma", "sigma", "sigma_err"),
            ("Gamma", "gamma", "gamma_err"),
            ("Offset", "offset", "offset_err"),
        ]:
            text = fmt(key, err_key)
            if text is not None:
                add_row(label, text)
        r2_text = fmt("r_squared", digits=5)
        if r2_text is not None:
            add_row("R² (goodness of fit)", r2_text)
        chi_text = fmt("reduced_chi_square", digits=4)
        if chi_text is not None:
            add_row("Reduced χ²", chi_text)
        table.resizeRowsToContents()

    def _render_fit_residuals(self, residuals: Dict, x_col: str):
        """Show a small residuals-vs-x plot (data minus fit) above the
        "Peak fit (optional)" controls box, so it's easy to see where the
        fitted model over- or under-shoots the real data."""
        panel = self.plot_fit_residuals_panel
        panel.clear()
        xs = residuals.get("x") or []
        ys = residuals.get("y") or []
        if not xs:
            panel.setVisible(False)
            return
        panel.plot_widget.plot(
            xs, ys, pen=None, symbol="o", symbolSize=6,
            symbolBrush=RESIDUAL_COLOR, symbolPen=RESIDUAL_COLOR, name="Residuals",
        )
        panel.plot_widget.addLine(
            y=0, pen=pg.mkPen(color=TEXT_SECONDARY, width=1, style=QtCore.Qt.DashLine)
        )
        panel.plot_widget.setLabel("bottom", x_col)
        panel.plot_widget.setLabel("left", "Residual (data − fit)")
        panel.setVisible(True)

    def _render_plot(self, panel: PlotPanel, df, x_col, y_cols, scans, plot_type, normalize, extra_curves=None):
        panel.clear()
        if df is None or "scan_number" not in df.columns:
            panel.show_empty("No data loaded.")
            return
        color_i = 0
        any_data = False
        target_scans = scans if scans else sorted(df["scan_number"].unique().tolist())
        for scan_num in target_scans:
            sub = df[df["scan_number"] == scan_num]
            if sub.empty or x_col not in sub.columns:
                continue
            x = sub[x_col].to_numpy(dtype=float, na_value=np.nan)
            for y_col in y_cols:
                if y_col not in sub.columns:
                    continue
                y = sub[y_col].to_numpy(dtype=float, na_value=np.nan)
                mask = ~(np.isnan(x) | np.isnan(y))
                xm, ym = x[mask], y[mask]
                if xm.size == 0:
                    continue
                if normalize and ym.size > 0:
                    y_min, y_max = np.nanmin(ym), np.nanmax(ym)
                    if y_max > y_min:
                        ym = (ym - y_min) / (y_max - y_min)
                color = _color_for(color_i)
                color_i += 1
                name = f"Scan {scan_num}: {y_col}"
                pen = pg.mkPen(color=color, width=2)
                if plot_type == "bar":
                    bg = pg.BarGraphItem(x=xm, height=ym, width=(xm.max() - xm.min()) * 0.02 if xm.size > 1 else 0.5, brush=color)
                    panel.plot_widget.addItem(bg)
                    self._add_legend_sample(panel, name, color)
                elif plot_type == "scatter":
                    panel.plot_widget.plot(
                        xm, ym, pen=None, symbol="o", symbolSize=6,
                        symbolBrush=color, symbolPen=color, name=name,
                    )
                elif plot_type == "line+scatter":
                    panel.plot_widget.plot(
                        xm, ym, pen=pen, symbol="o", symbolSize=6,
                        symbolBrush=color, symbolPen=color, name=name,
                    )
                else:
                    panel.plot_widget.plot(xm, ym, pen=pen, name=name)
                any_data = True
        for curve in (extra_curves or []):
            cx, cy = curve["x"], curve["y"]
            if cx is None or cy is None or len(cx) == 0:
                continue
            pen = pg.mkPen(
                color=curve.get("color", FIT_COLOR),
                width=curve.get("width", 2),
                style=curve.get("style", QtCore.Qt.DashLine),
            )
            panel.plot_widget.plot(cx, cy, pen=pen, name=curve.get("name", ""))
            any_data = True
        if not any_data:
            panel.show_empty("No matching data for the selected columns/scans.")
            return
        panel.plot_widget.setLabel("bottom", x_col)
        panel.plot_widget.setLabel("left", ", ".join(y_cols))
        # Always fit the view to the freshly-plotted data. pyqtgraph silently
        # disables its own auto-range as soon as the user pans/zooms the
        # chart, so without this, re-rendering after Auto-Refresh (or any
        # control change) can leave the view stuck on stale axis limits,
        # making it look like nothing updated until "⟲ Reset" is clicked
        # by hand. Forcing autoRange() here means every render — manual or
        # via Auto-Refresh — always shows the current data without that
        # extra manual step.
        panel.plot_widget.getViewBox().autoRange()

    @staticmethod
    def _add_legend_sample(panel: PlotPanel, name: str, color: str):
        # BarGraphItem doesn't auto-register with the legend; add a dummy curve.
        dummy = panel.plot_widget.plot([], [], pen=pg.mkPen(color=color, width=2), name=name)
        return dummy

    # ------------------------------------------------------------------
    # Summary tab (always-live watch-only view)
    # ------------------------------------------------------------------
    def _summary_refresh_tick(self):
        """Runs every 0.1s while the Summary tab is visible. Re-reads the
        currently loaded SPEC file from disk if it's changed since the
        last tick (tracked via its own self._summary_watch_mtime, kept
        separate from the Plot tab's Auto-Refresh/watch timers so the two
        features can't race each other), then always re-renders the
        latest-scan plot -- even on ticks where the file didn't change --
        so picking up a newer selection on the Plot tab (last_plot_info)
        shows up here within 0.1s too."""
        if self._loaded_file_path:
            try:
                mtime = os.stat(self._loaded_file_path).st_mtime
            except OSError:
                mtime = None
            if mtime is not None and (
                self._summary_watch_mtime is None or mtime > self._summary_watch_mtime
            ):
                try:
                    df, columns, metadata, scan_info = sc.load_spec_file(self._loaded_file_path)
                except Exception:
                    pass
                else:
                    self._summary_watch_mtime = mtime
                    label = os.path.basename(self._loaded_file_path)
                    self._apply_loaded_data(df, columns, metadata, scan_info, label)
        self._render_summary_plot()

    # ------------------------------------------------------------------
    # Slack Beam Alerts
    # ------------------------------------------------------------------
    def _load_remembered_slack_settings(self):
        """Restore a previously-remembered channel (and, only if the user
        opted into it last time, token) from SLACK_SETTINGS_PATH -- same
        opt-in-only persistence pattern as the Email dialog's
        _load_remembered_settings()."""
        try:
            with open(SLACK_SETTINGS_PATH, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return
        if data.get("channel"):
            self.slack_channel_edit.setText(data.get("channel", SLACK_DEFAULT_CHANNEL))
        if data.get("token"):
            self.slack_token_edit.setText(data.get("token", ""))
        self.slack_enable_chk.setChecked(bool(data.get("enabled", False)))
        self.slack_remember_chk.setChecked(bool(data.get("token")))

    def _save_remembered_slack_settings(self):
        data = {
            "channel": self.slack_channel_edit.text().strip(),
            "enabled": self.slack_enable_chk.isChecked(),
        }
        if self.slack_remember_chk.isChecked():
            data["token"] = self.slack_token_edit.text().strip()
        try:
            with open(SLACK_SETTINGS_PATH, "w") as f:
                json.dump(data, f)
            try:
                os.chmod(SLACK_SETTINGS_PATH, 0o600)
            except OSError:
                pass
        except OSError as exc:
            print("Could not save remembered Slack settings:", exc)

    def _send_slack_message(self, token: str, channel: str, text: str):
        """POSTs a chat.postMessage request to Slack's Web API using a Bot
        User OAuth Token passed as a Bearer credential -- no third-party
        Slack package, just urllib.request (stdlib) and json. Returns
        (ok: bool, detail: str); detail is either "" on success or an
        error message (Slack's own "error" field, e.g. "channel_not_found"
        / "not_in_channel" / "invalid_auth", or a network/HTTP failure
        description) on failure."""
        payload = json.dumps({"channel": channel, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            SLACK_POST_MESSAGE_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)
        if not body.get("ok"):
            return False, body.get("error", "unknown Slack API error")
        return True, ""

    def _on_slack_test_clicked(self):
        token = self.slack_token_edit.text().strip()
        channel = self.slack_channel_edit.text().strip() or SLACK_DEFAULT_CHANNEL
        if not token:
            QtWidgets.QMessageBox.warning(
                self, "Slack Alerts", "Enter a Slack Bot Token first."
            )
            return
        self.slack_test_btn.setEnabled(False)
        ok, detail = self._send_slack_message(
            token, channel,
            f"🔧 Test message from {APP_TITLE} -- Slack Alerts are "
            "configured correctly.",
        )
        self.slack_test_btn.setEnabled(True)
        if self.slack_remember_chk.isChecked():
            self._save_remembered_slack_settings()
        if ok:
            self.slack_status_label.setText(
                f"✅ Test message sent to {channel} at {time.strftime('%H:%M:%S')}."
            )
        else:
            self.slack_status_label.setText(f"❌ Send failed: {detail}")
            QtWidgets.QMessageBox.critical(
                self, "Slack Alerts", f"Could not send test message: {detail}"
            )

    def _maybe_alert_beam_slack(self, no_beam: bool, value: Optional[float] = None):
        """Called once per _beam_signals_tick() with the current no-beam
        state (same csig.is_no_beam(value) boolean that drives
        no_beam_banner's visibility) and the live reading of the config's
        signals.no_beam.channel that state was computed from (so the alert
        text can show the real number instead of a generic placeholder).
        Posts a Slack message
        only on a *transition* -- beam just lost (False -> True) or beam
        just restored (True -> False) -- never on every tick, so a
        no-beam period lasting hours doesn't spam the channel once a
        second. Does nothing if the "Post to Slack automatically"
        checkbox is unchecked or no token has been entered.

        First-tick handling: self._last_no_beam_state starts out as None
        (no prior tick to compare against yet). Rather than always
        skipping the very first tick -- which would silently swallow a
        real alert if the app happens to be launched while beam is
        ALREADY lost -- an unset previous state is treated as an
        implicit "beam present" baseline. That means: app starts with no
        beam -> immediately alerts (previous treated as False, current
        True, that's a transition). App starts with beam present ->
        no alert (False -> False is not a transition, which is correct,
        since beam was never actually lost)."""
        previous = self._last_no_beam_state
        self._last_no_beam_state = no_beam
        if previous is None:
            previous = False
        if previous == no_beam:
            return
        if not getattr(self, "slack_enable_chk", None) or not self.slack_enable_chk.isChecked():
            return
        token = self.slack_token_edit.text().strip()
        channel = self.slack_channel_edit.text().strip() or SLACK_DEFAULT_CHANNEL
        if not token:
            return
        no_beam_cfg = CONFIG["signals"]["no_beam"]
        label = csig.CHANNELS[no_beam_cfg["channel"]]["label"]
        units = no_beam_cfg.get("units") or ""
        value_text = f"{value:,.3f} {units}".rstrip() if value is not None else "unavailable"
        where = f"{APP_TITLE}, {CONFIG['beamline'].get('station', '')}".rstrip(", ")
        if no_beam:
            text = f"🚨 No Beam -- {label} reading {value_text} ({where})."
        else:
            text = f"✅ Beam Restored -- {label} reading {value_text} ({where})."
        # Append the actual current status message from the config's
        # signals.status_page_url (e.g. "Investigating", "Refilling",
        # operator notes, ...) when it can be fetched -- silently omitted
        # (falls back to the generic text above only) if no page is
        # configured, it can't be reached, or its markup doesn't match any
        # known pattern. See chess_signals.fetch_beam_status_message()'s
        # docstring for why this can come back empty even on-site (the
        # page may need JS execution to show the text).
        status_message = csig.fetch_beam_status_message()
        if status_message:
            text += f"\nStatus ({csig.NEW_STATUS_URL}): {status_message}"
        ok, detail = self._send_slack_message(token, channel, text)
        stamp = time.strftime("%H:%M:%S")
        if ok:
            self.slack_status_label.setText(f"✅ Alert sent to {channel} at {stamp}.")
        else:
            self.slack_status_label.setText(f"❌ Alert failed at {stamp}: {detail}")

    def _beam_signals_tick(self):
        """Runs every 1s for the whole life of the app. Reads the live value
        of every channel in the config's signals.channels via
        chess_signals.get_live_values() and updates the Summary tab's
        readout cards. Checks the currently loaded SPEC file's own columns
        (self.df/self.columns); if the "Try live network fetch" checkbox is
        checked, also makes a direct network request for every channel
        with a PV and, for each one, that live network value OVERRIDES the
        SPEC-file value (network-first), falling back to the SPEC-file
        value only if the network request for that channel fails/finds
        nothing. See csig.get_live_values() for why network-first.

        Also toggles the "No Beam" banner: shown whenever the reading of
        the config's signals.no_beam.channel (if any) is near enough to 0
        per chess_signals.is_no_beam() -- a small tolerance rather than an
        exact 0.0 match, since a genuinely-no-beam reading can still wander
        around a small "dark current" baseline. The banner is left hidden
        (not shown as "no beam") when there's no reading at all (None) --
        that's "unknown", not confirmed no-beam."""
        use_network = bool(
            getattr(self, "beam_network_checkbox", None)
            and self.beam_network_checkbox.isChecked()
        )
        values = csig.get_live_values(self.df, self.columns, use_network=use_network)
        for canonical, labels in getattr(self, "signal_labels", {}).items():
            info = values.get(canonical) or {}
            value = info.get("value")
            source = info.get("source")
            column = info.get("column")
            if value is None:
                text = "—"
                tooltip = ("No live value (no matching SPEC column" +
                           (", network fetch off)" if not use_network
                            else " and network fetch found nothing)"))
            else:
                text = f"{value:,.2f}"
                # Names the exact SPEC column (or PV) the number came from,
                # so a wrong-looking value can be diagnosed at a glance --
                # e.g. it matched some other, differently-numbered column
                # by mistake -- instead of just being trusted blindly.
                if source == "spec":
                    tooltip = f"Source: loaded SPEC file, column \"{column}\""
                else:
                    tooltip = f"Source: {csig.BASE_URL} (live network), PV {column}"
            for lbl in labels:
                lbl.setText(text)
                lbl.setToolTip(tooltip)

        no_beam_channel = CONFIG["signals"]["no_beam"].get("channel")
        if not no_beam_channel:
            return
        no_beam_value = (values.get(no_beam_channel) or {}).get("value")
        no_beam = csig.is_no_beam(no_beam_value)
        banner = getattr(self, "no_beam_banner", None)
        if banner is not None:
            banner.setVisible(no_beam)
        self._maybe_alert_beam_slack(no_beam, no_beam_value)

    def _render_summary_plot(self):
        """Draw the newest scan into the Summary tab's own PlotPanel, using
        the same X/Y column choices as the last plot made on the Plot tab
        (self.last_plot_info) if there is one, or the same
        last-scan/priority-counter/swept-motor defaults the Plot tab itself
        falls back to otherwise. Always follows whichever scan is newest,
        regardless of what's selected in the Plot tab's own scan list --
        that's the whole point of a "watch the live scan" view."""
        panel = self.summary_plot_panel
        if self.df is None:
            panel.show_empty("Load a SPEC file first.")
            return
        scan_numbers = self._scan_numbers_for(self.df)
        if not scan_numbers:
            panel.show_empty("No scans found.")
            return
        last_scan_str = scan_numbers[-1]

        x_col = None
        y_cols: List[str] = []
        if self.last_plot_info:
            x_col = self.last_plot_info.get("x_column")
            y_cols = [c for c in (self.last_plot_info.get("y_columns") or []) if c in self.columns]
        if not x_col or x_col not in self.columns or not y_cols:
            default_y = _default_y_columns(self.columns)
            y_cols = default_y if default_y else (self.columns[:1] if self.columns else [])
            command = self._command_for_scan(last_scan_str)
            x_col = _default_x_column(command, self.columns) or (
                self.columns[0] if self.columns else None
            )
        if not x_col or not y_cols:
            panel.show_empty("No columns available yet.")
            return

        self._render_plot(panel, self.df, x_col, y_cols, [int(last_scan_str)], "line", False)
        self.summary_status_label.setText(
            f"Always-live view — scan {last_scan_str}, updated "
            f"{time.strftime('%H:%M:%S')}."
        )

    # ------------------------------------------------------------------
    # Compare tab
    # ------------------------------------------------------------------
    def load_reference_file(self):
        """Load a second SPEC file, independent of the main one, so a scan
        from it can be overlaid on the Plot tab's chart for comparison.
        This folds in the one capability the old standalone "Compare Plot"
        tab offered beyond this tab's own multi-scan overlay."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Reference SPEC File", self.current_browse_path
        )
        if not path:
            return
        try:
            df2, columns2, metadata2, scan_info2 = sc.load_spec_file(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Loading File", str(exc))
            return
        self.df2, self.columns2, self.metadata2, self.scan_info2 = df2, columns2, metadata2, scan_info2
        self._reference_file_path = path
        self.reference_file_label.setText(
            f"Reference: {os.path.basename(path)} ({len(scan_info2)} scans)"
        )
        scan_numbers = self._scan_numbers_for(df2)
        _populate_combo(self.reference_scan_combo, scan_numbers)
        _populate_combo(self.reference_y_combo, columns2)
        self._apply_reference_defaults(scan_numbers)
        self.reference_overlay_chk.setChecked(True)
        if self.df is not None:
            self.do_plot()

    def _apply_reference_defaults(self, scan_numbers: List[str]):
        """Same default-selection logic as the main Plot tab, applied to
        the reference file: last scan, priority-counter Y column."""
        if not scan_numbers or not self.columns2:
            return
        _select_combo_text(self.reference_scan_combo, scan_numbers[-1])
        default_y = _default_y_columns(self.columns2)
        if default_y:
            _select_combo_text(self.reference_y_combo, default_y[0])

    def _reference_overlay_curve(self, x_col: str):
        """Build the optional reference-file overlay curve for do_plot(),
        or return None if there's no reference file loaded, the overlay
        checkbox is off, or the chosen X column doesn't exist in the
        reference file (X column is shared with the main plot's combo, so
        this can legitimately not line up)."""
        if self.df2 is None or not getattr(self, "reference_overlay_chk", None):
            return None
        if not self.reference_overlay_chk.isChecked():
            return None
        scan_text = self.reference_scan_combo.currentText()
        y_col = self.reference_y_combo.currentText()
        if not scan_text or not y_col or not x_col:
            return None
        if x_col not in self.columns2 or y_col not in self.columns2:
            return None
        try:
            scan_num = int(scan_text)
        except ValueError:
            return None
        if self.df2 is None or "scan_number" not in self.df2.columns:
            return None
        sub = self.df2[self.df2["scan_number"] == scan_num]
        if sub.empty or x_col not in sub.columns or y_col not in sub.columns:
            return None
        x = sub[x_col].to_numpy(dtype=float, na_value=np.nan)
        y = sub[y_col].to_numpy(dtype=float, na_value=np.nan)
        mask = ~(np.isnan(x) | np.isnan(y))
        xm, ym = x[mask], y[mask]
        if xm.size == 0:
            return None
        return xm, ym, f"Reference: scan {scan_num} ({y_col})"

    # ------------------------------------------------------------------
    # Folder Timeline tab
    # ------------------------------------------------------------------
    def _build_timeline_tab(self):
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)

        top = QtWidgets.QHBoxLayout()
        btn_choose = QtWidgets.QPushButton("Browse Folder…")
        btn_choose.clicked.connect(self.load_timeline)
        top.addWidget(btn_choose)
        btn_find = QtWidgets.QPushButton("Find Data Folder for Selected Scan")
        btn_find.clicked.connect(self.find_data_for_selected_timeline_row)
        top.addWidget(btn_find)
        self.timeline_summary_btn = QtWidgets.QPushButton("📋 Summary")
        self.timeline_summary_btn.clicked.connect(self.toggle_timeline_summary)
        top.addWidget(self.timeline_summary_btn)
        top.addStretch(1)
        layout.addLayout(top)

        self.timeline_table = QtWidgets.QTableWidget(0, 5)
        self.timeline_table.setHorizontalHeaderLabels(
            ["Scan #", "SPEC File", "Command", "Timestamp", "Temperature"]
        )
        self.timeline_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.timeline_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        layout.addWidget(self.timeline_table, 2)

        self.timeline_result_label = QtWidgets.QLabel("")
        layout.addWidget(self.timeline_result_label)

        # Experiment Summary panel — hidden until "📋 Summary" is clicked.
        # Ported from the web dashboard's Experiment Summary feature: per-file
        # stat cards, a per-sample/subfolder breakdown table, and charts.
        self.timeline_summary_scroll = QtWidgets.QScrollArea()
        self.timeline_summary_scroll.setWidgetResizable(True)
        self.timeline_summary_scroll.setVisible(False)
        self.timeline_summary_content = QtWidgets.QWidget()
        self.timeline_summary_layout = QtWidgets.QVBoxLayout(self.timeline_summary_content)
        self.timeline_summary_scroll.setWidget(self.timeline_summary_content)
        layout.addWidget(self.timeline_summary_scroll, 3)

        self._add_tab("timeline", w)

    def load_timeline(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Browse Folder", self.current_browse_path)
        if not path:
            return
        try:
            rows = sc.folder_timeline(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error", str(exc))
            return
        self._timeline_rows = rows
        self._timeline_folder_path = path
        self.timeline_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [
                row.get("scan_number", ""),
                row.get("spec_file", ""),
                row.get("command", ""),
                row.get("timestamp", ""),
                row.get("temperature", ""),
            ]
            for c, v in enumerate(vals):
                self.timeline_table.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))
        self.timeline_table.resizeColumnsToContents()
        self.status_label.setText(f"Timeline: {len(rows)} scans found in {path}")
        # Reset the summary panel — it must be rebuilt for the newly-loaded folder.
        self.timeline_summary_scroll.setVisible(False)
        self._timeline_summary_visible = False
        self._timeline_summary_data = None
        self._clear_layout(self.timeline_summary_layout)

    def find_data_for_selected_timeline_row(self):
        sel = self.timeline_table.selectionModel().selectedRows()
        if not sel or not self._timeline_folder_path:
            QtWidgets.QMessageBox.information(self, "Find Data", "Select a row in the timeline table first.")
            return
        idx = sel[0].row()
        row = self._timeline_rows[idx]
        result = sc.find_scan_data(row["scan_number"], row["spec_file"], self._timeline_folder_path)
        if result.get("found"):
            self.timeline_result_label.setText(f"Found: {result.get('path')}")
        else:
            self.timeline_result_label.setText(f"Not found: {result.get('message', 'no matching data folder')}")

    # ------------------------------------------------------------------
    # Experiment Summary (Folder Timeline tab), ported from the web
    # dashboard's "📋 Summary" feature: per-file stat cards, a per-sample/
    # subfolder breakdown table, and three charts (scans-per-file bar chart,
    # temperature-coverage scatter, and a Gantt-style experiment timeline).
    # ------------------------------------------------------------------
    def toggle_timeline_summary(self):
        if not self._timeline_rows:
            QtWidgets.QMessageBox.information(
                self, "Experiment Summary", "Load a folder timeline first."
            )
            return
        # Track visibility with an explicit flag rather than QWidget.isVisible(),
        # since isVisible() reflects ancestor visibility too and would report
        # False (and break the toggle) before the main window has been shown.
        if self._timeline_summary_visible:
            self.timeline_summary_scroll.setVisible(False)
            self._timeline_summary_visible = False
            return
        try:
            self._build_timeline_summary()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Experiment Summary", str(exc))
            return
        self.timeline_summary_scroll.setVisible(True)
        self._timeline_summary_visible = True

    def _clear_layout(self, layout: QtWidgets.QLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            else:
                child_layout = item.layout()
                if child_layout is not None:
                    self._clear_layout(child_layout)

    def _build_timeline_summary(self):
        rows = self._timeline_rows
        folder = self._timeline_folder_path

        # ── Aggregate per spec-file (mirrors buildTimelineSummary()) ──────
        by_file: Dict[str, Dict] = {}
        for r in rows:
            sf = r.get("spec_file", "")
            f = by_file.setdefault(sf, {
                "scans": 0, "temps": [], "earliest": None, "latest": None,
                "earliest_epoch": float("inf"), "latest_epoch": float("-inf"),
                "commands": {},
            })
            f["scans"] += 1
            temp = r.get("temperature")
            if temp and temp not in f["temps"]:
                f["temps"].append(temp)
            epoch = r.get("timestamp_epoch", 0.0) or 0.0
            if epoch > 0:
                if epoch < f["earliest_epoch"]:
                    f["earliest_epoch"] = epoch
                    f["earliest"] = r.get("timestamp")
                if epoch > f["latest_epoch"]:
                    f["latest_epoch"] = epoch
                    f["latest"] = r.get("timestamp")
            cmd0 = (r.get("command") or "").split(" ")[0]
            if cmd0:
                f["commands"][cmd0] = f["commands"].get(cmd0, 0) + 1

        # ── Overall experiment window ──────────────────────────────────────
        # rows are newest-first (per sc.folder_timeline), so the oldest
        # timestamped row is last and the newest is first.
        rows_with_epoch = [r for r in rows if (r.get("timestamp_epoch", 0.0) or 0.0) > 0]
        overall_start = rows_with_epoch[-1] if rows_with_epoch else None
        overall_end = rows_with_epoch[0] if rows_with_epoch else None

        duration_str = ""
        if overall_start and overall_end:
            secs = overall_end["timestamp_epoch"] - overall_start["timestamp_epoch"]
            days = int(secs // 86400)
            hrs = int((secs % 86400) // 3600)
            parts = []
            if days > 0:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            parts.append(f"{hrs} hr{'s' if hrs != 1 else ''}")
            duration_str = " ".join(parts)

        # ── Counts / calibration split ──────────────────────────────────────
        all_files = list(by_file.keys())
        sample_files = [f for f in all_files if not _is_calibration(f)]
        calib_files = [f for f in all_files if _is_calibration(f)]
        total_scans = len(rows)

        # ── Sub-folder breakdown per sample file ────────────────────────────
        subfolder_map: Dict[str, Dict] = {}
        for sf in sample_files:
            try:
                subfolder_map[sf] = sc.spec_subfolders(folder, sf)
            except Exception:
                subfolder_map[sf] = {"spec_file": sf, "subfolders": [], "data_root": None}

        self._timeline_summary_data = {
            "by_file": by_file,
            "sample_files": sample_files,
            "calib_files": calib_files,
            "subfolder_map": subfolder_map,
            "folder": folder,
            "overall_start": overall_start,
            "overall_end": overall_end,
            "duration_str": duration_str,
            "total_scans": total_scans,
        }
        self._render_timeline_summary(
            by_file, sample_files, calib_files, overall_start, overall_end,
            duration_str, total_scans, folder, subfolder_map,
        )

    def _render_timeline_summary(self, by_file, sample_files, calib_files,
                                  overall_start, overall_end, duration_str,
                                  total_scans, folder, subfolder_map):
        self._clear_layout(self.timeline_summary_layout)

        folder_name = os.path.basename(os.path.normpath(folder)) if folder else ""
        header_row = QtWidgets.QHBoxLayout()
        header = QtWidgets.QLabel(f"<b>📋 Experiment Summary</b> — {folder_name}")
        header.setStyleSheet("font-size: 14px; padding: 4px 0;")
        header_row.addWidget(header)
        header_row.addStretch(1)
        btn_download_summary = QtWidgets.QPushButton("⬇ Download Summary (PDF)")
        btn_download_summary.clicked.connect(self.download_timeline_summary)
        header_row.addWidget(btn_download_summary)
        self.timeline_summary_layout.addLayout(header_row)

        # ── Stat cards ───────────────────────────────────────────────────
        cards_row = QtWidgets.QHBoxLayout()
        calib_label = (
            "excl. calibration: " + ", ".join(calib_files)
            if calib_files else "no calibration files"
        )
        cards_row.addWidget(self._make_stat_card("🧪 Samples", str(len(sample_files)), calib_label))
        cards_row.addWidget(self._make_stat_card(
            "📊 Total Scans", str(total_scans),
            f"across {len(sample_files) + len(calib_files)} SPEC files",
        ))
        cards_row.addWidget(self._make_stat_card(
            "⏱ Experiment Start",
            overall_start["timestamp"] if overall_start else "N/A",
            f"{overall_start['spec_file']} scan {overall_start['scan_number']}" if overall_start else "",
        ))
        cards_row.addWidget(self._make_stat_card(
            "🏁 Experiment End",
            overall_end["timestamp"] if overall_end else "N/A",
            f"⏳ Duration: {duration_str}" if duration_str else "",
        ))
        self.timeline_summary_layout.addLayout(cards_row)

        # ── Per-sample detail table (with subfolder row-grouping) ─────────
        table = self._build_summary_table(by_file, sample_files, calib_files, subfolder_map)
        self.timeline_summary_layout.addWidget(table)

        # ── Charts ─────────────────────────────────────────────────────────
        bar_panel = self._build_summary_bar_chart(by_file, sample_files, calib_files, subfolder_map)
        temp_panel = self._build_summary_temp_chart(by_file, sample_files, calib_files, subfolder_map)
        gantt_panel = self._build_summary_gantt_chart(by_file, sample_files, calib_files)
        charts_row = QtWidgets.QHBoxLayout()
        charts_row.addWidget(bar_panel)
        charts_row.addWidget(temp_panel)
        charts_row.addWidget(gantt_panel)
        self.timeline_summary_layout.addLayout(charts_row)
        # Keep references so "Download Summary (PDF)" can export these same
        # charts as images without rebuilding them.
        self._timeline_summary_panels = {
            "bar": bar_panel, "temp": temp_panel, "gantt": gantt_panel,
        }

    def download_timeline_summary(self):
        """Export the Experiment Summary — the stat overview, the detail
        table, and the three summary charts — as a single PDF report."""
        data = self._timeline_summary_data
        if not data:
            QtWidgets.QMessageBox.information(self, "Download Summary", "Build the experiment summary first.")
            return
        by_file = data["by_file"]
        sample_files = data["sample_files"]
        calib_files = data["calib_files"]
        subfolder_map = data["subfolder_map"]
        folder = data.get("folder") or ""
        overall_start = data.get("overall_start")
        overall_end = data.get("overall_end")
        duration_str = data.get("duration_str") or ""
        total_scans = data.get("total_scans", 0)
        ordered_files = sample_files + calib_files

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Experiment Summary", "experiment_summary.pdf", "PDF Files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="qm2_summary_")
        try:
            self._render_summary_pdf(
                path, tmp_dir, folder, by_file, sample_files, calib_files,
                subfolder_map, ordered_files, overall_start, overall_end,
                duration_str, total_scans,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Saving PDF", str(exc))
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self.status_label.setText(f"Exported experiment summary PDF to {path}")

    def _render_summary_pdf(self, path, tmp_dir, folder, by_file, sample_files,
                             calib_files, subfolder_map, ordered_files,
                             overall_start, overall_end, duration_str, total_scans):
        styles = getSampleStyleSheet()
        title_style = styles["Title"]
        heading_style = styles["Heading2"]
        body_style = styles["BodyText"]
        small_style = styles["BodyText"].clone("Small")
        small_style.fontSize = 8
        small_style.leading = 10

        story = []
        folder_name = os.path.basename(os.path.normpath(folder)) if folder else "Experiment"
        story.append(Paragraph(f"Experiment Summary — {folder_name}", title_style))
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"Folder: {folder or 'N/A'}", body_style))
        story.append(Spacer(1, 12))

        # ── Stat overview (mirrors the on-screen stat cards) ────────────
        calib_note = ", ".join(calib_files) if calib_files else "none"
        stat_rows = [
            ["Samples", str(len(sample_files)), f"Calibration files excluded: {calib_note}"],
            ["Total Scans", str(total_scans), f"Across {len(sample_files) + len(calib_files)} SPEC files"],
            ["Experiment Start", overall_start["timestamp"] if overall_start else "N/A",
             f"{overall_start['spec_file']} scan {overall_start['scan_number']}" if overall_start else ""],
            ["Experiment End", overall_end["timestamp"] if overall_end else "N/A",
             f"Duration: {duration_str}" if duration_str else ""],
        ]
        stat_table = Table(stat_rows, colWidths=[110, 150, 220])
        stat_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), rl_colors.HexColor("#f0f0f0")),
        ]))
        story.append(stat_table)
        story.append(Spacer(1, 16))

        # ── Detail table (same content as the on-screen per-file table) ──
        story.append(Paragraph("Per-File Detail", heading_style))
        story.append(Spacer(1, 6))
        headers = ["SPEC File", "Scans", "Temperatures", "Scan Types", "Sub-sample", "Sub Scans", "Sub Temps"]
        detail_rows = [headers]
        for sf in ordered_files:
            f = by_file.get(sf, {})
            is_calib = _is_calibration(sf)
            temps = ", ".join(f"{t} K" for t in sorted(f.get("temps", []), key=_safe_float)) if f.get("temps") else "—"
            commands = f.get("commands", {})
            cmd_top = ", ".join(sorted(commands, key=lambda c: -commands[c])[:3]) if commands else "—"
            sfd = subfolder_map.get(sf)
            subs = sfd.get("subfolders") if sfd else []
            label = sf + (" (calib)" if is_calib else "")
            if subs:
                for i, sub in enumerate(subs):
                    sub_temps = ", ".join(f"{t} K" for t in sub.get("temperatures", [])) or "—"
                    detail_rows.append([
                        label if i == 0 else "", f.get("scans", 0) if i == 0 else "",
                        temps if i == 0 else "", cmd_top if i == 0 else "",
                        sub.get("name", ""), str(sub.get("scan_count", "") or "—"), sub_temps,
                    ])
            else:
                detail_rows.append([label, f.get("scans", 0), temps, cmd_top, "—", "—", "—"])
        detail_table = Table(
            detail_rows, repeatRows=1,
            colWidths=[95, 40, 75, 90, 70, 55, 65],
        )
        detail_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#e5e5e5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#f7f7f7")]),
        ]))
        story.append(detail_table)
        story.append(Spacer(1, 16))

        # ── Charts, exported at high resolution from the live pyqtgraph
        # panels so they match what's shown on screen. ────────────────────
        story.append(Paragraph("Charts", heading_style))
        story.append(Spacer(1, 6))
        panels = self._timeline_summary_panels or {}
        chart_specs = [
            ("bar", "Scans per SPEC File"),
            ("temp", "Temperature Coverage"),
            ("gantt", "Experiment Timeline (per SPEC File)"),
        ]
        for key, caption in chart_specs:
            panel = panels.get(key)
            if panel is None:
                continue
            img_path = os.path.join(tmp_dir, f"{key}.png")
            try:
                exporter = pg.exporters.ImageExporter(panel.plot_widget.plotItem)
                exporter.parameters()["width"] = 1400
                exporter.export(img_path)
            except Exception:
                continue
            if not os.path.exists(img_path):
                continue
            with PILImage.open(img_path) as im:
                src_w, src_h = im.size
            max_w = 6.4 * inch
            display_w = max_w
            display_h = display_w * (src_h / src_w) if src_w else 3 * inch
            max_h = 4.2 * inch
            if display_h > max_h:
                display_h = max_h
                display_w = display_h * (src_w / src_h) if src_h else max_w
            story.append(Paragraph(caption, small_style))
            story.append(RLImage(img_path, width=display_w, height=display_h))
            story.append(Spacer(1, 10))

        doc = SimpleDocTemplate(
            path, pagesize=letter,
            leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        )
        doc.build(story)

    @staticmethod
    def _make_stat_card(title: str, value: str, sub: str) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        frame.setStyleSheet(
            f"QFrame {{ background: {BG_PANEL}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 6px; }}"
        )
        v = QtWidgets.QVBoxLayout(frame)
        title_lbl = QtWidgets.QLabel(title)
        title_lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        v.addWidget(title_lbl)
        value_lbl = QtWidgets.QLabel(str(value))
        value_lbl.setStyleSheet(f"color: {ACCENT}; font-size: 15px; font-weight: 700;")
        value_lbl.setWordWrap(True)
        v.addWidget(value_lbl)
        if sub:
            sub_lbl = QtWidgets.QLabel(sub)
            sub_lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px;")
            sub_lbl.setWordWrap(True)
            v.addWidget(sub_lbl)
        return frame

    def _build_summary_table(self, by_file, sample_files, calib_files, subfolder_map) -> QtWidgets.QTableWidget:
        headers = [
            "SPEC File", "Total Scans", "Temperatures", "Scan Types",
            "Sub-sample", "Sub Scans", "Sub Temperatures",
        ]
        ordered_files = sample_files + calib_files

        row_specs = []
        total_rows = 0
        for sf in ordered_files:
            sfd = subfolder_map.get(sf)
            has_subs = bool(sfd and sfd.get("subfolders"))
            subs = sfd["subfolders"] if has_subs else []
            row_specs.append((sf, _is_calibration(sf), has_subs, subs, sfd))
            total_rows += (len(subs) + 1) if has_subs else 1

        table = QtWidgets.QTableWidget(total_rows, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        table.verticalHeader().setVisible(False)

        r = 0
        for sf, is_calib, has_subs, subs, sfd in row_specs:
            f = by_file[sf]
            tl_temps = (
                ", ".join(f"{t} K" for t in sorted(f["temps"], key=_safe_float))
                if f["temps"] else "—"
            )
            cmd_top = (
                ", ".join(sorted(f["commands"], key=lambda c: -f["commands"][c])[:3])
                if f["commands"] else "—"
            )
            data_root = sfd.get("data_root") if sfd else None
            spec_label = sf + (" (calib)" if is_calib else "")
            if data_root:
                spec_label += f"\n{data_root}"

            def _set(row_i, col_i, text, dim=False):
                item = QtWidgets.QTableWidgetItem(str(text))
                if dim:
                    font = item.font()
                    font.setItalic(True)
                    item.setFont(font)
                    item.setForeground(QtGui.QColor("#999999"))
                table.setItem(row_i, col_i, item)

            _set(r, 0, spec_label, dim=is_calib)
            _set(r, 1, f["scans"], dim=is_calib)
            _set(r, 2, tl_temps, dim=is_calib)
            _set(r, 3, cmd_top, dim=is_calib)

            if has_subs:
                table.setSpan(r, 0, len(subs) + 1, 1)
                _set(r, 4, f"{len(subs)} sub-sample(s)", dim=is_calib)
                table.setSpan(r, 4, 1, 3)
                r += 1
                for sub in subs:
                    sub_temps = (
                        ", ".join(f"{t} K" for t in sub.get("temperatures", []))
                        or "—"
                    )
                    _set(r, 4, sub.get("name", ""), dim=is_calib)
                    _set(r, 5, sub.get("scan_count", "") or "—", dim=is_calib)
                    _set(r, 6, sub_temps, dim=is_calib)
                    r += 1
            else:
                _set(r, 4, data_root or "—", dim=is_calib)
                table.setSpan(r, 4, 1, 3)
                r += 1

        table.resizeColumnsToContents()
        table.resizeRowsToContents()
        table.setMinimumHeight(
            table.horizontalHeader().height() + table.verticalHeader().length() + 8
        )
        table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        return table

    def _build_summary_bar_chart(self, by_file, sample_files, calib_files, subfolder_map) -> PlotPanel:
        all_files = sample_files + calib_files
        panel = PlotPanel()
        _style_summary_chart(panel, "Scans per SPEC File", "Scans")
        if not all_files:
            panel.show_empty("No data")
            return panel

        axis = panel.plot_widget.getAxis("bottom")
        axis.setTicks([[(i, _short_label(sf)) for i, sf in enumerate(all_files)]])
        x = np.arange(len(all_files), dtype=float)

        all_sub_names: List[str] = []
        for sf in all_files:
            sfd = subfolder_map.get(sf)
            if sfd and sfd.get("subfolders"):
                for s in sfd["subfolders"]:
                    if s["name"] not in all_sub_names:
                        all_sub_names.append(s["name"])

        if all_sub_names:
            bottom = np.zeros(len(all_files))
            for si, sn in enumerate(all_sub_names):
                vals = []
                for sf in all_files:
                    sfd = subfolder_map.get(sf)
                    v = 0
                    if sfd and sfd.get("subfolders"):
                        for s in sfd["subfolders"]:
                            if s["name"] == sn:
                                v = s.get("scan_count", 0) or 0
                    vals.append(v)
                vals = np.array(vals, dtype=float)
                color = _color_for(si)
                bg = pg.BarGraphItem(x=x, height=vals, y0=bottom, width=0.6, brush=color)
                panel.plot_widget.addItem(bg)
                self._add_legend_sample(panel, sn, color)
                bottom = bottom + vals
        else:
            for i, sf in enumerate(all_files):
                color = CALIB_COLOR if _is_calibration(sf) else SAMPLE_COLOR
                bg = pg.BarGraphItem(x=[x[i]], height=[by_file[sf]["scans"]], width=0.6, brush=color)
                panel.plot_widget.addItem(bg)
        return panel

    def _build_summary_temp_chart(self, by_file, sample_files, calib_files, subfolder_map) -> PlotPanel:
        all_files = sample_files + calib_files
        panel = PlotPanel()
        _style_summary_chart(panel, "Temperature Coverage", "Temperature (K)")
        if not all_files:
            panel.show_empty("No data")
            return panel

        axis = panel.plot_widget.getAxis("bottom")
        axis.setTicks([[(i, _short_label(sf)) for i, sf in enumerate(all_files)]])

        spots = []
        for i, sf in enumerate(all_files):
            f = by_file[sf]
            sfd = subfolder_map.get(sf)
            temps_for_file = {str(t) for t in f["temps"]}
            if sfd and sfd.get("subfolders"):
                for sub in sfd["subfolders"]:
                    for t in sub.get("temperatures", []):
                        temps_for_file.add(str(t))
            color = CALIB_COLOR if _is_calibration(sf) else SAMPLE_COLOR
            for t in temps_for_file:
                spots.append({
                    "pos": (i, _safe_float(t)), "brush": color,
                    "pen": pg.mkPen(TEXT_PRIMARY), "size": 12,
                })

        if not spots:
            panel.show_empty("No temperature data available")
            return panel
        scatter = pg.ScatterPlotItem(spots)
        panel.plot_widget.addItem(scatter)
        return panel

    def _build_summary_gantt_chart(self, by_file, sample_files, calib_files) -> PlotPanel:
        all_files = sample_files + calib_files
        panel = PlotPanel()
        _style_summary_chart(panel, "Experiment Timeline (per SPEC File)")

        files_with_time = [
            sf for sf in all_files
            if by_file[sf]["earliest_epoch"] != float("inf")
            and by_file[sf]["latest_epoch"] != float("-inf")
        ]
        if not files_with_time:
            panel.show_empty("No timestamp data available")
            return panel

        date_axis = pg.DateAxisItem(orientation="bottom")
        panel.plot_widget.setAxisItems({"bottom": date_axis})
        axis_left = panel.plot_widget.getAxis("left")
        axis_left.setTicks([[(i, _short_label(sf)) for i, sf in enumerate(files_with_time)]])
        # Long file-name tick labels on the left axis need extra width or
        # they get clipped/overlap the plot area.
        axis_left.setWidth(110)

        for i, sf in enumerate(files_with_time):
            f = by_file[sf]
            color = CALIB_COLOR if _is_calibration(sf) else SAMPLE_COLOR
            panel.plot_widget.plot(
                [f["earliest_epoch"], f["latest_epoch"]], [i, i],
                pen=pg.mkPen(color=color, width=10),
                symbol="o", symbolBrush=color, symbolSize=12,
            )
        panel.plot_widget.setYRange(-1, len(files_with_time))
        return panel

    # ------------------------------------------------------------------
    # Export tab
    # ------------------------------------------------------------------
    def _build_export_tab(self):
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)

        btn_all = QtWidgets.QPushButton("Export All to CSV…")
        btn_all.clicked.connect(self.export_all)
        layout.addWidget(btn_all)

        btn_plotted = QtWidgets.QPushButton("Export Plotted Data to CSV…")
        btn_plotted.clicked.connect(self.export_plotted)
        layout.addWidget(btn_plotted)

        layout.addWidget(QtWidgets.QLabel("Export selected scans/columns (all selected by default):"))
        row = QtWidgets.QHBoxLayout()

        scans_col = QtWidgets.QVBoxLayout()
        scans_col.addWidget(QtWidgets.QLabel("Scan(s):"))
        self.export_scans_list = QtWidgets.QListWidget()
        self.export_scans_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        scans_col.addWidget(self.export_scans_list)
        row.addLayout(scans_col)

        cols_col = QtWidgets.QVBoxLayout()
        cols_col.addWidget(QtWidgets.QLabel("Column(s):"))
        self.export_columns_list = QtWidgets.QListWidget()
        self.export_columns_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        cols_col.addWidget(self.export_columns_list)
        row.addLayout(cols_col)

        layout.addLayout(row, 1)

        btn_selected = QtWidgets.QPushButton("Export Selected Scans/Columns to CSV…")
        btn_selected.clicked.connect(self.export_selected)
        layout.addWidget(btn_selected)

        self._add_tab("export", w)

    def _refresh_export_controls(self):
        scan_numbers = self._scan_numbers_for(self.df)
        _populate_list(self.export_scans_list, scan_numbers)
        _select_list_items(self.export_scans_list, scan_numbers)
        _populate_list(self.export_columns_list, self.columns)
        _select_list_items(self.export_columns_list, self.columns)

    def export_all(self):
        if self.df is None:
            QtWidgets.QMessageBox.information(self, "Export", "Load a SPEC file first.")
            return
        df_out = sc.export_all(self.df)
        self._save_csv(df_out)

    def export_plotted(self):
        if not self.last_plot_info:
            QtWidgets.QMessageBox.information(self, "Export", "Make a plot first.")
            return
        df_out = sc.export_plotted(
            self.df,
            self.last_plot_info["x_column"],
            self.last_plot_info["y_columns"],
            self.last_plot_info.get("scans"),
        )
        self._save_csv(df_out)

    def _render_plot_image(self) -> QtGui.QImage:
        """Render the Plot tab's chart to an in-memory QImage at high
        resolution, honoring the "Include grid lines" checkbox and
        appending whatever's in the Notes box as a caption band (see
        _compose_export_image()). This is the single rendering path shared
        by save_plot_image(), copy_plot_image() (both via
        _export_plot_image() below) and the "Send by Email" dialog's
        attachment, so a saved file, a clipboard copy and an emailed PNG
        are always byte-identical for the same chart/notes state."""
        plot_item = self.plot_panel.plot_widget.plotItem
        include_grid = self.plot_grid_chk.isChecked()
        axes = [self.plot_panel.plot_widget.getAxis(a) for a in ("bottom", "left")]
        try:
            # Force the grid on/off both via showGrid() *and* directly at
            # the axis level, then flush pending paint events before
            # exporting. showGrid() alone was reported as sometimes not
            # actually affecting the exported PNG — setting each axis's
            # own grid alpha plus a processEvents() flush guards against a
            # stale cached paint being what ImageExporter grabs.
            self.plot_panel.plot_widget.showGrid(x=include_grid, y=include_grid, alpha=0.3)
            for axis in axes:
                if axis is not None:
                    axis.setGrid(255 if include_grid else False)
            QtWidgets.QApplication.processEvents()
            exporter = pg.exporters.ImageExporter(plot_item)
            exporter.parameters()["width"] = 1600
            # Always render to an in-memory QImage first (toBytes=True),
            # regardless of the final destination — that gives us a chance
            # to append the Notes caption band below before actually
            # writing the file / setting the clipboard / attaching to an
            # email, instead of letting the exporter write/copy the bare
            # chart directly.
            base_image = exporter.export(toBytes=True)
            return self._compose_export_image(base_image)
        finally:
            # Restore to the checkbox's *actual* state (not unconditionally
            # "on") — the live chart should keep reflecting whatever the
            # user has chosen, exactly as it already did before this export.
            self.plot_panel.plot_widget.showGrid(x=include_grid, y=include_grid, alpha=0.3)
            for axis in axes:
                if axis is not None:
                    axis.setGrid(255 if include_grid else False)
            QtWidgets.QApplication.processEvents()

    def _export_plot_image(self, *, path: Optional[str] = None, to_clipboard: bool = False):
        """Save or copy the Plot tab's chart via _render_plot_image().
        Shared by save_plot_image() (path given) and copy_plot_image()
        (to_clipboard=True)."""
        final_image = self._render_plot_image()
        if to_clipboard:
            QtWidgets.QApplication.clipboard().setImage(final_image)
        else:
            final_image.save(path)

    def _compose_export_image(self, base_image: QtGui.QImage) -> QtGui.QImage:
        """If the Notes box has any text in it, append it as a wrapped
        caption band underneath the chart image; otherwise return
        base_image unchanged. Uses PIL (already a dependency, used
        elsewhere for the PDF report) rather than QPainter, since PIL's
        text layout doesn't depend on any particular Qt font backend being
        available."""
        notes = ""
        if hasattr(self, "plot_notes_edit"):
            notes = self.plot_notes_edit.toPlainText().strip()
        if not notes:
            return base_image
        from io import BytesIO

        from PIL import ImageDraw, ImageFont

        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.ReadWrite)
        base_image.save(buf, "PNG")
        pil_base = PILImage.open(BytesIO(bytes(buf.data()))).convert("RGB")
        buf.close()

        margin = 24
        font_size = 20
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        measurer = ImageDraw.Draw(pil_base)
        usable_width = max(pil_base.width - 2 * margin, 50)
        lines = self._wrap_notes_text(notes, measurer, font, usable_width)
        line_bbox = measurer.textbbox((0, 0), "Ag", font=font)
        line_height = (line_bbox[3] - line_bbox[1]) + 8
        band_height = margin * 2 + line_height * max(len(lines), 1)

        bg_rgb = QtGui.QColor(BG_PANEL).getRgb()[:3]
        fg_rgb = QtGui.QColor(TEXT_PRIMARY).getRgb()[:3]
        composed = PILImage.new(
            "RGB", (pil_base.width, pil_base.height + band_height), bg_rgb
        )
        composed.paste(pil_base, (0, 0))
        draw = ImageDraw.Draw(composed)
        y = pil_base.height + margin
        for line in lines:
            draw.text((margin, y), line, font=font, fill=fg_rgb)
            y += line_height

        out_buf = BytesIO()
        composed.save(out_buf, format="PNG")
        result = QtGui.QImage()
        result.loadFromData(out_buf.getvalue(), "PNG")
        return result

    @staticmethod
    def _wrap_notes_text(text: str, measurer, font, max_width: int) -> List[str]:
        """Word-wrap (plus honoring existing newlines) so the Notes text
        fits within max_width pixels for the given PIL font."""
        lines: List[str] = []
        for paragraph in text.splitlines() or [""]:
            words = paragraph.split(" ")
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                width = measurer.textbbox((0, 0), candidate, font=font)[2] if candidate else 0
                if width <= max_width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines or [""]

    def save_plot_image(self):
        if not self.last_plot_info:
            QtWidgets.QMessageBox.information(self, "Save Plot Image", "Make a plot first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Plot Image", "plot.png", "PNG Image (*.png)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            self._export_plot_image(path=path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Saving Image", str(exc))
            return
        self.status_label.setText(f"Saved plot image to {path}")

    def copy_plot_image(self):
        if not self.last_plot_info:
            QtWidgets.QMessageBox.information(self, "Copy Plot Image", "Make a plot first.")
            return
        try:
            self._export_plot_image(to_clipboard=True)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Copying Image", str(exc))
            return
        self.status_label.setText("Copied plot image to clipboard.")

    def open_email_dialog(self):
        """Open the "Send by Email" dialog, pre-loading it with the exact
        CSV data and PNG image that would come out of the existing "Export
        Plotted Data" / "Save Plot Image" features, so the emailed
        attachments always match what the user is currently looking at."""
        if not self.last_plot_info:
            QtWidgets.QMessageBox.information(self, "Email Plot Data", "Make a plot first.")
            return

        csv_bytes = None
        try:
            df_out = sc.export_plotted(
                self.df,
                self.last_plot_info["x_column"],
                self.last_plot_info["y_columns"],
                self.last_plot_info.get("scans"),
            )
            csv_bytes = df_out.to_csv(index=False).encode("utf-8")
        except Exception as exc:
            print("Could not build CSV attachment for email:", exc)

        png_bytes = None
        try:
            image = self._render_plot_image()
            buf = QtCore.QBuffer()
            buf.open(QtCore.QIODevice.ReadWrite)
            image.save(buf, "PNG")
            png_bytes = bytes(buf.data())
            buf.close()
        except Exception as exc:
            print("Could not build PNG attachment for email:", exc)

        scans = self.last_plot_info.get("scans") or []
        if scans:
            scan_txt = " (scan " + ", ".join(str(s) for s in scans) + ")" if len(scans) == 1 \
                else " (scans " + ", ".join(str(s) for s in scans) + ")"
        else:
            scan_txt = ""
        default_subject = f"SPEC Dashboard plot{scan_txt}"

        dlg = EmailSendDialog(
            self, csv_bytes=csv_bytes, png_bytes=png_bytes, default_subject=default_subject
        )
        dlg.exec()

    def export_selected(self):
        if self.df is None:
            QtWidgets.QMessageBox.information(self, "Export", "Load a SPEC file first.")
            return
        scans_text = _multiselect_values(self.export_scans_list)
        if not scans_text:
            QtWidgets.QMessageBox.information(self, "Export", "Select at least one scan.")
            return
        try:
            scans = [int(s) for s in scans_text]
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Export", "Scan numbers must be integers.")
            return
        columns = _multiselect_values(self.export_columns_list) or None
        df_out = sc.export_selected_scans(self.df, scans, columns)
        self._save_csv(df_out)

    def _save_csv(self, df_out):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save CSV", "export.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            df_out.to_csv(path, index=False)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error Saving File", str(exc))
            return
        self.status_label.setText(f"Exported to {path}")

    def closeEvent(self, event):
        """Stop the Live Image tab's background polling thread, and the
        Summary tab's own refresh timers, before the app exits, so closing
        the window doesn't leave a QThread (or a running QTimer) behind."""
        if hasattr(self, "live_image_tab"):
            self.live_image_tab.stop()
        if hasattr(self, "_summary_timer"):
            self._summary_timer.stop()
        if hasattr(self, "_beam_signals_timer"):
            self._beam_signals_timer.stop()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="SPEC beamline dashboard")
    parser.add_argument(
        "--config", default=bcfg.DEFAULT_CONFIG_PATH,
        help="beamline YAML config (default: %(default)s)",
    )
    args, qt_args = parser.parse_known_args()
    try:
        _apply_config(bcfg.load_config(args.config))
    except (bcfg.ConfigError, re.error) as exc:
        sys.exit(f"Config error: {exc}")

    app = QtWidgets.QApplication(sys.argv[:1] + qt_args)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    app.setStyleSheet(_build_qss())
    window = SpecDashboardApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
