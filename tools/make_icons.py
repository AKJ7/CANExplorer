"""Generate the application, desktop, packaging and documentation icons from the logo artwork (docs/assets/logo.svg).

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/make_icons.py

The logo is a flat vector mark (a diamond frame around a solid diamond, in the brand gradient) with transparent
space around it, so every icon is just the SVG rendered at that size. Run this after changing docs/assets/logo.svg.
"""

import struct
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'docs' / 'assets'
LOGO_SVG = ASSETS / 'logo.svg'
TRANSPARENT = ASSETS / 'logo-transparent.png'  # A large transparent raster of the logo, for reuse outside the app

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_TYPES = ((b'ic07', 128), (b'ic08', 256), (b'ic09', 512), (b'ic10', 1024))
DESKTOP_SIZES = (16, 32, 48, 64, 128, 256, 512)
BANNER_SIZE = 720

_renderer: QSvgRenderer | None = None


def render(size: int) -> QImage:
    """The logo rendered into a transparent square of the given size."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    _renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print('wrote', path.relative_to(ROOT))


def save(image: QImage, path: Path) -> None:
    write(path, png_bytes(image))


def ico(images: dict) -> bytes:
    """Windows icon with one PNG entry per size."""
    entries = [(size, png_bytes(image)) for size, image in sorted(images.items())]
    offset = 6 + 16 * len(entries)
    header = struct.pack('<HHH', 0, 1, len(entries))
    directory = b''
    for size, data in entries:
        dimension = 0 if size >= 256 else size  # 0 means 256
        directory += struct.pack('<BBBBHHII', dimension, dimension, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + directory + b''.join(data for _, data in entries)


def icns(images: dict) -> bytes:
    """macOS icon with PNG entries."""
    body = b''
    for kind, size in ICNS_TYPES:
        data = png_bytes(images[size])
        body += kind + struct.pack('>I', len(data) + 8) + data
    return b'icns' + struct.pack('>I', len(body) + 8) + body


def main() -> None:
    global _renderer
    QGuiApplication([])
    _renderer = QSvgRenderer(str(LOGO_SVG))
    if not _renderer.isValid():
        raise SystemExit(f'Could not read {LOGO_SVG}')

    sizes = sorted({512, 1024, *ICO_SIZES, *(size for _, size in ICNS_TYPES), *DESKTOP_SIZES})
    icons = {size: render(size) for size in sizes}

    save(icons[1024], TRANSPARENT)

    # Application: window icon, sidebar, about dialog and welcome page
    save(icons[512], ROOT / 'can_explorer' / 'gui' / 'qt' / 'CanExplorer' / 'images' / 'logo.png')

    # Desktop entry icons (installed by `can-explorer-desktop`, see can_explorer/desktop.py)
    for size in DESKTOP_SIZES:
        save(icons[size], ROOT / 'can_explorer' / 'resources' / 'icons' / f'{size}.png')

    # Packaging: PyInstaller and Nuitka icons per platform
    packaging = ROOT / 'packaging' / 'icons'
    save(icons[512], packaging / 'canexplorer.png')
    write(packaging / 'canexplorer.ico', ico({size: icons[size] for size in ICO_SIZES}))
    write(packaging / 'canexplorer.icns', icns(icons))

    # Documentation: header logo, favicon and the banner of the home page and the README
    save(icons[128], ASSETS / 'icon.png')
    save(icons[64], ASSETS / 'favicon.png')
    save(render(BANNER_SIZE), ASSETS / 'logo-banner.png')


if __name__ == '__main__':
    main()
