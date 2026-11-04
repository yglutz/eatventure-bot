"""Explicit, capture-only ADB calibration preview; never enables gameplay/input."""

import argparse
import os
from pathlib import Path
import time

import cv2
import numpy as np

from adb_backend import ADBBackend
import config


def _owns_path(path, handle):
    """A replaced pathname (including a symlink) is not ours to remove."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    reserved = os.fstat(handle.fileno())
    return (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino)


def annotated_preview(frame):
    """Draw configured logical coordinates, without rescaling their positions."""
    preview = frame.copy()
    height, width = frame.shape[:2]
    for name, point in vars(config).items():
        if name.endswith('_POS') and isinstance(point, tuple) and len(point) == 2:
            x, y = point
            cv2.drawMarker(preview, (x, y), (0, 255, 255),
                           cv2.MARKER_CROSS, 12, 1)
            cv2.putText(preview, name, (max(0, x - 130), max(12, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, .3, (0, 255, 255), 1)
    prefixes = sorted({name[:-6] for name in vars(config)
                       if name.startswith('FORBIDDEN_ZONE_') and name.endswith('_X_MIN')})
    for prefix in ['FORBIDDEN_CLICK', *prefixes]:
        x1 = getattr(config, prefix + '_X_MIN')
        x2 = getattr(config, prefix + '_X_MAX')
        y1 = getattr(config, prefix + '_Y_MIN')
        y2 = getattr(config, prefix + '_Y_MAX', height - 1)
        cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 0, 255), 1)
        cv2.putText(preview, prefix, (x1, max(12, y1)),
                    cv2.FONT_HERSHEY_SIMPLEX, .3, (0, 0, 255), 1)
    # A separate banner avoids covering the coordinates being inspected.
    banner = np.zeros((52, max(width, 460), 3), dtype=np.uint8)
    cv2.putText(banner, 'PREVIEW - NOT approved calibration', (6, 20),
                cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 0, 255), 1)
    cv2.putText(banner, 'Logical coordinates; out-of-frame targets are clipped', (6, 42),
                cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
    canvas = np.zeros((height + 52, banner.shape[1], 3), dtype=np.uint8)
    canvas[:52] = banner
    canvas[52:, :width] = preview
    return canvas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--native-size', nargs=2, type=int, required=True, metavar=('W', 'H'))
    parser.add_argument('--crop', nargs=4, type=int, required=True, metavar=('X', 'Y', 'W', 'H'))
    parser.add_argument('--logical-size', nargs=2, type=int, required=True, metavar=('W', 'H'))
    parser.add_argument('--output', type=Path, required=True, help='New normalized .png file')
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != '.png':
        parser.error('--output must have a .png extension')
    preview_path = args.output.with_name(args.output.stem + '.annotated.png')
    created = []
    backend = None
    try:
        # Exclusive creation also rejects dangling symlinks and racing writers.
        for path in (args.output, preview_path):
            handle = path.open('xb')
            created.append((path, handle))
        backend = ADBBackend(serial=args.serial, package=args.package,
                             native_size=args.native_size, crop=args.crop,
                             logical_size=args.logical_size)
        start = time.monotonic()
        frame = backend.capture()
        elapsed = time.monotonic() - start
        for (path, handle), image in zip(created, (frame, annotated_preview(frame))):
            if not _owns_path(path, handle):
                raise RuntimeError(f'Reserved output was replaced: {path}')
            ok, encoded = cv2.imencode('.png', image)
            if not ok:
                raise RuntimeError('PNG encoding failed')
            # Never reopen the pathname: capture may have allowed another process
            # to replace it with a symlink to an unrelated file.
            handle.write(encoded.tobytes())
            handle.flush()
        if any(not _owns_path(path, handle) for path, handle in created):
            raise RuntimeError('Reserved output was replaced')
    except Exception as exc:
        for path, handle in created:
            if _owns_path(path, handle):
                path.unlink(missing_ok=True)
        parser.exit(1, f'Capture-only calibration failed: {exc}\n')
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            for _, handle in created:
                handle.close()
    print(f'Capture elapsed: {elapsed:.3f} s')
    print(f'Normalized screenshot: {args.output}')
    print(f'Preview (NOT approved calibration): {preview_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
