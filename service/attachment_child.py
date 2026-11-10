"""Turn one untrusted file's bytes into something Wisp can read. Runs in a CHILD process.

service/documents.run_child starts `python -I -B` on `main()` here, feeds the
bytes on stdin and reads one JSON line back. The file came from someone else
(an email attachment) or is a very large picture, and image decoders and ZIP
readers are exactly where a hostile file does its damage, so none of this runs
in the service. The parent kills this process at its deadline or memory limit.

Five jobs, picked by argv[1]:
- `tiles`: a picture as JPEG data URLs, an overview and, when it is large and
  looks like a screen or a page, up to MAX_TILES zoomed sections.
- `pdf_pages`: the page images of a scanned PDF, which has no text to extract.
- `inspect`: what a file really is, without opening it as anything, and a plain risk verdict.
- `structured`: an invite (.ics) or contact card (.vcf) read as plain data. Pure Python, never executed.
- `ocr_images`: a picture, or the page pictures of a scanned PDF, re-encoded as JPEGs for text recognition.

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
import time
import warnings
from pathlib import Path, PurePath
from zipfile import BadZipFile, ZipFile

from service.document_extract import _limit_self, is_locked_office

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
# Pictures for text recognition: big enough for small print, small enough to send on.
OCR_MAX_SIDE = 3000
# Recognition misreads small text (measured: 22 px letters on a 3024 px screen, 12 of 15 rows right;
# the same screen doubled, 15 of 15). So a picture that is mostly text is enlarged first.
OCR_TEXT_SIDE = 6000            # a single picture of text is enlarged up to this long side
OCR_PAGE_TEXT_SIDE = 3600       # a scanned page, of which there can be forty
OCR_MAX_ENLARGE = 3.0
PDF_UNSUPPORTED_CIPHER = "That PDF is encrypted in a way Wisp can't open."
ENCRYPTED_PDF_REASON = "The PDF is encrypted, so its contents can't be checked."
OCR_QUALITIES = (88, 82, 76, 70, 64, 60)
OCR_MAX_BYTES = 3 * 1024 * 1024
MAX_OCR_PAGES = 40
# The parent kills the child at 60 s (less when the request is nearly spent) and then gets nothing back,
# so ocr_images stops preparing pages itself well before that and returns what it has.
OCR_TIME_BUDGET_S = 35.0
OCR_MIN_PICTURE_SIDE = 300
OCR_MAX_TOTAL_PIXELS = 400_000_000
OCR_MAX_TOTAL_BYTES = 48 * 1024 * 1024
_SIPS = "/usr/bin/sips"
_SIPS_TIMEOUT_S = 20.0
_SIPS_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}

MAX_ZIP_NAMES = 40
MAX_URLS = 8
_URL_SCAN_BYTES = 2 * 1024 * 1024
_URL = re.compile(rb"https?://[!-~]{4,200}")

_OLD_OFFICE_LABEL = "an old Office file"
# A docx, xlsx or pptx saved with a password: the same container as an old Office file, holding
# the encrypted document (document_extract.is_locked_office).
_LOCKED_OFFICE_LABEL = "a password-protected Office file"
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
    (b"\xd0\xcf\x11\xe0", _OLD_OFFICE_LABEL, "document"),
    (b"\x89PNG\r\n\x1a\n", "a PNG image", "image"),
    (b"\xff\xd8\xff", "a JPEG image", "image"),
    (b"GIF8", "a GIF image", "image"),
    (b"BM", "a BMP image", "image"),
    (b"II*\x00", "a TIFF image", "image"),
    (b"MM\x00*", "a TIFF image", "image"),
)
_RISKY_SUFFIXES = frozenset({
    "action", "app", "applescript", "bat", "bin", "cmd", "com", "command", "cpl", "desktop", "dll", "dmg",
    "dylib", "exe", "hta", "img", "inetloc", "iso", "jar", "jnlp", "js", "jse", "lnk", "mpkg", "msi", "pif",
    "pkg", "ps1", "reg", "scpt", "scr", "sh", "so", "terminal", "tool", "url", "vbe", "vbs", "vhd", "webloc",
    "workflow", "wsf", "zsh",
})
# Shortcut files open a program or an address the moment they are opened.
_SHORTCUT_SUFFIXES = frozenset({"desktop", "inetloc", "lnk", "url", "webloc"})
_MACRO_SUFFIXES = frozenset({"docm", "dotm", "potm", "pptm", "ppsm", "xlam", "xlsm", "xltm"})
# Types that have no signature to check, so unknown bytes under them mean nothing.
_PLAIN_TEXT_SUFFIXES = frozenset({"txt", "csv", "md", "json", "html", "htm", "ics", "vcf", "rtf", "log", "xml"})
_HTML_SUFFIXES = frozenset({"html", "htm", "xhtml", "shtml"})
_HTML_SCAN_BYTES = 512 * 1024
_HTML_START = re.compile(rb"\A\s*(?:<!doctype\s+html|<html|<head|<body)", re.I)
_PASSWORD_FIELD = re.compile(rb"<input\b[^>]{0,400}?\btype\s*=\s*[\"']?\s*password", re.I)
_FORM = re.compile(rb"<form\b", re.I)
_OLE_VBA = "_VBA_PROJECT".encode("utf-16-le")
MAX_RISK_REASONS = 5
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

def _is_windows_program(data: bytes) -> bool:
    """Whether bytes that start with "MZ" are a program and not text that happens to start so.

    A Windows program names where its PE header is at byte 0x3C, and "PE\\0\\0" is there. An older
    DOS or 16-bit program has no PE header, but its header is binary, so it has a zero byte early
    on, which text does not. Anything else that starts "MZ" is left to be read as what its name says.
    """
    if len(data) >= 64:
        at = int.from_bytes(data[0x3C:0x40], "little")
        if 0 < at <= len(data) - 4 and data[at:at + 4] == b"PE\x00\x00":
            return True
    return b"\x00" in data[:8192]


def _is_bmp(data: bytes) -> bool:
    """Whether bytes that start with "BM" carry a BMP header: one of the known header sizes at byte 14."""
    return len(data) >= 26 and int.from_bytes(data[14:18], "little") in (12, 40, 52, 56, 64, 108, 124)


def _signature(data: bytes) -> tuple[str, str]:
    """(what the bytes say this is, its family), or ('', '') when they say nothing."""
    for magic, label, family in _SIGNATURES:
        if data.startswith(magic):
            # Two letters are not enough: a list of part numbers or a name can start with them.
            if (magic == b"MZ" and not _is_windows_program(data)) or (magic == b"BM" and not _is_bmp(data)):
                continue
            if label == _OLD_OFFICE_LABEL and is_locked_office(data):
                return _LOCKED_OFFICE_LABEL, family
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


def _looks_like_html(suffix: str, data: bytes) -> bool:
    return suffix in _HTML_SUFFIXES or bool(_HTML_START.match(data[:2048].lstrip(b"\xef\xbb\xbf")))


def _risk(name: str, data: bytes, suffix: str, label: str, family: str, found: dict) -> dict:
    """A plain verdict on the file. Reasons are fixed sentences; nothing from the file is quoted
    except a name suffix that was matched against a known set first."""
    high: list[str] = []
    caution: list[str] = []
    parts = [part.strip() for part in PurePath((name or "").rstrip(" .")).name.lower().split(".")]
    if family == "program":
        high.append(f"The file itself is {label}, which could run on your Mac or PC.")
    elif family == "script" and suffix not in _PLAIN_TEXT_SUFFIXES:
        high.append("The file itself is a script, which could run commands if opened.")
    if suffix in _SHORTCUT_SUFFIXES:
        high.append(f"It is a .{suffix} shortcut, which can open a program or website as soon as it is opened.")
    elif suffix in _RISKY_SUFFIXES:
        high.append(f"It is a .{suffix} file, a kind of program or script that runs when it is opened.")
    if _DECEPTIVE.search(name or ""):
        high.append("Its name has hidden characters that make it read differently from what it is.")
    if len(parts) >= 3 and parts[-1] in _INNOCENT_SUFFIXES and any(p in _RISKY_SUFFIXES for p in parts[1:-1]):
        high.append("Its name has a program or script type tucked inside it, ahead of a harmless-looking ending.")
    elif len(parts) >= 3 and parts[-1] in _RISKY_SUFFIXES and parts[-2] in _INNOCENT_SUFFIXES:
        high.append(f"Its name is dressed up as a .{parts[-2]} file, but it really ends in .{parts[-1]}.")
    if family in ("program", "script") and suffix in _INNOCENT_SUFFIXES and suffix not in _PLAIN_TEXT_SUFFIXES:
        high.append(f"Its name says .{suffix}, but its contents are not a document or picture.")
    if family == "zip" and found.get("risky_entries"):
        high.append("The archive lists programs or scripts inside it.")
    if found.get("macros") or suffix in _MACRO_SUFFIXES or (
            label in (_OLD_OFFICE_LABEL, _LOCKED_OFFICE_LABEL) and _OLE_VBA in data):
        caution.append("It is an Office file that contains macros, which can run commands if you enable them.")
    if found.get("encrypted"):
        caution.append("The archive is password-protected, so its contents can't be checked.")
    if label == _LOCKED_OFFICE_LABEL:
        caution.append("It is password-protected, so its contents can't be checked.")
    if label == "a PDF" and (b"/Encrypt" in data[-8192:] or b"/Encrypt" in data[:2048]):
        caution.append(ENCRYPTED_PDF_REASON)
    if _looks_like_html(suffix, data):
        page = data[:_HTML_SCAN_BYTES]
        if _PASSWORD_FIELD.search(page):
            caution.append("It is a web page that asks for a password, which is how fake sign-in pages work.")
        if _FORM.search(page):
            caution.append("It is a web page with a form, which could send what you type to someone else.")
    if (not label and data and suffix in _INNOCENT_SUFFIXES and suffix not in _PLAIN_TEXT_SUFFIXES
            and not data.lstrip().startswith(b"{\\rtf")):
        caution.append(f"Its name says .{suffix}, but its contents don't match any file type Wisp knows.")
    reasons = (high + caution)[:MAX_RISK_REASONS]
    return {"level": "high" if high else "caution" if caution else "", "reasons": reasons}


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
    out["risk"] = _risk(name, data, suffix, label, family, out)
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
        _unlock(reader)
        total = len(reader.pages)
        urls: list[str] = []
        for page in reader.pages[:MAX_PDF_PAGE_IMAGES]:
            try:
                pictures = sorted(page.images, key=lambda item: len(item.data), reverse=True)
                if pictures:
                    urls.append(_jpeg_url(_open_picture(pictures[0].data, "")))
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 — one unreadable page, not an unreadable file
                if _is_dependency_error(error):
                    raise ValueError(PDF_UNSUPPORTED_CIPHER) from None
                continue
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 — a damaged PDF is unreadable, never a crash
        if _is_dependency_error(error):
            raise ValueError(PDF_UNSUPPORTED_CIPHER) from None
        raise ValueError("That PDF couldn't be read. It may be damaged.") from None
    if not urls:
        raise ValueError("That PDF has no pages Wisp can read.")
    return {"images": urls, "pages": total, "shown": len(urls)}


def _is_dependency_error(error: BaseException) -> bool:
    """pypdf's DependencyError (a cipher whose library, `cryptography`, Wisp doesn't ship), by class name."""
    return any(cls.__name__ == "DependencyError" for cls in type(error).__mro__)


def _unlock(reader) -> None:
    """Many PDFs are encrypted only to limit printing or copying and open with no password."""
    if not reader.is_encrypted:
        return
    try:
        opened = bool(reader.decrypt(""))
    except Exception as error:  # noqa: BLE001 — a cipher this Python has no library for, or a broken dictionary
        if _is_dependency_error(error):
            raise ValueError(PDF_UNSUPPORTED_CIPHER) from None
        opened = False
    if not opened:
        raise ValueError("That PDF is password-protected. Wisp can't read it without the password.")


def _recognition_jpeg(image, text_side: int = OCR_TEXT_SIDE) -> str:
    """One RGB JPEG data URL for text recognition, at most OCR_MAX_BYTES encoded, from decoded
    pixels (never an embedded stream passed through). A picture that is mostly text is enlarged
    (up to OCR_MAX_ENLARGE times, longest side at most `text_side`) so small letters are read
    right; any other picture is at most OCR_MAX_SIDE on its longest side."""
    from PIL import Image
    longest = max(image.size)
    side = OCR_MAX_SIDE
    textual = bool(longest) and looks_textual(image)
    if textual:
        side = min(text_side, int(longest * OCR_MAX_ENLARGE))
    while True:
        if textual and side > longest:
            factor = side / longest
            copy = image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))),
                                Image.Resampling.LANCZOS)
        else:
            copy = image.copy()
            copy.thumbnail((side, side), Image.Resampling.LANCZOS)
        rgb = copy.convert("RGB")
        for quality in OCR_QUALITIES:
            out = io.BytesIO()
            rgb.save(out, "JPEG", quality=quality)
            if out.tell() <= OCR_MAX_BYTES:
                return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        if side <= 400:
            raise ValueError("That picture couldn't be read.")
        side = int(side * 0.8)


def _pdf_stream_limits() -> None:
    from pypdf import filters
    # Stream ceilings well below pypdf's 75 MB default; a scanned page is a big raster, so more than text gets.
    for limit in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH",
                  "JBIG2_MAX_OUTPUT_LENGTH", "MAX_DECLARED_STREAM_LENGTH", "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH"):
        if hasattr(filters, limit):
            setattr(filters, limit, PDF_IMAGE_STREAM_BYTES)


def ocr_images(name: str, data: bytes, workdir: str = "") -> dict:
    """{images: [data URL], pages, read_pages: [1-based]} for a picture (one image) or a PDF's page pictures.
    `capped` is set when the page, pixel or size limit (not a blank page) left later pages out."""
    started = time.monotonic()
    if not data.startswith(b"%PDF"):
        picture = _open_picture(data, workdir)
        return {"images": [_recognition_jpeg(picture)], "overview": _jpeg_url(picture), "pages": 1,
                "read_pages": [1]}
    from PIL import Image
    from pypdf import PdfReader
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    _pdf_stream_limits()
    urls: list[str] = []
    read: list[int] = []
    pixels = encoded = attempts = 0
    slowest = 0.0
    stopped_early = False
    capped = False          # a limit, not a blank page, left later pages unprepared
    try:
        reader = PdfReader(io.BytesIO(data))
        _unlock(reader)
        total = len(reader.pages)
        capped = total > MAX_OCR_PAGES
        for number, page in enumerate(reader.pages[:MAX_OCR_PAGES], start=1):
            now = time.monotonic()
            # Another page of the slowest kind so far must still fit; the first page is always tried.
            if attempts and now - started + slowest > OCR_TIME_BUDGET_S:
                stopped_early = True
                break
            attempts += 1
            try:
                pictures = sorted(page.images, key=lambda item: len(item.data), reverse=True)
                if not pictures:
                    continue
                try:
                    width, height = Image.open(io.BytesIO(pictures[0].data)).size
                except Exception:  # noqa: BLE001 — size unknown: _open_picture decides whether it can be read
                    width = height = MAX_WORKING_SIDE
                if max(width, height) < OCR_MIN_PICTURE_SIDE:
                    continue
                if pixels + width * height > OCR_MAX_TOTAL_PIXELS:
                    capped = True
                    break
                url = _recognition_jpeg(_open_picture(pictures[0].data, ""), OCR_PAGE_TEXT_SIDE)
            except Exception as error:  # noqa: BLE001 — one unreadable or oversized page, not an unreadable file
                if _is_dependency_error(error):
                    raise ValueError(PDF_UNSUPPORTED_CIPHER) from None
                continue
            finally:
                slowest = max(slowest, time.monotonic() - now)
            if encoded + len(url) > OCR_MAX_TOTAL_BYTES:
                capped = True
                break
            pixels += width * height
            encoded += len(url)
            urls.append(url)
            read.append(number)
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 — a damaged PDF is unreadable, never a crash
        if _is_dependency_error(error):
            raise ValueError(PDF_UNSUPPORTED_CIPHER) from None
        raise ValueError("That PDF couldn't be read. It may be damaged.") from None
    if not urls:
        raise ValueError("That PDF has no pages Wisp can read.")
    result = {"images": urls, "pages": total, "read_pages": read}
    if stopped_early:
        result["stopped_early"] = True
    if capped:
        result["capped"] = True
    return result


# --- invites and contact cards ------------------------------------------------------

MAX_STRUCTURED_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 20
MAX_ATTENDEES = 12
MAX_CONTACTS = 20
MAX_CONTACT_ITEMS = 6
# A 2 MiB file of one-character lines is a million of them; no real invite or card comes near these.
MAX_LOGICAL_LINES = 100_000
MAX_PROPERTIES = 50_000
STRUCTURED_TOO_BIG = "That file is larger than Wisp reads as a calendar or contact file."
_DAYS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday",
         "SU": "Sunday"}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?(Z)?$")


def _unfold(text: str) -> list[str]:
    """Logical lines: a line that starts with a space or tab continues the one before it, and an old
    vCard's quoted-printable value continues across a line that ends with '='. Linear in the text:
    the pieces of a logical line are collected in a list and joined once."""
    groups: list[list[str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and groups:
            groups[-1].append(raw[1:])
        else:
            groups.append([raw])
    merged: list[str] = []
    index = 0
    while index < len(groups):
        pieces = groups[index]
        index += 1
        first = "".join(pieces)
        if first.endswith("=") and "QUOTED-PRINTABLE" in first.partition(":")[0].upper():
            parts = [first[:-1]]
            while index < len(groups):
                nxt = "".join(groups[index])
                index += 1
                if nxt.endswith("="):
                    parts.append(nxt[:-1])
                else:
                    parts.append(nxt)
                    break
            first = "".join(parts)
        merged.append(first)
        if len(merged) > MAX_LOGICAL_LINES:
            raise ValueError(STRUCTURED_TOO_BIG)
    return merged


def _split_outside_quotes(text: str, separator: str, limit: int = -1) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted and limit != 0:
            parts.append("".join(current))
            current = []
            limit -= 1
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value, flags=re.S)


def _split_unescaped(value: str, separator: str) -> list[str]:
    parts = re.split(r"(?<!\\)" + re.escape(separator), value)
    return [_unescape(part) for part in parts]


def _parse_line(line: str):
    """(NAME, {PARAM: value}, raw value) or None for a line that is not a property."""
    head = _split_outside_quotes(line, ":", 1)
    if len(head) < 2:
        return None
    pieces = _split_outside_quotes(head[0], ";")
    name = pieces[0].strip().upper().rsplit(".", 1)[-1]
    if not name or not re.fullmatch(r"[A-Z0-9-]{1,40}", name):
        return None
    collected: dict[str, list[str]] = {}
    for piece in pieces[1:]:
        key, sep, value = piece.partition("=")
        if not sep:  # vCard 2.1 writes TEL;CELL;VOICE:
            key, value = "TYPE", key
        collected.setdefault(key.strip().upper(), []).append(value.strip().strip('"'))
    params = {key: ",".join(values) for key, values in collected.items()}
    value = head[1]
    if "QUOTED-PRINTABLE" in params.get("ENCODING", "").upper():
        import quopri
        charset = params.get("CHARSET", "utf-8")
        try:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except (LookupError, ValueError):
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
    return name, params, value


def _components(lines: list[str], wanted: str):
    """(blocks of (name, params, value) for the first MAX of `wanted`, the true count of them, the METHOD)."""
    blocks: list[list] = []
    count = 0
    method = ""
    stack: list[str] = []
    seen = 0
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            continue
        seen += 1
        if seen > MAX_PROPERTIES:
            raise ValueError(STRUCTURED_TOO_BIG)
        name, params, value = parsed
        upper = value.strip().upper()
        if name == "BEGIN":
            stack.append(upper)
            if upper == wanted:
                count += 1
                if count <= MAX_EVENTS:
                    blocks.append([])
        elif name == "END":
            if stack:
                stack.pop()
        elif stack and stack[-1] == wanted and count <= MAX_EVENTS:
            blocks[-1].append((name, params, value))
        elif name == "METHOD" and stack == ["VCALENDAR"]:
            method = _pretty(value, 20).upper()
    return blocks, count, method


def _first(props, name):
    for prop in props:
        if prop[0] == name:
            return prop
    return None


def _when(prop):
    """(text as written, all-day, timezone) for a DTSTART/DTEND property, or ('', False, '')."""
    if prop is None:
        return "", False, ""
    _, params, value = prop
    value = value.strip()
    match = _DATE.match(value)
    if not match:
        return _pretty(value, 40), False, ""
    year, month, day, hour, minute, _second, zulu = match.groups()
    all_day = hour is None or params.get("VALUE", "").upper() == "DATE"
    text = f"{year}-{month}-{day}" if all_day else f"{year}-{month}-{day} {hour}:{minute}"
    zone = "" if all_day else "UTC" if zulu else _pretty(params.get("TZID", ""), 60)
    return text, all_day, zone


def _person(prop) -> str:
    if prop is None:
        return ""
    _, params, value = prop
    who = params.get("CN", "").strip()
    if not who:
        who = re.sub(r"^mailto:", "", _unescape(value).strip(), flags=re.I)
    return _pretty(who, 120)


def _plural(count: int, unit: str) -> str:
    return unit if count == 1 else f"{count} {unit}s"


def _ordinal(number: int) -> str:
    if number < 0:
        return "last" if number == -1 else f"{_ordinal(-number)} from last"
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _repeats(rule: str) -> str:
    """A common RRULE said plainly; just 'repeats' when it can't be."""
    fields: dict[str, str] = {}
    for part in rule.upper().split(";"):
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(fields.get("FREQ", ""))
    if not unit or not set(fields) <= {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "UNTIL", "COUNT", "WKST"}:
        return "repeats"
    try:
        interval = int(fields.get("INTERVAL", "1"))
        if not 1 <= interval <= 999:
            return "repeats"
        text = "repeats every " + _plural(interval, unit)
        if "BYDAY" in fields:
            days = []
            for item in fields["BYDAY"].split(","):
                match = re.fullmatch(r"([+-]?\d{1,2})?([A-Z]{2})", item.strip())
                if not match or match.group(2) not in _DAYS:
                    return "repeats"
                day = _DAYS[match.group(2)]
                days.append(f"the {_ordinal(int(match.group(1)))} {day}" if match.group(1) else day)
            text += " on " + ", ".join(days)
        if "BYMONTHDAY" in fields:
            numbers = [int(n) for n in fields["BYMONTHDAY"].split(",")]
            text += " on the " + ", ".join(_ordinal(n) for n in numbers)
        if "BYMONTH" in fields:
            months = [_MONTHS[int(n) - 1] for n in fields["BYMONTH"].split(",")]
            text += " in " + ", ".join(months)
        if "COUNT" in fields:
            count = int(fields["COUNT"])
            text += f", {_plural(count, 'time')} in all"
        elif "UNTIL" in fields:
            match = _DATE.match(fields["UNTIL"])
            if not match:
                return "repeats"
            text += f", until {match.group(1)}-{match.group(2)}-{match.group(3)}"
    except (ValueError, IndexError):
        return "repeats"
    return text


