"""The "Map Queue" tab: watch and steer the mapqueue runner.

It talks only to the runner's HTTP API, through mapqueue's
standard-library client, imported from the mapqueue checkout named in
the beamline YAML's `map_queue` section. Every request runs in a worker
thread, so the UI never waits on the network. The runner is the only
thing that changes the queue: a drag-and-drop or a button asks it to,
and the table shows the result on the next fetch.
"""

import json
import os
import queue as job_queue
import sys
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

# Points that can be moved or removed; the runner refuses the others.
EDITABLE = ("pending", "blocked")
STATUSES = ("pending", "blocked", "running", "done", "failed")
COLUMNS = [
    ("id", "Point"), ("status", "Status"), ("sample", "Sample"),
    ("dataset_id", "Dataset"), ("scan", "Scan"), ("mode", "Mode"),
    ("attempts", "Attempts"), ("scan_n", "Scan #"), ("problems", "Problems"),
]
MIME_TYPE = "application/x-mapqueue-ids"
STATUS_COLORS = {"running": "#2a7ae2", "blocked": "#e69500",
                 "failed": "#d9534f", "done": "#8a8a8a"}


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
        if role == QtCore.Qt.ForegroundRole and item["status"] in STATUS_COLORS:
            return QtGui.QBrush(QtGui.QColor(STATUS_COLORS[item["status"]]))
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
                 poll_s: float = 1.0, parent=None):
        super().__init__(parent)
        self.url = url
        self._status: Dict = {}
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
        self.state_label = QtWidgets.QLabel("connecting…")
        font = self.state_label.font()
        font.setBold(True)
        self.state_label.setFont(font)
        top.addWidget(self.state_label)
        self.where_label = QtWidgets.QLabel("")
        top.addWidget(self.where_label)
        top.addStretch(1)
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
        outer.addLayout(filters)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
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
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._show_details)
        splitter.addWidget(self.table)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a point to see its "
                                        "parameters and history.")
        splitter.addWidget(self.details)
        splitter.setSizes([500, 200])
        outer.addWidget(splitter, 1)

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

    # Worker results

    def _on_status(self, status: Dict):
        self._status = status
        state = status.get("state", "connecting…")
        if status.get("paused"):
            state += f" (paused: {status.get('pause_reason') or 'paused'})"
        self.state_label.setText(state)
        if status:
            self.where_label.setText(
                f"   sample {status.get('current_sample')}   mode "
                f"{status.get('current_mode')}   running {status.get('running')}")
            self.connection_label.setText(self.url)
        counts = status.get("counts") or {}
        self.counts_label.setText(
            "   " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
        holding = status.get("holding")
        self.hold_label.setText("⚠ holding for beam: " + "; ".join(holding)
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

    def _on_unreachable(self, error: str):
        self.state_label.setText("runner not reachable")
        self.connection_label.setText(f"{self.url}: {error}")
        self._on_status({})
        self.state_label.setText("runner not reachable")

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

    def _show_details(self, *_):
        ids = self._selected_ids()
        if len(ids) != 1:
            self.details.setPlainText("" if not ids else
                                      f"{len(ids)} points selected")
            return
        item = self.model.item(self.model.row_of(ids[0]))
        shown = {key: item.get(key) for key in
                 ("id", "status", "attempts", "scan_n", "problems", "source",
                  "added", "started", "finished", "params", "history")}
        self.details.setPlainText(json.dumps(shown, indent=1))

    def shutdown(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
