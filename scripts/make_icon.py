"""Generate the EFOS Windows icon from the in-app brand artwork.

The repository intentionally keeps the icon out of source control (the existing
packaging configuration only needs an .ico at build time). build.ps1 calls this
helper when assets/efos.ico is absent so the executable still carries the EFOS
brand mark.

Usage:
    python scripts/make_icon.py build\\efos.ico
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from efos_app.ui.icons import brand_pixmap  # noqa: E402
from efos_app.ui.theme import apply_theme  # noqa: E402


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/efos.ico").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    apply_theme(app, "dark")
    pixmap = brand_pixmap(256)
    temporary = target.with_suffix(".png")
    if not pixmap.save(str(temporary), "PNG"):
        raise SystemExit(f"Could not render the EFOS brand icon to {temporary}")
    try:
        image = Image.open(temporary).convert("RGBA")
        image.save(
            target,
            format="ICO",
            sizes=[
                (16, 16),
                (24, 24),
                (32, 32),
                (48, 48),
                (64, 64),
                (128, 128),
                (256, 256),
            ],
        )
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    print(f"Wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