def _last_day(text: str) -> str:
    """An all-day end is written as the day AFTER the last day; say the last day itself."""
    from datetime import date, timedelta
    try:
        return (date.fromisoformat(text) - timedelta(days=1)).isoformat()
    except ValueError:
        return text


def _event(props) -> dict:
    start, all_day, zone = _when(_first(props, "DTSTART"))
    end, end_all_day, _ = _when(_first(props, "DTEND"))
    if end and all_day and end_all_day and end > start:
        end = _last_day(end)
    attendees = [_person(p) for p in props if p[0] == "ATTENDEE"]
    status = _pretty(_first(props, "STATUS")[2], 20).upper() if _first(props, "STATUS") else ""
    rule = _first(props, "RRULE")
    return {
        "summary": _pretty(_unescape(_first(props, "SUMMARY")[2]), 200) if _first(props, "SUMMARY") else "",
        "start": start, "end": end, "all_day": all_day, "timezone": zone,
        "location": _pretty(_unescape(_first(props, "LOCATION")[2]), 200) if _first(props, "LOCATION") else "",
        "organizer": _person(_first(props, "ORGANIZER")),
        "attendees": [a for a in attendees if a][:MAX_ATTENDEES], "attendee_count": len(attendees),
        "repeats": _repeats(rule[2]) if rule else "",
        "status": status if status in ("CANCELLED", "CONFIRMED", "TENTATIVE") else "",
        "description": (_pretty(_unescape(_first(props, "DESCRIPTION")[2]), 600)
                        if _first(props, "DESCRIPTION") else ""),
    }


