"""Turn one untrusted file's bytes into something Wisp can read. Runs in a CHILD process.

service/documents.run_child starts `python -I -B` on `main()` here, feeds the
bytes on stdin and reads one JSON line back. The file came from someone else
(an email attachment) or is a very large picture, and image decoders and ZIP
readers are exactly where a hostile file does its damage, so none of this runs
in the service. The parent kills this process at its deadline or memory limit.

Three jobs, picked by argv[1]:
- `tiles`: a picture as JPEG data URLs, an overview and, when it is large and
  looks like a screen or a page, up to MAX_TILES zoomed sections.
- `pdf_pages`: the page images of a scanned PDF, which has no text to extract.
- `inspect`: what a file really is, without opening it as anything.

Rules:
- IMPORTANT: nothing here ever RUNS a file. A program, script, installer or
  archive is only looked at as bytes: its signature, its name, the names a ZIP
  lists in its directory, and web addresses in its first bytes. An archive is
  never unpacked.
- IMPORTANT: everything this returns is untrusted data for the caller to fence.
- Why tiles: Gemma's vision tower sees about 0.64 megapixels per image whatever
  it is sent (a flat ~280 tokens, service/vision.py), so one shrunk picture of a
  3000 px screen loses its small text. The same tokens spent on a section keep
  the text close to its real size, and several images fit in a single call.
- A photo is left as one image: tiling it costs time and gains nothing, and a
  flat-background test (`looks_textual`) tells the two apart without a model.
- Pillow reads what it can, in memory. HEIC, AVIF and camera RAW go through
  macOS's `sips`, which needs a file: the parent passes a private directory and
  deletes it whether or not this process survives. Bytes for those formats are
  the only thing ever written to disk, and only for the length of this call.
- Bounded: at most MAX_PIXELS decoded, MAX_INPUT_BYTES read, one sips call with
  a timeout, and `sips` is asked for the size before it is asked to decode.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path, PurePath
from zipfile import BadZipFile, ZipFile

from service.document_extract import _limit_self

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 80_000_000
# The app's own limit for one picture before anything is read from it.
OVERVIEW_SIDE = 1568
# What a large picture is normalized to before it is cut up.
MAX_WORKING_SIDE = 4096
JPEG_QUALITY = 82
# Only a picture with more detail than one image can carry gets sections.
TILE_WHEN_SIDE = 1800
TILE_SIDE = 1400
MAX_TILES = 8
TILE_OVERLAP = 0.08
# Share of neighbouring pixels that are nearly equal: screens and pages are mostly
# flat background, photos are not (see looks_textual).
FLAT_SHARE = 0.62
FLAT_DIFF = 3
MAX_PDF_PAGE_IMAGES = 6
PDF_IMAGE_STREAM_BYTES = 64 * 1024 * 1024
_SIPS = "/usr/bin/sips"
_SIPS_TIMEOUT_S = 20.0
_SIPS_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}

MAX_ZIP_NAMES = 40
MAX_URLS = 8
_URL_SCAN_BYTES = 2 * 1024 * 1024
_URL = re.compile(rb"https?://[!-~]{4,200}")

# Longest signatures first where one is a prefix of another.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"MZ", "a Windows program", "program"),
    (b"\x7fELF", "a Linux program", "program"),
    (b"\xcf\xfa\xed\xfe", "a Mac program", "program"),
    (b"\xce\xfa\xed\xfe", "a Mac program", "program"),
    (b"\xfe\xed\xfa\xcf", "a Mac program", "program"),
    (b"\xfe\xed\xfa\xce", "a Mac program", "program"),
    (b"\xca\xfe\xba\xbe", "a Mac or Java program", "program"),
    (b"xar!", "a Mac installer package", "program"),
    (b"#!", "a script", "script"),
    (b"%PDF", "a PDF", "document"),
    (b"PK\x03\x04", "a ZIP-based file", "zip"),
    (b"PK\x05\x06", "an empty ZIP file", "zip"),
    (b"Rar!", "a RAR archive", "archive"),
    (b"7z\xbc\xaf\x27\x1c", "a 7-Zip archive", "archive"),
    (b"\x1f\x8b", "a gzip archive", "archive"),
    (b"\xd0\xcf\x11\xe0", "an old Office file", "document"),
    (b"\x89PNG\r\n\x1a\n", "a PNG image", "image"),
    (b"\xff\xd8\xff", "a JPEG image", "image"),
    (b"GIF8", "a GIF image", "image"),
    (b"BM", "a BMP image", "image"),
    (b"II*\x00", "a TIFF image", "image"),
    (b"MM\x00*", "a TIFF image", "image"),
)
_RISKY_SUFFIXES = frozenset({
    "app", "bat", "bin", "cmd", "com", "command", "cpl", "dll", "dmg", "dylib", "exe", "hta", "iso", "jar",
    "js", "jse", "lnk", "mpkg", "msi", "pif", "pkg", "ps1", "reg", "scpt", "scr", "sh", "so", "tool", "vbe",
    "vbs", "wsf", "zsh",
})
_INNOCENT_SUFFIXES = frozenset({
    "bmp", "csv", "doc", "docx", "gif", "heic", "jpeg", "jpg", "pdf", "png", "ppt", "pptx", "rtf", "tiff",
    "txt", "xls", "xlsx", "zip",
})
# Characters that make a name read differently from what it is.
_DECEPTIVE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\ufeff]")


def _suffix(name: str) -> str:
    # Windows ignores spaces and dots at the end of a name, so "run.exe " is still run.exe.
    return PurePath((name or "").rstrip(" .")).suffix.lower().lstrip(".")


def _pretty(text: str, limit: int = 120) -> str:
    """A file or entry name for display: no control or direction-changing characters."""
    # Direction-changing characters go first: they are unprintable, and would otherwise become a space.
    cleaned = "".join(c if c.isprintable() else " " for c in _DECEPTIVE.sub("", str(text or "")))
    return " ".join(cleaned.split())[:limit]


# --- inspect ----------------------------------------------------------------------

def _signature(data: bytes) -> tuple[str, str]:
    """(what the bytes say this is, its family), or ('', '') when they say nothing."""
    for magic, label, family in _SIGNATURES:
        if data.startswith(magic):
            return label, family
    if len(data) >= 512 and data[-512:-508] == b"koly":
        return "a Mac disk image", "program"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"hevc"):
            return "a HEIC image", "image"
        if brand in (b"avif", b"avis"):
            return "an AVIF image", "image"
        return "a video", "video"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "a WebP image", "image"
    return "", ""


def _zip_listing(data: bytes) -> dict:
    """Names from the ZIP's directory. Nothing is inflated, so size bombs don't matter."""
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except (BadZipFile, ValueError, OSError, RuntimeError, EOFError, NotImplementedError):
        return {"zip_error": True}
    names = [info.filename for info in infos]
    # A bundle shows once, as the bundle, rather than as hundreds of inner files.
    # An Office file's own .bin parts (printer settings, OLE objects) are not a hidden program.
    office = "[Content_Types].xml" in names
    risky = sorted({_pretty(n) for n in names if not n.endswith("/") and _suffix(n) in _RISKY_SUFFIXES
                    and not (office and _suffix(n) == "bin")})
    macros = any(PurePath(n).name.lower() == "vbaproject.bin" for n in names)
    return {"entries": [_pretty(n) for n in names[:MAX_ZIP_NAMES]], "entry_count": len(names),
            "risky_entries": risky[:12], "macros": macros,
            "encrypted": any(info.flag_bits & 0x1 for info in infos)}


def inspect(name: str, data: bytes) -> dict:
    """What this file really is and what looks wrong, from its bytes alone."""
    label, family = _signature(data)
    suffix = _suffix(name)
    out: dict = {"real_type": label, "family": family, "warnings": []}
    if _DECEPTIVE.search(name or ""):
        out["warnings"].append("Its name contains hidden characters that change how it reads.")
    parts = [part.strip() for part in PurePath((name or "").rstrip(" .")).name.lower().split(".")]
    if len(parts) >= 3 and parts[-1] in _RISKY_SUFFIXES and parts[-2] in _INNOCENT_SUFFIXES:
        out["warnings"].append(f"Its name is made to look like a .{parts[-2]}, but it ends in .{parts[-1]}.")
    if family == "program" and suffix in _INNOCENT_SUFFIXES:
        out["warnings"].append(f"Its name says .{suffix}, but it is really {label}.")
    if family == "zip":
        out.update(_zip_listing(data))
        if out.get("macros"):
            out["warnings"].append("It contains macros, which Wisp does not run.")
    if family in ("program", "archive", "") and len(data) > 0 and b"\x00" in data[:8192]:
        found: list[str] = []
        for match in _URL.finditer(data[:_URL_SCAN_BYTES]):
            url = match.group(0).decode("ascii", "replace").rstrip(".,;)'\"")
            if url not in found:
                found.append(url)
            if len(found) >= MAX_URLS:
                break
        if found:
            out["urls"] = found
    return out


# --- pictures ---------------------------------------------------------------------

def plan_tiles(width: int, height: int, *, side: int = TILE_SIDE, max_tiles: int = MAX_TILES,
               overlap: float = TILE_OVERLAP) -> list[tuple[int, int, int, int]]:
    """(left, top, right, bottom) boxes in reading order, covering the whole picture
    and overlapping slightly so a line on a boundary is whole in one of them."""
    columns, rows = max(1, math.ceil(width / side)), max(1, math.ceil(height / side))
    # Over the cap: give up one row or column at a time, from the side with the most,
    # so a 4096 px square becomes 3 x 2 sections rather than a coarse 2 x 2.
    while columns * rows > max_tiles:
        if columns >= rows:
            columns -= 1
        else:
            rows -= 1
    step_x, step_y = width / columns, height / rows
    margin_x, margin_y = int(step_x * overlap), int(step_y * overlap)
    boxes = []
    for row in range(rows):
        for column in range(columns):
            boxes.append((max(0, int(column * step_x) - margin_x), max(0, int(row * step_y) - margin_y),
                          min(width, int((column + 1) * step_x) + margin_x),
                          min(height, int((row + 1) * step_y) + margin_y)))
    return boxes


def looks_textual(image) -> bool:
    """Whether this is mostly flat background, as a screen, page or chart is."""
    from PIL import Image
    small = image.convert("L").resize((256, 256), Image.Resampling.NEAREST)
    pixels = small.tobytes()
    flat = total = 0
    for row in range(256):
        start = row * 256
        for column in range(255):
            total += 1
            if abs(pixels[start + column] - pixels[start + column + 1]) <= FLAT_DIFF:
                flat += 1
    return flat / max(1, total) >= FLAT_SHARE


def _jpeg_url(image, max_side: int = OVERVIEW_SIDE) -> str:
    from PIL import Image
    copy = image.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    copy.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=False)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _through_sips(data: bytes, workdir: str):
    """A format Pillow can't read, converted by macOS. ValueError when it can't be."""
    from PIL import Image
    if not workdir or not os.path.isdir(workdir):
        raise ValueError("Wisp can't read that kind of picture here.")
    source, target = Path(workdir, "input"), Path(workdir, "output.jpg")
    source.write_bytes(data)
    try:
        size = subprocess.run([_SIPS, "-g", "pixelWidth", "-g", "pixelHeight", str(source)], capture_output=True,
                              timeout=_SIPS_TIMEOUT_S, env=_SIPS_ENV)
        values = dict(re.findall(rb"pixel(Width|Height):\s*(\d+)", size.stdout))
        width, height = int(values.get(b"Width", 0)), int(values.get(b"Height", 0))
        if width < 1 or height < 1:
            raise ValueError("That picture couldn't be read.")
        if width * height > MAX_PIXELS * 2:
            raise ValueError("That picture is too large for Wisp to read.")
        done = subprocess.run([_SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "90", "-Z",
                               str(MAX_WORKING_SIDE), str(source), "--out", str(target)], capture_output=True,
                              timeout=_SIPS_TIMEOUT_S, env=_SIPS_ENV)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("That picture couldn't be read.") from None
    if done.returncode != 0 or not target.is_file():
        raise ValueError("That picture couldn't be read.")
    image = Image.open(io.BytesIO(target.read_bytes()))
    image.load()
    return image


def _open_picture(data: bytes, workdir: str):
    from PIL import Image, ImageOps, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data))
            # Only the first frame of a GIF or multi-page TIFF: the first frame is the picture.
            if image.format == "JPEG":
                image.draft("RGB", (MAX_WORKING_SIDE, MAX_WORKING_SIDE))
            image.load()
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ValueError("That picture is too large for Wisp to read.") from None
        except UnidentifiedImageError:
            image = _through_sips(data, workdir)
        except (OSError, SyntaxError, ValueError, EOFError):
            raise ValueError("That picture couldn't be read. It may be damaged.") from None
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_WORKING_SIDE, MAX_WORKING_SIDE), Image.Resampling.LANCZOS)
    return image


def tiles(data: bytes, workdir: str = "") -> dict:
    """{width, height, images: [overview, section, ...], tiled}."""
    image = _open_picture(data, workdir)
    width, height = image.size
    images = [_jpeg_url(image)]
    large = max(width, height) > TILE_WHEN_SIDE
    if large and looks_textual(image):
        for box in plan_tiles(width, height):
            images.append(_jpeg_url(image.crop(box)))
    return {"width": width, "height": height, "images": images, "tiled": len(images) > 1}


def pdf_pages(data: bytes) -> dict:
    """The largest image on each of the first pages of a scanned PDF, as data URLs."""
    from PIL import Image
    from pypdf import PdfReader, filters
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    # Stream ceilings well below pypdf's 75 MB default; a scanned page is a big raster, so more than text gets.
    for limit in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH",
                  "JBIG2_MAX_OUTPUT_LENGTH", "MAX_DECLARED_STREAM_LENGTH", "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH"):
        if hasattr(filters, limit):
            setattr(filters, limit, PDF_IMAGE_STREAM_BYTES)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("That PDF is password-protected. Wisp can't read it without the password.")
        total = len(reader.pages)
        urls: list[str] = []
        for page in reader.pages[:MAX_PDF_PAGE_IMAGES]:
            try:
                pictures = sorted(page.images, key=lambda item: len(item.data), reverse=True)
                if pictures:
                    urls.append(_jpeg_url(_open_picture(pictures[0].data, "")))
            except ValueError:
                raise
            except Exception:  # noqa: BLE001 — one unreadable page, not an unreadable file
                continue
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — a damaged PDF is unreadable, never a crash
        raise ValueError("That PDF couldn't be read. It may be damaged.") from None
    if not urls:
        raise ValueError("That PDF has no pages Wisp can read.")
    return {"images": urls, "pages": total, "shown": len(urls)}


def main() -> None:
    """Child entry: argv = job, file name, private work directory. One JSON line out."""
    _limit_self()
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    workdir = sys.argv[3] if len(sys.argv) > 3 else ""
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("That file is too large for Wisp to read.")
        if job == "inspect":
            result = inspect(name, data)
        elif job == "tiles":
            result = tiles(data, workdir)
        elif job == "pdf_pages":
            result = pdf_pages(data)
        else:
            raise ValueError("Unknown job.")
    except ValueError as error:
        result = {"error": str(error) or "That file couldn't be read."}
    except MemoryError:
        result = {"error": "That file is too large for Wisp to read."}
    except Exception:  # noqa: BLE001 — a parser's surprise is an unreadable file, never a traceback
        result = {"error": "That file couldn't be read. It may be damaged."}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
