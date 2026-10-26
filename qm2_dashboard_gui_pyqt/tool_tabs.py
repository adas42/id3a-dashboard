"""Tools from other repos (e.g. fast-scripts), shown as Tools-menu tabs.

The beamline YAML's `tools:` section lists them:

    tools:
      - key: array_gen
        label: Array Generation
        description: ...
        path: /nfs/chess/user/chess_id3a/fast-scripts   # a repo root
        widget: array_generation_gui.array_gen_gui:ArrayGenPanel
        options: {mode: ff}                             # optional

The contract a tool keeps:
- `path` (the repo root) goes on sys.path; the tool's own folder never
  does. The tool is a package there (a folder with an __init__.py), and
  it is imported by its dotted name.
- Inside the package, sibling modules are imported relatively
  (`from . import array_gen`), never bare (`import array_gen`), so two
  tools' modules can't collide. Checked before importing.
- `Class` is a QWidget, built as Class(**options). It doesn't create a
  QApplication, call sys.exit, change the window, or block the UI
  thread for long (slow work goes in a thread).
- Optional: `set_context(dict)` (the dashboard's context, e.g. "btr";
  unknown keys are ignored), `apply_theme(dict)` (the palette, on a
  theme switch), a `message` Signal(str) (shown in the status bar), and
  `shutdown()` (when the dashboard closes).

What the dashboard does in return: a tool that breaks any of this, or
fails while loading, becomes a tab saying why; the rest runs on. Errors
in the optional calls are printed, not fatal. About shows each tool's
source and git commit.
"""

import ast
import importlib
import os
import subprocess
import sys
import traceback
from typing import Callable, Dict, List

from PySide6 import QtCore, QtWidgets


def bare_sibling_imports(package_dir: str) -> List[str]:
    """Imports inside a package that name a module next to them without
    the package (`import utils` instead of `from . import utils`)."""
    problems = []
    for folder, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        siblings = {f[:-3] for f in files if f.endswith(".py")}
        siblings |= {d for d in dirs
                     if any(f.endswith(".py")
                            for f in os.listdir(os.path.join(folder, d)))}
        siblings.discard("__init__")
        for name in sorted(f for f in files if f.endswith(".py")):
            path = os.path.join(folder, name)
            shown = os.path.relpath(path, os.path.dirname(package_dir))
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), path)
            except (OSError, SyntaxError, ValueError) as exc:
                problems.append(f"{shown}: can't read it ({exc})")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif (isinstance(node, ast.ImportFrom) and node.level == 0
                      and node.module):
                    modules = [node.module.split(".")[0]]
                else:
                    continue
                problems.extend(
                    f"{shown}:{node.lineno}: bare import of {module!r}; "
                    f"use 'from . import {module}'"
                    for module in modules if module in siblings)
    return problems


class ToolError(Exception):
    """A tool doesn't keep the contract."""


def _build(entry: Dict) -> QtWidgets.QWidget:
    root = os.path.abspath(entry["path"])
    module_name, class_name = entry["widget"].split(":")
    package = module_name.split(".")[0]
    package_dir = os.path.join(root, package)
    if not os.path.isdir(root):
        raise ToolError(f"path {root} isn't a folder")
    if not os.path.isfile(os.path.join(package_dir, "__init__.py")):
        raise ToolError(f"{package_dir} needs an __init__.py (a package, so "
                        "its modules can't collide with other tools')")
    problems = bare_sibling_imports(package_dir)
    if problems:
        raise ToolError("bare imports of sibling modules:\n" + "\n".join(problems))
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, QtWidgets.QWidget)):
        raise ToolError(f"{entry['widget']} isn't a QWidget class")
    return cls(**entry["options"])


class ToolErrorPanel(QtWidgets.QWidget):
    """Stands in for a tool that couldn't be loaded."""

    def __init__(self, entry: Dict, details: str, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(f"Couldn't load {entry['label']} "
                                 f"({entry['widget']} from {entry['path']})")
        title.setStyleSheet("font-weight: bold")
        title.setWordWrap(True)
        layout.addWidget(title)
        text = QtWidgets.QPlainTextEdit(details)
        text.setReadOnly(True)
        layout.addWidget(text, 1)
        hint = QtWidgets.QLabel("The tool contract is in tool_tabs.py; the "
                                "beamline YAML's tools: section says where "
                                "the tool is.")
        hint.setProperty("secondaryText", True)
        layout.addWidget(hint)


def load_tool(entry: Dict) -> QtWidgets.QWidget:
    """The tool's widget, or a ToolErrorPanel saying why not."""
    try:
        return _build(entry)
    except ToolError as exc:
        details = str(exc)
    except Exception:  # anything the tool raises while importing or building
        details = traceback.format_exc()
    print(f"tool {entry['key']}: {details}", file=sys.stderr)
    return ToolErrorPanel(entry, details)


def call_optional(widget: QtWidgets.QWidget, name: str, *args):
    """Call an optional contract method if the tool has it; a tool's error
    is printed, not passed on to the dashboard."""
    method = getattr(widget, name, None)
    if not callable(method):
        return
    try:
        method(*args)
    except Exception:
        traceback.print_exc()


def connect_messages(widget: QtWidgets.QWidget, callback: Callable[[str], None]):
    """Send the tool's `message` signal, if it has one, to ``callback``."""
    signal = getattr(widget, "message", None)
    if isinstance(signal, QtCore.SignalInstance):
        signal.connect(callback)


def source_version(path: str) -> str:
    """The git commit of a tool's repo, e.g. "0ea79c5" or "0ea79c5 +
    uncommitted changes"; best effort."""
    try:
        head = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        if head.returncode:
            return "not a git checkout"
        dirty = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return head.stdout.strip() + (" + uncommitted changes"
                                  if dirty.stdout.strip() else "")