def _type_label(params: dict) -> str:
    skip = {"VOICE", "PREF", "INTERNET", "X-INTERNET", "PREFERRED"}
    for kind in params.get("TYPE", "").split(","):
        kind = kind.strip().lower()
        if kind and kind.upper() not in skip and re.fullmatch(r"[a-z0-9-]{1,20}", kind):
            return kind
    return ""


def _contact(props) -> dict:
    full = _first(props, "FN")
    name = _pretty(_unescape(full[2]), 120) if full else ""
    if not name:
        n = _first(props, "N")
        if n:
            family, given, middle, prefix, suffix = (_split_unescaped(n[2], ";") + [""] * 5)[:5]
            name = _pretty(" ".join(x.strip() for x in (prefix, given, middle, family, suffix) if x.strip()), 120)
    org = _first(props, "ORG")
    title = _first(props, "TITLE")
    note = _first(props, "NOTE")
    phones, emails, addresses = [], [], []
    for prop_name, params, value in props:
        if prop_name == "TEL" and len(phones) < MAX_CONTACT_ITEMS:
            number = _pretty(re.sub(r"^tel:", "", _unescape(value).strip(), flags=re.I), 40)
            label = _type_label(params)
            if number:
                phones.append(f"{label}: {number}" if label else number)
        elif prop_name == "EMAIL" and len(emails) < MAX_CONTACT_ITEMS:
            address = _pretty(re.sub(r"^mailto:", "", _unescape(value).strip(), flags=re.I), 120)
            if address:
                emails.append(address)
        elif prop_name == "ADR" and len(addresses) < MAX_CONTACT_ITEMS:
            joined = _pretty(", ".join(p.strip() for p in _split_unescaped(value, ";") if p.strip()), 200)
            if joined:
                addresses.append(joined)
    return {
        "name": name,
        "organization": _pretty(", ".join(p.strip() for p in _split_unescaped(org[2], ";") if p.strip()), 120)
        if org else "",
        "title": _pretty(_unescape(title[2]), 120) if title else "",
        "phones": phones, "emails": emails, "addresses": addresses,
        "note": _pretty(_unescape(note[2]), 300) if note else "",
    }


def structured(name: str, data: bytes) -> dict:
    """An invite or a contact card as plain data. Read as text only: nothing in it is followed or run."""
    if len(data) > MAX_STRUCTURED_BYTES:
        raise ValueError(STRUCTURED_TOO_BIG)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    else:
        text = data.decode("utf-8", "replace")
    lines = _unfold(text.lstrip("﻿"))
    events, event_count, method = _components(lines, "VEVENT")
    if event_count:
        return {"kind": "calendar", "method": method, "events": [_event(props) for props in events],
                "event_count": event_count}
    cards, card_count, _ = _components(lines, "VCARD")
    if card_count:
        return {"kind": "contacts", "contacts": [_contact(props) for props in cards], "contact_count": card_count}
    raise ValueError("That file has no events or contacts Wisp can read.")


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
        elif job == "structured":
            result = structured(name, data)
        elif job == "ocr_images":
            result = ocr_images(name, data, workdir)
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
