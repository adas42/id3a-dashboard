"""
hexrd_display.py — connectors between the dashboard and hexrdgui's Cartesian
view.

The Live Image tab shows exactly what hexrdgui's Cartesian view renders:
`hexrdgui.calibration.cartesian_plot.cartesian_viewer()`, which warps every
detector panel onto one virtual plane using the instrument geometry. That
viewer reads the instrument and the per-panel images from hexrdgui's
`HexrdConfig` singleton, so the functions here only load an instrument file
and detector files into `HexrdConfig` the way hexrdgui's own load dialogs do
(`ImageFileManager.load_images` + `ImageLoadManager.apply_operations`), then
call the viewer unchanged.

Needs a running QApplication first: `HexrdConfig` is a QObject that reads the
application's arguments. Inside the dashboard it loads the dashboard's own
QSettings, not hexrdgui's, and nothing here saves settings. hexrdgui is
imported lazily so the rest of the dashboard works where it isn't installed.
"""

from typing import Dict, Optional, Sequence, Tuple

import numpy as np

# Flip names accepted in the config -> hexrd ProcessedImageSeries flip op.
# "lr"/"ud" are numpy's fliplr/flipud, as in the Dexela powder-integration
# config; hexrd's own names pass through. hexrd silently ignores a name it
# doesn't know, so anything else is rejected here instead.
_FLIP_OPS = {
    "lr": "v", "ud": "h",
    "v": "v", "y": "v", "h": "h", "x": "h",
    "vh": "vh", "hv": "vh", "r180": "vh", "t": "t",
    "r90": "r90", "ccw90": "r90", "r270": "r270", "cw90": "r270",
}


def load_instrument(path: str) -> list:
    """Load a hexrd instrument file (.yml/.yaml/.hexrd) into HexrdConfig.
    Returns the panel names."""
    from hexrdgui.hexrd_config import HexrdConfig

    HexrdConfig().load_instrument_config(path)
    return list(HexrdConfig().detector_names)


def load_detector_files(
    files: Dict[str, str],
    hdf5_path: Optional[Sequence[str]] = None,
    flips: Optional[Dict[str, str]] = None,
    frame: int = -1,
) -> None:
    """Open one detector file per panel into HexrdConfig().imageseries_dict.

    files: {panel name: file path}. For an instrument whose panels are ROIs
    cut from one full frame, give every panel the same file.
    hdf5_path: (group, dataset) holding the images in plain HDF5 files, e.g.
    ("imageseries", "images"); hexrdgui normally asks for this in a dialog.
    Eiger stream files are recognized from their attributes.
    flips: optional {panel name: flip}, "lr"/"ud" (numpy fliplr/flipud) or
    a hexrd flip op ("v", "h", "vh", "t", "r90", "r270"), applied before any
    ROI cut, as hexrdgui does.
    frame: which frame of each file to show; -1 is the last one.
    """
    from hexrd import imageseries
    from hexrdgui.create_hedm_instrument import create_hedm_instrument
    from hexrdgui.hexrd_config import HexrdConfig
    from hexrdgui.image_file_manager import ImageFileManager

    config = HexrdConfig()
    manager = ImageFileManager()
    if hdf5_path:
        manager.path = list(hdf5_path)

    missing = [name for name in config.detector_names if name not in files]
    if missing:
        raise ValueError(f"No detector file for panel(s): {', '.join(missing)}")

    instr = create_hedm_instrument()
    has_roi = config.instrument_has_roi
    ims_dict = config.imageseries_dict
    ims_dict.clear()
    for name in config.detector_names:
        ims = manager.open_file(files[name])
        ops = []
        flip = (flips or {}).get(name)
        if flip:
            if flip not in _FLIP_OPS:
                raise ValueError(f"Unknown flip {flip!r} for panel {name}")
            ops.append(("flip", _FLIP_OPS[flip]))
        if has_roi:
            # The rectangle op goes last, as in ImageLoadManager.apply_operations.
            ops.append(("rectangle", instr.detectors[name].roi))
        if ops:
            ims = imageseries.process.ProcessedImageSeries(ims, ops)
        ims_dict[name] = ims

    n_frames = min(len(ims) for ims in ims_dict.values())
    config.current_imageseries_idx = frame % n_frames


def render_cartesian() -> Tuple[np.ma.MaskedArray, Tuple[float, float, float, float]]:
    """Run hexrdgui's Cartesian viewer on what's loaded in HexrdConfig.
    Returns (image, extent): the stitched masked image, rows x cols, and its
    (left, right, bottom, top) extent in mm on the virtual plane."""
    from hexrdgui.calibration.cartesian_plot import cartesian_viewer

    viewer = cartesian_viewer()
    return viewer.img, viewer.extent
