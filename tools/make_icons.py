"""Generate the transparent logo and the application, desktop, packaging and documentation icons from the logo
artwork (docs/assets/logo.png, drawn on black).

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/make_icons.py

The black background is made transparent. The artwork is drawn in flat colours (navy head, white fur, dark navy
nose and eye ring), which stay opaque together with everything they enclose. The rest is glow: a colour fading into
black, so its opacity is its brightness relative to the colour it fades from (navy, cyan or white; "colour to
alpha"). The glow is narrowed, leaving a soft edge.
"""

import struct
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QRect, QRectF, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'docs' / 'assets'
ARTWORK = ASSETS / 'logo.png'
TRANSPARENT = ASSETS / 'logo-transparent.png'

# Flat colours of the artwork (RGB) and how far a pixel may differ from them to count as artwork
NAVY, DARK_NAVY = (1, 39, 94), (0, 20, 60)  # Head; nose and eye ring
NAVY_TOLERANCE = 4
# The glow fading out passes through dark navy too, so dark navy counts only in flat areas (the glow is a
# gradient): where the pixels FLAT_DISTANCE away are dark navy as well, and up to that distance from there
DARK_TOLERANCE, FLAT_TOLERANCE, FLAT_DISTANCE = 10, 4, 8
WHITE_LEVEL = 238  # Pixels with all channels at least this bright are white fur
# Brightness the glow has where it is opaque, by hue: navy, cyan (whiskers) and white
NAVY_LEVEL, CYAN_LEVEL, WHITE_GLOW_LEVEL = 94, 210, 255
ENCLOSING_ALPHA = 0.9  # Navy shading this opaque also encloses (keeps the inside of the head from leaking out)
GLOW_FADE = 3.0  # The glow's opacity is raised to this power, narrowing the wide halo of the artwork
CROP_ALPHA = 0.04  # Icons are cropped to the pixels at least this opaque
MARGIN = 0.04  # Free space around the icon content, of the icon size
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_TYPES = ((b'ic07', 128), (b'ic08', 256), (b'ic09', 512), (b'ic10', 1024))
DESKTOP_SIZES = (16, 32, 48, 64, 128, 256, 512)


def _distance(colour: tuple, reference: tuple) -> int:
    return sum(abs(a - b) for a, b in zip(colour, reference, strict=True))


def _ramp(value: float, low: float, high: float) -> float:
    return min(1.0, max(0.0, (value - low) / (high - low)))


def glow_level(r: int, g: int, b: int, brightest: int) -> float:
    """Brightness of the colour a glow pixel fades from, judged from its hue."""
    cyan = _ramp(g / b if b else 0.0, 0.45, 0.8)  # Green share: navy ~0.41, cyan ~0.85
    white = _ramp(r / brightest, 0.15, 0.8)  # Red share: navy and cyan ~0, white glow ~0.8
    level = NAVY_LEVEL + (CYAN_LEVEL - NAVY_LEVEL) * cyan
    return level + (WHITE_GLOW_LEVEL - level) * white


def make_transparent(artwork: QImage) -> QImage:
    image = artwork.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    pointer = image.bits()
    pointer.setsize(image.sizeInBytes())
    pixels = memoryview(pointer).cast('B')  # B, G, R, A per pixel (little endian ARGB32)
    stride = image.bytesPerLine()

    def offset(x: int, y: int) -> int:
        return y * stride + 4 * x

    # Artwork (opaque) and opacity of the glow
    core = [[False] * width for _ in range(height)]
    shading = [[False] * width for _ in range(height)]  # Almost opaque navy
    alpha = [[0.0] * width for _ in range(height)]
    for y in range(height):
        core_row, alpha_row = core[y], alpha[y]
        for x in range(width):
            i = offset(x, y)
            b, g, r = pixels[i], pixels[i + 1], pixels[i + 2]
            if min(r, g, b) >= WHITE_LEVEL or _distance((r, g, b), NAVY) <= NAVY_TOLERANCE:
                core_row[x] = True
                continue
            brightest = max(r, g, b)
            if brightest == 0:
                continue
            level = glow_level(r, g, b, brightest)
            alpha_row[x] = min(1.0, brightest / level)
            if level == NAVY_LEVEL and alpha_row[x] >= ENCLOSING_ALPHA:
                shading[y][x] = True

    # Dark navy areas: flat seeds grown over the dark navy pixels around them
    def rgb(x: int, y: int) -> tuple:
        i = offset(x, y)
        return pixels[i + 2], pixels[i + 1], pixels[i]

    d = FLAT_DISTANCE
    grow = deque()
    for y in range(d, height - d):
        for x in range(d, width - d):
            if all(
                _distance(rgb(px, py), DARK_NAVY) <= FLAT_TOLERANCE
                for px, py in ((x, y), (x - d, y), (x + d, y), (x, y - d), (x, y + d))
            ):
                grow.append((x, y, 0))
    while grow:
        x, y, steps = grow.popleft()
        if core[y][x] or steps > d or not (0 <= x < width and 0 <= y < height):
            continue
        if steps and _distance(rgb(x, y), DARK_NAVY) > DARK_TOLERANCE:
            continue
        core[y][x] = True
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                grow.append((nx, ny, steps + 1))

    # Pixels reachable from the border without crossing the opaque head are outside; the rest is enclosed
    outside = [[False] * width for _ in range(height)]
    queue = deque()
    for x in range(width):
        queue.extend(((x, 0), (x, height - 1)))
    for y in range(height):
        queue.extend(((0, y), (width - 1, y)))
    while queue:
        x, y = queue.popleft()
        if outside[y][x] or core[y][x] or shading[y][x]:
            continue
        outside[y][x] = True
        if x > 0:
            queue.append((x - 1, y))
        if x < width - 1:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y < height - 1:
            queue.append((x, y + 1))

    for y in range(height):
        for x in range(width):
            i = offset(x, y)
            a = alpha[y][x] if outside[y][x] and not core[y][x] else 1.0
            if 0.0 < a < 1.0:  # The colour seen over black was colour * alpha
                for channel in range(3):
                    pixels[i + channel] = min(255, round(pixels[i + channel] / a))
                a **= GLOW_FADE
            pixels[i + 3] = round(a * 255)
    return image


def content_rect(image: QImage) -> QRect:
    """Bounding box of the pixels at least CROP_ALPHA opaque."""
    level = round(CROP_ALPHA * 255)
    xs, ys = [], []
    step = 2
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            if (image.pixel(x, y) >> 24) >= level:
                xs.append(x)
                ys.append(y)
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def square(logo: QImage, content: QRect, size: int) -> QImage:
    """The content centred in a transparent square."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    side = max(content.width(), content.height()) / (1 - 2 * MARGIN)
    source = QRectF(content.center().x() - side / 2, content.center().y() - side / 2, side, side)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(0, 0, size, size), logo, source)
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
    QGuiApplication([])
    artwork = QImage(str(ARTWORK))
    if artwork.isNull():
        raise SystemExit(f'Could not read {ARTWORK}')

    logo = make_transparent(artwork)
    save(logo, TRANSPARENT)
    content = content_rect(logo)
    sizes = sorted({512, *ICO_SIZES, *(size for _, size in ICNS_TYPES), *DESKTOP_SIZES})
    icons = {size: square(logo, content, size) for size in sizes}

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
    banner = logo.copy(content).scaledToWidth(720, Qt.TransformationMode.SmoothTransformation)
    save(banner, ASSETS / 'logo-banner.png')


if __name__ == '__main__':
    main()
