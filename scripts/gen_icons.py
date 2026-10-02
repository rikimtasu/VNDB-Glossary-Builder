"""Generate minimal Tauri icons (PNG + ICO) with stdlib only (plus PIL if present)."""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
ROOT.mkdir(parents=True, exist_ok=True)

SIZE_BG = (47, 111, 237)   # #2f6fed
SIZE_FG = (255, 255, 255)


def draw(size: int) -> bytearray:
    px = bytearray()
    for y in range(size):
        for x in range(size):
            # white "V" band on blue background
            u = abs(x - size / 2) / (size / 2)
            v = y / size
            on_v = abs(u - (0.55 - v * 0.42)) < 0.13 and 0.12 < v < 0.88
            c = SIZE_FG if on_v else SIZE_BG
            px += bytes(c)
    return px


def png_bytes(size: int) -> bytes:
    raw = b"".join(b"\x00" + draw(size)[i * size * 3:(i + 1) * size * 3] for i in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def ico_bytes(png32: bytes) -> bytes:
    # Single-image ICO with a PNG payload (Vista+ compatible).
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png32), 6 + 16)
    return header + entry + png32


p32 = png_bytes(32)
p128 = png_bytes(128)
(ROOT / "32x32.png").write_bytes(p32)
(ROOT / "128x128.png").write_bytes(p128)
(ROOT / "128x128@2x.png").write_bytes(png_bytes(256))
(ROOT / "icon.ico").write_bytes(ico_bytes(p32))
print("icons written to", ROOT)
