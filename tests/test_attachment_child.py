"""service/attachment_child.py: pictures cut into sections, scanned PDF pages, and what a file really is.

Synthetic bytes only. Nothing here opens a real attachment, loads a model or touches Mail.
The child-process tests run the real child through service/documents.run_attachment_child.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import struct
import tempfile
import time
import zipfile
import zlib
from unittest.mock import patch

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-attachment-child-"))

import pytest
from PIL import Image, ImageDraw, ImageFilter

from service import attachment_child as A
from service import documents as D


def run(coro):
    return asyncio.run(coro)


def encoded(image, fmt, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **kwargs)
    return out.getvalue()


def screen(width=2400, height=1500):
    """A flat white page with lines of text: what a screenshot is like."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for row in range(0, height - 30, 28):
        draw.text((20, row), f"Line {row}: the quarterly figures are listed below", fill="black")
    return image


def noise(width=1800, height=1200):
    """Something with detail everywhere, as a photograph has."""
    return Image.effect_noise((width, height), 60).convert("RGB")


def png_header_only(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims a huge bitmap."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * (width + 1))) + chunk(b"IEND", b""))


def zipped(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def image_of(url: str):
    assert url.startswith("data:image/jpeg;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


# --------------------------------------------------------------------- plan_tiles

def _covers(boxes, width, height) -> bool:
    """Boxes are a grid, so the picture is covered when both axes' intervals are."""
    for axis, size in ((0, width), (1, height)):
        spans = sorted({(b[axis], b[axis + 2]) for b in boxes})
        reach = 0
        for start, end in spans:
            if start > reach:
                return False
            reach = max(reach, end)
        if reach < size or spans[0][0] != 0:
            return False
    return True


SIZES = [(1, 1), (1, 4096), (4096, 1), (10, 4096), (4096, 10), (1400, 1400), (1401, 1400), (1400, 1401),
         (1800, 1801), (2800, 2800), (4096, 2560), (4096, 4096), (4096, 100), (5000, 5000), (30000, 30000),
         (30000, 10), (3, 90000)]


@pytest.mark.parametrize("width,height", SIZES)
def test_plan_tiles_covers_every_pixel_and_never_exceeds_the_cap(width, height):
    boxes = A.plan_tiles(width, height)
    assert 1 <= len(boxes) <= A.MAX_TILES
    assert _covers(boxes, width, height)
    for left, top, right, bottom in boxes:
        assert 0 <= left < right <= width and 0 <= top < bottom <= height


def test_plan_tiles_holds_for_random_sizes_and_other_caps():
    rng = random.Random(7)
    for _ in range(400):
        width, height = rng.randint(1, 6000), rng.randint(1, 6000)
        cap = rng.randint(1, 9)
        boxes = A.plan_tiles(width, height, max_tiles=cap)
        assert len(boxes) <= cap and _covers(boxes, width, height), (width, height, cap)


def test_plan_tiles_is_in_reading_order_and_neighbours_overlap():
    boxes = A.plan_tiles(2800, 1400, max_tiles=8)
    assert boxes == sorted(boxes, key=lambda b: (b[1], b[0]))
    assert len(boxes) == 2 and boxes[1][0] < boxes[0][2]     # overlap, so a line on the seam is whole in one


def test_a_picture_that_fits_one_section_gets_one_box():
    assert A.plan_tiles(900, 700) == [(0, 0, 900, 700)]


# ------------------------------------------------------------------ looks_textual

def test_a_screen_is_textual_and_a_photo_is_not():
    assert A.looks_textual(screen())
    assert not A.looks_textual(noise())
    # Smoothed detail is still detail: the neighbouring pixels differ.
    assert not A.looks_textual(Image.effect_noise((1200, 1200), 80).filter(ImageFilter.GaussianBlur(2)).convert("RGB"))


def test_a_plain_chart_on_white_is_textual():
    chart = Image.new("RGB", (2000, 1000), "white")
    draw = ImageDraw.Draw(chart)
    for i in range(10):
        draw.rectangle((100 + i * 150, 900 - i * 70, 200 + i * 150, 900), fill=(40, 90, 200))
    assert A.looks_textual(chart)


# --------------------------------------------------------------------- _open_picture

def test_a_transparent_png_is_flattened_onto_white_not_black():
    flat = A._open_picture(encoded(Image.new("RGBA", (50, 40), (255, 0, 0, 0)), "PNG"), "")
    assert flat.mode == "RGB" and flat.getpixel((3, 3)) == (255, 255, 255)


def test_a_palette_png_with_transparency_is_flattened():
    palette = Image.new("P", (30, 30), 0)
    palette.putpalette([0, 0, 0] * 256)
    opened = A._open_picture(encoded(palette, "PNG", transparency=0), "")
    assert opened.mode == "RGB" and opened.getpixel((2, 2)) == (255, 255, 255)


@pytest.mark.parametrize("mode", ["LA", "L", "1", "CMYK"])
def test_other_colour_modes_come_out_as_rgb(mode):
    fmt = "JPEG" if mode == "CMYK" else "PNG"
    assert A._open_picture(encoded(Image.new(mode, (12, 12)), fmt), "").mode == "RGB"


def test_an_animated_gif_gives_its_first_frame():
    frames = [Image.new("RGB", (30, 20), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    gif = encoded(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    opened = A._open_picture(gif, "")
    assert opened.size == (30, 20)
    red, green, blue = opened.getpixel((2, 2))
    assert red > 200 and green < 60 and blue < 60


def test_exif_rotation_is_applied():
    exif = Image.Exif()
    exif[0x0112] = 6                      # rotate 90 clockwise to view
    assert A._open_picture(encoded(Image.new("RGB", (60, 20), "blue"), "JPEG", exif=exif), "").size == (20, 60)


def test_a_big_picture_is_brought_down_to_the_working_side():
    opened = A._open_picture(encoded(Image.new("RGB", (6000, 3000), "white"), "JPEG"), "")
    assert max(opened.size) == A.MAX_WORKING_SIDE and opened.size == (4096, 2048)


@pytest.mark.parametrize("cut", [300, 600])
def test_a_truncated_file_is_a_plain_error(cut):
    photo = noise(300, 300)
    for fmt in ("PNG", "JPEG"):
        with pytest.raises(ValueError, match="couldn't be read"):
            A._open_picture(encoded(photo, fmt)[:cut], "")


@pytest.mark.parametrize("size", [(9000, 9000), (20000, 20000)])
def test_a_decompression_bomb_is_refused_with_a_plain_sentence(size):
    # 9000 x 9000 is over MAX_PIXELS (a warning that is raised), 20000 x 20000 over twice that (an error).
    with pytest.raises(ValueError, match="too large"):
        A._open_picture(png_header_only(*size), "")


def test_bytes_that_are_not_a_picture_are_refused_without_a_work_directory():
    with pytest.raises(ValueError, match="can't read that kind of picture"):
        A._open_picture(b"definitely not an image", "")


def test_a_format_pillow_cannot_read_goes_to_sips_with_a_size_check_first(tmp_path):
    calls = []

    class Done:
        def __init__(self, stdout=b"", returncode=0):
            self.stdout, self.returncode = stdout, returncode

    def fake_run(command, **kwargs):
        calls.append(command)
        if "-g" in command:
            return Done(b"  pixelWidth: 3000\n  pixelHeight: 2000\n")
        (tmp_path / "output.jpg").write_bytes(encoded(Image.new("RGB", (30, 20), "green"), "JPEG"))
        return Done()
    with patch.object(A.subprocess, "run", fake_run):
        opened = A._open_picture(b"ftypheic-ish unknown bytes", str(tmp_path))
    assert opened.size == (30, 20)
    assert "-g" in calls[0] and "-s" in calls[1]                 # asked for the size before decoding


def test_sips_is_not_asked_to_decode_a_picture_that_claims_to_be_enormous(tmp_path):
    class Done:
        stdout, returncode = b"pixelWidth: 40000\npixelHeight: 40000\n", 0
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return Done()
    with patch.object(A.subprocess, "run", fake_run), pytest.raises(ValueError, match="too large"):
        A._open_picture(b"unknown bytes", str(tmp_path))
    assert len(calls) == 1


def test_a_sips_failure_is_a_plain_error(tmp_path):
    def broken(*args, **kwargs):
        raise OSError("no sips here")
    with patch.object(A.subprocess, "run", broken), pytest.raises(ValueError, match="couldn't be read"):
        A._open_picture(b"unknown bytes", str(tmp_path))


# ------------------------------------------------------------------------- tiles

def test_a_large_screen_gets_an_overview_and_sections():
    result = A.tiles(encoded(screen(2400, 1500), "PNG"))
    assert result["tiled"] and (result["width"], result["height"]) == (2400, 1500)
    assert 3 <= len(result["images"]) <= 1 + A.MAX_TILES
    overview = image_of(result["images"][0])
    assert max(overview.size) <= A.OVERVIEW_SIDE
    for url in result["images"][1:]:
        assert max(image_of(url).size) <= A.OVERVIEW_SIDE


def test_a_small_picture_is_one_image():
    result = A.tiles(encoded(screen(1200, 800), "PNG"))
    assert not result["tiled"] and len(result["images"]) == 1


def test_a_large_photo_is_one_image_not_tiles():
    result = A.tiles(encoded(noise(2400, 1600), "PNG"))
    assert not result["tiled"] and len(result["images"]) == 1 and result["width"] == 2400


def test_a_huge_screen_stays_within_the_tile_cap():
    result = A.tiles(encoded(screen(6000, 4000), "JPEG"))
    assert result["tiled"] and len(result["images"]) <= 1 + A.MAX_TILES


# ---------------------------------------------------------------------- pdf_pages

def scanned_pdf(pages: int = 2, size=(300, 400)) -> bytes:
    images = [Image.effect_noise(size, 40 + i).convert("RGB") for i in range(pages)]
    return encoded(images[0], "PDF", save_all=True, append_images=images[1:])


def test_scanned_pdf_pages_come_back_as_images():
    result = A.pdf_pages(scanned_pdf(3))
    assert result["pages"] == 3 and result["shown"] == 3 and len(result["images"]) == 3
    assert image_of(result["images"][0]).size == (300, 400)


def test_only_the_first_pages_of_a_long_scan_are_returned_and_counted_honestly():
    result = A.pdf_pages(scanned_pdf(A.MAX_PDF_PAGE_IMAGES + 3, size=(40, 40)))
    assert result["pages"] == A.MAX_PDF_PAGE_IMAGES + 3 and result["shown"] == A.MAX_PDF_PAGE_IMAGES


def test_a_damaged_or_pictureless_pdf_is_a_plain_error():
    with pytest.raises(ValueError, match="damaged"):
        A.pdf_pages(b"%PDF-1.4 this is not really a pdf")
    from pypdf import PdfWriter
    blank = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(blank)
    with pytest.raises(ValueError, match="no pages Wisp can read"):
        A.pdf_pages(blank.getvalue())


def test_an_encrypted_pdf_asks_for_the_password():
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(scanned_pdf(1))))
    writer.encrypt("secret")
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(ValueError, match="password"):
        A.pdf_pages(out.getvalue())


# ------------------------------------------------------------------------ inspect

MZ = b"MZ\x90\x00" + b"\x00" * 200


def warned(result, *words) -> bool:
    text = " ".join(result["warnings"]).lower()
    return all(word in text for word in words)


def test_a_program_in_a_document_name_is_caught():
    result = A.inspect("invoice.pdf.exe", MZ)
    assert result["family"] == "program" and warned(result, "look like a .pdf", ".exe")


def test_a_program_named_pdf_is_caught_by_its_bytes():
    result = A.inspect("invoice.pdf", MZ)
    assert result["real_type"] == "a Windows program" and warned(result, "says .pdf", "really")


def test_a_trailing_space_or_dot_does_not_hide_the_real_extension():
    assert warned(A.inspect("invoice.pdf .exe", MZ), "look like a .pdf")
    assert warned(A.inspect("invoice.pdf.exe ", MZ), "look like a .pdf")
    assert warned(A.inspect("report.pdf.", MZ), "says .pdf")


def test_a_right_to_left_override_is_flagged_and_removed_from_display():
    name = "photo‮gpj.exe"
    assert warned(A.inspect(name, b"hello"), "hidden characters")
    assert "‮" not in A._pretty(name) and A._pretty(name) == "photogpj.exe"


def test_a_plain_document_raises_no_warning():
    assert A.inspect("notes.txt", b"hi there")["warnings"] == []
    pdf = A.inspect("a.pdf", b"%PDF-1.7\n")
    assert pdf["real_type"] == "a PDF" and pdf["warnings"] == []


@pytest.mark.parametrize("magic,family", [
    (b"\x7fELF", "program"), (b"\xcf\xfa\xed\xfe", "program"), (b"#!/bin/sh\n", "script"),
    (b"Rar!\x1a\x07", "archive"), (b"\x1f\x8b\x08", "archive"), (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xd0\xcf\x11\xe0", "document"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image"),
    (b"\x00\x00\x00\x18ftypheic", "image"), (b"\x00\x00\x00\x18ftypisom", "video")])
def test_signatures_name_the_family(magic, family):
    assert A.inspect("file", magic + b"\x00" * 40)["family"] == family


def pe_program(pad: bytes = b"\x01") -> bytes:
    """The smallest thing with a Windows program's shape: "MZ", then where the PE header is, then it."""
    return b"MZ" + pad * 58 + (0x80).to_bytes(4, "little") + pad * 64 + b"PE\x00\x00" + pad * 20


@pytest.mark.parametrize("name,data", [
    ("list.csv", b"MZ-100,widget,4\nMZ-101,gasket,9\n"),
    ("names.txt", b"MZ"),
    ("notes.log", b"MZ " + b"stock count for the north shelf. " * 400),
    ("a.txt", b"BM hello"),
    ("readme.md", b"BMW service notes, kept for the next visit.\n" * 4),
])
def test_text_that_starts_with_the_letters_of_a_program_or_picture_is_not_one(name, data):
    result = A.inspect(name, data)
    assert result["real_type"] == "" and result["family"] == ""
    assert result["warnings"] == [] and result["risk"] == {"level": "", "reasons": []}


def test_a_real_windows_program_is_still_one_whatever_it_is_called():
    old_dos = b"MZ\x90\x00\x03" + b"\x01" * 200                 # no PE header, but a binary one
    for data in (pe_program(), pe_program(b"A"), old_dos, MZ, b"MZ\x00"):
        for name in ("list.csv", "notes.txt", "invoice.pdf", "setup.exe", "thing"):
            result = A.inspect(name, data)
            assert result["real_type"] == "a Windows program" and result["family"] == "program", (name, data[:8])
            assert result["risk"]["level"] == "high"
    assert warned(A.inspect("list.csv", pe_program(b"A")), "says .csv", "really a windows program")
    # With no zero byte in the first 8 KB, the PE header alone decides: it must be where the file
    # says it is, and inside the file. (Only a header 16 MB in has an address with no zero byte.)
    far = 0x01010101
    start = b"MZ" + b"A" * 58 + far.to_bytes(4, "little")
    body = b"A" * (far - len(start))
    assert A._is_windows_program(start + body + b"PE\x00\x00") is True
    assert A.inspect("list.csv", start + body + b"PE\x00\x00")["family"] == "program"
    assert A._is_windows_program(start + body + b"PEAA") is False
    assert A._is_windows_program(start + body + b"PE\x00") is False             # cut off inside the header
    assert A._is_windows_program(start + b"A" * 64) is False                    # the address is past the end
    assert A._is_windows_program(b"MZ" + b"A" * 61) is False                    # too short to say where it is


def test_a_real_bmp_is_still_a_picture_and_two_letters_are_not():
    bmp = encoded(Image.new("RGB", (4, 4), "white"), "BMP")
    assert A.inspect("p.bmp", bmp)["real_type"] == "a BMP image" and A.inspect("p.txt", bmp)["family"] == "image"
    for size in (12, 40, 52, 56, 64, 108, 124):
        assert A._is_bmp(b"BM" + b"\x01" * 12 + size.to_bytes(4, "little") + b"\x01" * 8)
    assert not A._is_bmp(b"BM" + b"\x01" * 12 + (41).to_bytes(4, "little") + b"\x01" * 8)
    assert not A._is_bmp(b"BM" + b"\x01" * 12 + (40).to_bytes(4, "little"))     # cut off inside the header
    # A picture name over text is still told it doesn't match.
    assert risk_of("scan.bmp", b"BM hello")["level"] == "caution"
    assert risk_of("scan.jpg", b"MZ-100,widget")["level"] == "caution"


def test_a_mac_disk_image_is_known_by_its_trailer():
    result = A.inspect("update.pdf", b"x" * 1000 + b"koly" + b"y" * 508)
    assert result["family"] == "program" and warned(result, "disk image")


def test_macros_in_an_office_file_are_reported_and_not_run():
    result = A.inspect("budget.xlsm", zipped({"[Content_Types].xml": b"<x/>", "xl/vbaProject.bin": b"\x00",
                                               "xl/printerSettings/p.bin": b"\x00"}))
    assert result["family"] == "zip" and result["macros"] and warned(result, "macros")
    assert result["risky_entries"] == []              # an Office file's own .bin parts are not a hidden program


def test_a_zip_is_only_listed_never_extracted():
    archive = zipped({"setup.exe": b"MZ", "docs/readme.txt": b"hi", "a/": b""})

    def forbidden(*args, **kwargs):
        raise AssertionError("an archive must never be read or unpacked")
    with patch.object(zipfile.ZipFile, "extract", forbidden), patch.object(zipfile.ZipFile, "extractall", forbidden), \
            patch.object(zipfile.ZipFile, "open", forbidden), patch.object(zipfile.ZipFile, "read", forbidden):
        result = A.inspect("photos.zip", archive)
    assert result["entries"] == ["setup.exe", "docs/readme.txt", "a/"] and result["entry_count"] == 3
    assert result["risky_entries"] == ["setup.exe"]


def test_a_zip_bomb_is_listed_without_being_inflated():
    bomb = zipped({"zeros.txt": b"\x00" * 60_000_000})
    assert len(bomb) < 100_000
    started = time.monotonic()
    result = A.inspect("big.zip", bomb)
    assert result["entry_count"] == 1 and time.monotonic() - started < 2


def test_zip_names_are_bounded_and_cleaned():
    many = zipped({f"f{i}.txt": b"x" for i in range(120)} | {"evil‮txt.exe": b"x"})
    result = A.inspect("many.zip", many)
    assert len(result["entries"]) == A.MAX_ZIP_NAMES and result["entry_count"] == 121
    assert "evil‮txt.exe" not in result["risky_entries"] and "eviltxt.exe" in result["risky_entries"]


def test_an_encrypted_or_damaged_zip_says_so():
    encrypted = bytearray(zipped({"a.txt": b"hello"}))
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):          # set the encryption bit in both headers
        at = encrypted.find(marker)
        flag_at = at + (6 if marker == b"PK\x03\x04" else 8)
        encrypted[flag_at] |= 1
    assert A.inspect("a.zip", bytes(encrypted))["encrypted"] is True
    assert A.inspect("bad.zip", b"PK\x03\x04garbage")["zip_error"] is True


def test_web_addresses_in_a_program_are_listed_and_bounded():
    links = b" ".join(b"http://host%d.example/path" % i for i in range(20))
    result = A.inspect("tool.exe", b"MZ\x00\x00" + links + b" see https://x.test/a).")
    assert len(result["urls"]) == A.MAX_URLS and result["urls"][0] == "http://host0.example/path"
    assert A.inspect("tool.exe", b"MZ\x00\x00 https://x.test/a).")["urls"] == ["https://x.test/a"]
    assert "urls" not in A.inspect("notes.txt", b"see http://example.com")


def test_empty_and_unknown_files_are_not_misreported():
    assert A.inspect("", b"") == {"real_type": "", "family": "", "warnings": [], "risk": {"level": "", "reasons": []}}
    assert A.inspect("x.bin", b"\x01\x02\x03")["family"] == ""


# ----------------------------------------------------------- the child, for real

def _child_can_start() -> bool:
    import subprocess
    import sys
    try:
        return subprocess.run([sys.executable, "-I", "-c", "print(1)"], capture_output=True,
                              timeout=20).stdout.strip() == b"1"
    except (OSError, subprocess.TimeoutExpired):
        return False


CHILD = pytest.mark.skipif(not _child_can_start(), reason="this sandbox can't start a child process")


@CHILD
def test_inspect_runs_in_a_child_and_returns_json():
    result = run(D.run_attachment_child("inspect", "invoice.pdf.exe", MZ, timeout=30))
    assert result["family"] == "program" and result["warnings"]


@CHILD
def test_tiles_run_in_a_child_and_the_work_directory_is_removed():
    made = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path
    with patch.object(D.tempfile, "mkdtemp", spy):
        result = run(D.run_attachment_child("tiles", "screen.png", encoded(screen(2400, 1500), "PNG"), timeout=60))
    assert result["tiled"] and len(result["images"]) >= 3
    assert len(made) == 1 and not os.path.exists(made[0])


@CHILD
def test_the_work_directory_is_private_and_removed_even_when_the_child_is_killed(monkeypatch):
    seen = {}
    monkeypatch.setattr(D, "_ATTACHMENT_CHILD", "import sys, time; sys.stdin.buffer.read(); time.sleep(30)")
    real = D.run_child

    async def spy(code, argv, data, *, timeout, fallback=None):
        seen["dir"] = argv[2]
        seen["mode"] = os.stat(argv[2]).st_mode & 0o777
        return await real(code, argv, data, timeout=timeout, fallback=fallback)
    monkeypatch.setattr(D, "run_child", spy)
    with pytest.raises(ValueError, match="took too long"):
        run(D.run_attachment_child("tiles", "a.png", b"x", timeout=0.6))
    assert seen["mode"] == 0o700 and not os.path.exists(seen["dir"])


@CHILD
def test_other_jobs_get_no_work_directory():
    captured = {}
    real = D.run_child

    async def spy(code, argv, data, *, timeout, fallback=None):
        captured["argv"] = argv
        return await real(code, argv, data, timeout=timeout, fallback=fallback)
    with patch.object(D, "run_child", spy):
        run(D.run_attachment_child("inspect", "/some/dir/a.txt", b"hi", timeout=30))
    assert captured["argv"] == ["inspect", "a.txt"]


@CHILD
def test_a_picture_the_child_cannot_read_comes_back_as_a_plain_valueerror():
    with pytest.raises(ValueError, match="can't read that kind of picture|couldn't be read"):
        run(D.run_attachment_child("tiles", "a.png", b"not a picture", timeout=30))
    with pytest.raises(ValueError, match="Unknown job"):
        run(D.run_attachment_child("rm -rf", "a.png", b"x", timeout=30))


@CHILD
def test_a_scanned_pdf_goes_through_the_child():
    result = run(D.run_attachment_child("pdf_pages", "scan.pdf", scanned_pdf(2), timeout=60))
    assert result["pages"] == 2 and len(result["images"]) == 2


def test_main_reports_a_parser_surprise_as_a_plain_error_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["x", "tiles", "a.png", ""])
    monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": io.BytesIO(b"x")})())
    monkeypatch.setattr(A, "tiles", lambda data, workdir="": (_ for _ in ()).throw(RuntimeError("boom")))
    A.main()
    assert "couldn't be read" in json.loads(capsys.readouterr().out)["error"]


def test_main_refuses_more_than_the_input_limit(monkeypatch, capsys):
    monkeypatch.setattr(A, "MAX_INPUT_BYTES", 10)
    monkeypatch.setattr("sys.argv", ["x", "inspect", "a.bin"])
    monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": io.BytesIO(b"x" * 50)})())
    A.main()
    assert "too large" in json.loads(capsys.readouterr().out)["error"]


# ------------------------------------------------------------------- risk verdict

def risk_of(name, data=b"hello"):
    return A.inspect(name, data)["risk"]


def pdf_bytes(encrypt=False):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    if encrypt:
        writer.encrypt("secret")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_innocent_files_have_no_risk():
    assert risk_of("notes.txt") == {"level": "", "reasons": []}
    assert risk_of("a.pdf", pdf_bytes())["level"] == ""
    assert risk_of("p.png", encoded(Image.new("RGB", (4, 4)), "PNG"))["level"] == ""
    docx = zipped({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<x/>", "word/printerSettings/p.bin": b"\x00"})
    assert risk_of("letter.docx", docx) == {"level": "", "reasons": []}
    assert risk_of("table.csv", b"a,b\n1,2\n")["level"] == ""
    assert risk_of("invite.ics", b"BEGIN:VCALENDAR\n")["level"] == ""


@pytest.mark.parametrize("name", ["setup.exe", "run.command", "link.lnk", "go.url", "site.webloc", "x.inetloc",
                                  "app.desktop", "m.scpt", "m.applescript", "w.workflow", "a.action", "t.terminal",
                                  "t.jar", "t.jnlp", "t.hta", "t.scr", "t.pif", "t.msi", "t.reg", "d.iso", "d.img",
                                  "d.vhd"])
def test_a_program_or_shortcut_name_is_high(name):
    result = risk_of(name)
    assert result["level"] == "high" and result["reasons"]
    assert "hello" not in " ".join(result["reasons"])


def test_program_bytes_are_high_whatever_the_name():
    assert risk_of("thing", MZ)["level"] == "high"
    under_doc = risk_of("invoice.pdf", MZ)
    assert under_doc["level"] == "high" and any("not a document or picture" in r for r in under_doc["reasons"])
    assert risk_of("photo.jpg", b"\x7fELF" + b"\x00" * 40)["level"] == "high"
    assert risk_of("update.dmg", b"x" * 1000 + b"koly" + b"y" * 508)["level"] == "high"
    assert risk_of("run", b"#!/bin/sh\necho hi\n")["level"] == "high"


def test_a_document_name_hiding_a_risky_ending_or_direction_characters_is_high():
    assert risk_of("invoice.pdf.exe")["level"] == "high"
    hidden = risk_of("invoice.exe.pdf")
    assert hidden["level"] == "high" and any("tucked inside" in r for r in hidden["reasons"])
    flipped = risk_of("invoice‮gpj.txt")
    assert flipped["level"] == "high" and any("hidden characters" in r for r in flipped["reasons"])
    assert not any("‮" in r for r in flipped["reasons"])


def test_an_archive_listing_programs_is_high_and_reasons_are_fixed_sentences():
    result = risk_of("photos.zip", zipped({"a.txt": b"x", "Setup.EXE": b"MZ", "x/run.sh": b"#!"}))
    assert result["level"] == "high" and "The archive lists programs or scripts inside it." in result["reasons"]
    assert not any("Setup" in r for r in result["reasons"])
    assert risk_of("photos.zip", zipped({"a.txt": b"x"}))["level"] == ""


def test_macros_make_an_office_file_a_caution():
    macro = zipped({"[Content_Types].xml": b"<x/>", "xl/vbaProject.bin": b"\x00"})
    assert risk_of("budget.xlsx", macro) == {"level": "caution", "reasons": [
        "It is an Office file that contains macros, which can run commands if you enable them."]}
    assert risk_of("budget.xlsm", zipped({"[Content_Types].xml": b"<x/>"}))["level"] == "caution"
    for suffix in ("docm", "pptm"):
        assert risk_of(f"a.{suffix}", b"PK\x03\x04")["level"] == "caution"
    old = b"\xd0\xcf\x11\xe0" + b"\x00" * 100 + "_VBA_PROJECT".encode("utf-16-le") + b"\x00" * 50
    assert risk_of("old.doc", old)["level"] == "caution"
    assert risk_of("old.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 100)["level"] == ""


def test_a_password_protected_archive_or_pdf_is_a_caution():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("a.txt", b"x")
    raw = bytearray(out.getvalue())
    # Set the "encrypted" bit in both the local header and the central directory entry.
    raw[6] |= 1
    raw[raw.index(b"PK\x01\x02") + 8] |= 1
    assert risk_of("secret.zip", bytes(raw))["level"] == "caution"
    locked = risk_of("statement.pdf", pdf_bytes(encrypt=True))
    assert locked["level"] == "caution" and locked["reasons"] == [A.ENCRYPTED_PDF_REASON]


def locked_office(extra: bytes = b"") -> bytes:
    names = b"".join(name.encode("utf-16-le") + b"\x00" * 40 for name in ("EncryptionInfo", "EncryptedPackage"))
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504 + names + extra + b"\x07" * 600


def test_an_office_file_saved_with_a_password_is_named_and_is_a_caution():
    from service import mail_attachments as ma
    from service.document_extract import LOCKED
    for name in ("payroll.xlsx", "letter.docx", "deck.pptx"):
        result = A.inspect(name, locked_office())
        assert result["real_type"] == "a password-protected Office file" and result["family"] == "document"
        assert result["warnings"] == []
        assert result["risk"] == {"level": "caution", "reasons": [
            "It is password-protected, so its contents can't be checked."]}
    # The sentence the reader gives for it is one of Wisp's own, so it is shown as the reason.
    assert ma._file_problem(ValueError(LOCKED)) == LOCKED
    # An old Office file that is not locked is described as before, with no new caution.
    old = A.inspect("old.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    assert old["real_type"] == "an old Office file" and old["risk"]["level"] == ""
    # Naming the two streams does not hide macros.
    both = risk_of("old.doc", locked_office("_VBA_PROJECT".encode("utf-16-le")))
    assert both["level"] == "caution" and len(both["reasons"]) == 2
    assert both["reasons"][0].startswith("It is an Office file that contains macros")


def test_a_web_page_with_a_password_field_or_form_is_a_caution():
    for html in (b"<html><body><INPUT class=a TYPE = 'Password' name=p></body></html>",
                 b'<input name="x" type="password">', b"<input type=password>"):
        assert any("password" in r for r in risk_of("signin.html", html)["reasons"]), html
    assert risk_of("page.htm", b"<p>hi</p><form action=x>")["level"] == "caution"
    assert risk_of("noext", b"<!DOCTYPE html><form>")["level"] == "caution"
    assert risk_of("page.html", b"<html><body>just words</body></html>")["level"] == ""
    # Only the first 512 KiB are scanned.
    assert risk_of("page.html", b"<p>" + b"a" * (512 * 1024) + b"</p><form>")["level"] == ""


def test_unknown_bytes_under_a_document_or_picture_name_are_a_caution_but_not_for_text_types():
    assert risk_of("scan.jpg", b"not a picture at all")["level"] == "caution"
    assert risk_of("scan.pdf", b"not a pdf")["level"] == "caution"
    for name in ("a.txt", "a.csv", "a.md", "a.json", "a.html", "a.ics", "a.vcf", "a.rtf"):
        assert risk_of(name, b"plain words")["level"] == "", name
    assert risk_of("a.pdf", b"")["level"] == ""


def test_the_highest_level_wins_and_reasons_are_capped():
    result = risk_of("invoice.pdf.exe‮", MZ)
    assert result["level"] == "high" and 1 < len(result["reasons"]) <= 5
    mixed = risk_of("x.docm", zipped({"[Content_Types].xml": b"<x/>", "run.exe": b"MZ"}))
    assert mixed["level"] == "high" and len(mixed["reasons"]) == 2
    assert mixed["reasons"][-1].startswith("It is an Office file")


def test_the_old_warnings_are_unchanged_next_to_risk():
    assert warned(A.inspect("invoice.pdf.exe", MZ), "made to look like a .pdf")


# ---------------------------------------------------------------------- structured

def ics(*events, method="", extra=""):
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + (f"METHOD:{method}\r\n" if method else "") + extra
    return (head + "".join(f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n").encode()


def test_calendar_event_with_timezone_attendees_and_escapes():
    body = ("SUMMARY:Planning\\, Q4\\; phase 2\r\n"
            "DTSTART;TZID=America/Los_Angeles:20261114T093000\r\nDTEND;TZID=America/Los_Angeles:20261114T103000\r\n"
            "LOCATION:Room 4\\nSecond floor\r\nORGANIZER;CN=Dana Lee:mailto:dana@example.com\r\n"
            "ATTENDEE;CN=\"Sam, Jr.\":mailto:sam@example.com\r\nATTENDEE:mailto:kim@example.com\r\n"
            "STATUS:CONFIRMED\r\nDESCRIPTION:Back\\\\slash")
    result = A.structured("invite.ics", ics(body, method="REQUEST"))
    assert result["kind"] == "calendar" and result["method"] == "REQUEST" and result["event_count"] == 1
    event = result["events"][0]
    assert event["summary"] == "Planning, Q4; phase 2"
    assert (event["start"], event["end"], event["timezone"], event["all_day"]) == (
        "2026-11-14 09:30", "2026-11-14 10:30", "America/Los_Angeles", False)
    assert event["location"] == "Room 4 Second floor"
    assert event["organizer"] == "Dana Lee"
    assert event["attendees"] == ["Sam, Jr.", "kim@example.com"] and event["attendee_count"] == 2
    assert event["status"] == "CONFIRMED" and event["description"] == "Back\\slash" and event["repeats"] == ""


def test_calendar_utc_all_day_and_folded_lines():
    utc = A.structured("a.ics", ics("SUMMARY:A\r\nDTSTART:20261114T170000Z\r\nDTEND:20261114T180000Z"))
    assert utc["events"][0]["start"] == "2026-11-14 17:00" and utc["events"][0]["timezone"] == "UTC"
    day = A.structured("a.ics", ics("SUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261126\r\nDTEND;VALUE=DATE:20261127"))
    event = day["events"][0]
    assert event["all_day"] and event["start"] == "2026-11-26" and event["end"] == "2026-11-26"
    assert event["timezone"] == ""
    folded = A.structured("a.ics", ics("SUMMARY:A very lo\r\n ng title that\r\n\tkept going\r\nDTSTART:20261114"))
    assert folded["events"][0]["summary"] == "A very long title thatkept going"
    lf_only = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Ab\n cd\nEND:VEVENT\nEND:VCALENDAR\n"
    assert A.structured("a.ics", lf_only)["events"][0]["summary"] == "Abcd"


def test_calendar_repeats_status_and_cancel_method():
    weekly = A.structured("a.ics", ics("SUMMARY:S\r\nDTSTART:20261116T090000\r\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE"))
    assert weekly["events"][0]["repeats"] == "repeats every week on Monday, Wednesday"
    assert A.structured("a.ics", ics("RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5\r\nDTSTART:20261116"))[
        "events"][0]["repeats"] == "repeats every 2 days, 5 times in all"
    assert A.structured("a.ics", ics("RRULE:FREQ=MONTHLY;BYDAY=2TU;UNTIL=20271231T000000Z\r\nDTSTART:20261116"))[
        "events"][0]["repeats"] == "repeats every month on the 2nd Tuesday, until 2027-12-31"
    assert A.structured("a.ics", ics("RRULE:FREQ=YEARLY\r\nDTSTART:20261116"))["events"][0]["repeats"] == \
        "repeats every year"
    assert A.structured("a.ics", ics("RRULE:FREQ=HOURLY;BYSETPOS=2\r\nDTSTART:20261116"))[
        "events"][0]["repeats"] == "repeats"
    cancel = A.structured("a.ics", ics("SUMMARY:Gone\r\nSTATUS:CANCELLED\r\nDTSTART:20261116", method="CANCEL"))
    assert cancel["method"] == "CANCEL" and cancel["events"][0]["status"] == "CANCELLED"
    assert A.structured("a.ics", ics("SUMMARY:x\r\nSTATUS:weird\r\nDTSTART:20261116"))["events"][0]["status"] == ""


def test_calendar_alarm_does_not_override_the_event_and_caps_hold():
    alarm = "SUMMARY:Real\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder text\r\nEND:VALARM\r\nDTSTART:20261116"
    event = A.structured("a.ics", ics(alarm))["events"][0]
    assert event["summary"] == "Real" and event["description"] == ""
    many = A.structured("a.ics", ics(*[f"SUMMARY:E{i}\r\nDTSTART:20261116" for i in range(25)]))
    assert many["event_count"] == 25 and len(many["events"]) == 20 and many["events"][19]["summary"] == "E19"
    crowd = "\r\n".join(f"ATTENDEE:mailto:p{i}@example.com" for i in range(30))
    event = A.structured("a.ics", ics(f"SUMMARY:Big\r\nDTSTART:20261116\r\n{crowd}\r\nDESCRIPTION:{'x' * 900}"))["events"][0]
    assert len(event["attendees"]) == 12 and event["attendee_count"] == 30 and len(event["description"]) == 600


def test_vcard_21_quoted_printable_and_30():
    old = ("BEGIN:VCARD\r\nVERSION:2.1\r\nN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:M=C3=BCller;Jos=C3=A9;;Dr.;\r\n"
           "ORG:Acme;Sales\r\nTITLE:Manager\r\nTEL;CELL;VOICE:+1 415 555 0100\r\nTEL;WORK:+1 415 555 0101\r\n"
           "EMAIL;INTERNET:jose@example.com\r\nADR;HOME:;;1 Main St;Springfield;IL;62701;USA\r\n"
           "NOTE;ENCODING=QUOTED-PRINTABLE:Line one=\r\n continues here=\r\nand more\r\nEND:VCARD\r\n").encode()
    result = A.structured("c.vcf", old)
    assert result["kind"] == "contacts" and result["contact_count"] == 1
    card = result["contacts"][0]
    assert card["name"] == "Dr. José Müller" and card["organization"] == "Acme, Sales" and card["title"] == "Manager"
    assert card["phones"] == ["cell: +1 415 555 0100", "work: +1 415 555 0101"]
    assert card["emails"] == ["jose@example.com"]
    assert card["addresses"] == ["1 Main St, Springfield, IL, 62701, USA"]
    assert card["note"].startswith("Line one")
    new = ("﻿BEGIN:VCARD\nVERSION:3.0\nFN:Ada Lovelace\nN:Lovelace;Ada;;;\nitem1.EMAIL;TYPE=work:mailto:ada@example.com\n"
           "TEL;TYPE=CELL,VOICE:+44 20 7946 0958\nORG:Analytical Engines\\, Ltd\nNOTE:Met at the\\nconference\nEND:VCARD\n"
           "BEGIN:VCARD\nVERSION:3.0\nN:Babbage;Charles;;;\nEND:VCARD\n").encode()
    result = A.structured("c.vcf", new)
    assert result["contact_count"] == 2
    ada, charles = result["contacts"]
    assert ada["name"] == "Ada Lovelace" and ada["emails"] == ["ada@example.com"]
    assert ada["phones"] == ["cell: +44 20 7946 0958"] and ada["organization"] == "Analytical Engines, Ltd"
    assert ada["note"] == "Met at the conference"
    assert charles["name"] == "Charles Babbage"


def test_contact_caps():
    cards = "".join(f"BEGIN:VCARD\nFN:P{i}\n" + "".join(f"TEL:{n}\nEMAIL:e{n}@x.com\n" for n in range(9))
                    + "NOTE:" + "n" * 500 + "\nEND:VCARD\n" for i in range(25))
    result = A.structured("c.vcf", cards.encode())
    assert result["contact_count"] == 25 and len(result["contacts"]) == 20
    first = result["contacts"][0]
    assert len(first["phones"]) == 6 and len(first["emails"]) == 6 and len(first["note"]) == 300


def test_structured_strings_lose_control_and_direction_characters():
    event = A.structured("a.ics", ics("SUMMARY:Pay ‮evil‬ now\x07\x1b[31m\r\nDTSTART:20261116\r\n"
                                     "LOCATION:A⁦B‏C"))["events"][0]
    for text in (event["summary"], event["location"]):
        assert all(c.isprintable() and c not in "‮⁦‏" for c in text)
    assert event["location"] == "ABC"
    card = A.structured("c.vcf", "BEGIN:VCARD\nFN:Ev‮e\x00il\nEND:VCARD\n".encode())["contacts"][0]
    assert "‮" not in card["name"] and "\x00" not in card["name"]


def test_structured_refuses_other_files_and_big_ones():
    with pytest.raises(ValueError, match="no events or contacts"):
        A.structured("a.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    with pytest.raises(ValueError, match="no events or contacts"):
        A.structured("a.vcf", b"\xff\xfe\x00 garbage")
    with pytest.raises(ValueError, match="larger than Wisp reads as a calendar or contact file"):
        A.structured("a.ics", b"x" * (2 * 1024 * 1024 + 1))


def test_unfolding_a_two_megabyte_file_of_tiny_continuations_is_linear():
    size = 2 * 1024 * 1024
    folded = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\n" + " a\n" * ((size - 100) // 3)
              + "\nEND:VEVENT\nEND:VCALENDAR\n").encode()
    started = time.monotonic()
    result = A.structured("a.ics", folded)
    assert time.monotonic() - started < 2.0
    assert result["event_count"] == 1 and result["events"][0]["summary"].startswith("xaaa")
    quoted = ("BEGIN:VCARD\nFN;ENCODING=QUOTED-PRINTABLE:a=\n" + "b=\n" * ((size - 100) // 3)
              + "c\nEND:VCARD\n").encode()
    started = time.monotonic()
    card = A.structured("c.vcf", quoted)
    assert time.monotonic() - started < 2.0
    assert card["contacts"][0]["name"].startswith("abbb")
    # Many repeats of one parameter must not be quadratic either.
    params = ("BEGIN:VCARD\nFN:x\nTEL" + ";X=1" * ((size - 100) // 4) + ":123\nEND:VCARD\n").encode()
    started = time.monotonic()
    A.structured("p.vcf", params)
    assert time.monotonic() - started < 2.0


def test_unfolding_joins_continuations_and_quoted_printable_lines_correctly():
    assert A._unfold("A:1\n 2\n\t3\nB:x") == ["A:123", "B:x"]
    assert A._unfold("N;ENCODING=QUOTED-PRINTABLE:a=\nb=\nc\nZ:1") == ["N;ENCODING=QUOTED-PRINTABLE:abc", "Z:1"]
    assert A._unfold("N:a=\nb") == ["N:a=", "b"]            # only a quoted-printable value continues


def test_a_file_with_too_many_lines_or_properties_is_refused(monkeypatch):
    monkeypatch.setattr(A, "MAX_LOGICAL_LINES", 50)
    with pytest.raises(ValueError, match="larger than Wisp reads as a calendar or contact file"):
        A.structured("a.ics", b"BEGIN:VCALENDAR\nBEGIN:VEVENT\n" + b"X-A:1\n" * 60 + b"END:VEVENT\nEND:VCALENDAR\n")
    monkeypatch.setattr(A, "MAX_LOGICAL_LINES", 100_000)
    monkeypatch.setattr(A, "MAX_PROPERTIES", 10)
    with pytest.raises(ValueError, match="larger than Wisp reads as a calendar or contact file"):
        A.structured("a.ics", b"BEGIN:VCALENDAR\nBEGIN:VEVENT\n" + b"X-A:1\n" * 20 + b"END:VEVENT\nEND:VCALENDAR\n")
    assert A.structured("a.ics", ics("SUMMARY:Hi\r\nDTSTART:20261116"))["event_count"] == 1


# ------------------------------------------------------------------------ ocr_images

def jpeg_of(url):
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:3] == b"\xff\xd8\xff"
    return raw, Image.open(io.BytesIO(raw))


def test_a_huge_picture_comes_back_as_one_bounded_jpeg():
    big = noise(1500, 1000).resize((6000, 4000), Image.Resampling.NEAREST)
    result = A.ocr_images("scan.png", encoded(big, "PNG"))
    assert result["pages"] == 1 and result["read_pages"] == [1] and len(result["images"]) == 1
    raw, image = jpeg_of(result["images"][0])
    assert max(image.size) == 3000 and image.mode == "RGB" and len(raw) <= 3 * 1024 * 1024


def test_a_picture_too_noisy_for_the_size_limit_is_shrunk_until_it_fits():
    wild = Image.effect_noise((3000, 2000), 120).convert("RGB")
    raw, image = jpeg_of(A.ocr_images("p.png", encoded(wild, "PNG"))["images"][0])
    assert len(raw) <= 3 * 1024 * 1024 and max(image.size) <= 3000


def test_ocr_images_refuses_bombs_and_non_pictures():
    with pytest.raises(ValueError, match="too large"):
        A.ocr_images("p.png", png_header_only(30000, 30000))
    with pytest.raises(ValueError):
        A.ocr_images("p.png", b"nothing")


def test_an_image_only_pdf_gives_one_jpeg_per_page():
    result = A.ocr_images("scan.pdf", scanned_pdf(3, size=(600, 800)))
    assert result["pages"] == 3 and result["read_pages"] == [1, 2, 3] and len(result["images"]) == 3
    for url in result["images"]:
        assert jpeg_of(url)[1].size == (600, 800)       # these pages are noise, not text: never enlarged


def test_a_picture_of_text_is_enlarged_for_recognition_and_a_photo_is_not():
    page = Image.new("RGB", (1512, 982), "white")
    ImageDraw.Draw(page).text((40, 40), "Invoice ABC-1234 total $12.50", fill="black")
    shown = jpeg_of(A.ocr_images("screen.png", encoded(page, "PNG"))["images"][0])[1]
    assert shown.size == (4536, 2946)                                   # three times, under the 6000 cap
    wide = jpeg_of(A.ocr_images("wide.png", encoded(Image.new("RGB", (4000, 1000), "white"), "PNG"))["images"][0])[1]
    assert max(wide.size) == A.OCR_TEXT_SIDE
    photo = jpeg_of(A.ocr_images("photo.png", encoded(noise(1200, 800), "PNG"))["images"][0])[1]
    assert photo.size == (1200, 800)


def test_a_picture_of_text_also_returns_a_small_overview_for_the_vision_model():
    page = Image.new("RGB", (3024, 1964), "white")
    ImageDraw.Draw(page).text((40, 40), "Invoice ABC-1234 total $12.50", fill="black")
    result = A.ocr_images("screen.png", encoded(page, "PNG"))
    recognition = jpeg_of(result["images"][0])[1]
    overview = jpeg_of(result["overview"])[1]
    assert max(recognition.size) > 4096                  # too big for vision.decode_image: why the overview exists
    assert max(overview.size) <= A.OVERVIEW_SIDE and overview.mode == "RGB"
    assert result["overview"] != result["images"][0]


def test_a_pdf_result_has_no_overview():
    assert "overview" not in A.ocr_images("scan.pdf", scanned_pdf(2, size=(600, 800)))


def test_a_pdf_that_opens_with_no_password_is_not_refused():
    class Blank:
        is_encrypted = True

        def decrypt(self, password):
            return 1 if password == "" else 0
    A._unlock(Blank())

    class Locked(Blank):
        def decrypt(self, password):
            return 0
    with pytest.raises(ValueError, match="password-protected"):
        A._unlock(Locked())

    class NoCipher(Blank):
        def decrypt(self, password):
            raise RuntimeError("cryptography>=3.1 is required for AES algorithm")
    with pytest.raises(ValueError, match="password-protected"):
        A._unlock(NoCipher())


def test_ocr_pdf_skips_tiny_pictures_caps_pages_and_stops_at_the_pixel_budget(monkeypatch):
    assert A.MAX_OCR_PAGES == 40
    with pytest.raises(ValueError, match="no pages Wisp can read"):
        A.ocr_images("s.pdf", scanned_pdf(2, size=(100, 120)))
    monkeypatch.setattr(A, "MAX_OCR_PAGES", 2)
    capped = A.ocr_images("s.pdf", scanned_pdf(4, size=(320, 320)))
    assert capped["pages"] == 4 and capped["read_pages"] == [1, 2] and capped["capped"] is True
    monkeypatch.setattr(A, "MAX_OCR_PAGES", 40)
    assert "capped" not in A.ocr_images("s.pdf", scanned_pdf(4, size=(320, 320)))
    monkeypatch.setattr(A, "OCR_MAX_TOTAL_PIXELS", 320 * 320 * 2 + 10)
    budget = A.ocr_images("s.pdf", scanned_pdf(4, size=(320, 320)))
    assert budget["read_pages"] == [1, 2] and budget["pages"] == 4 and budget["capped"] is True
    monkeypatch.setattr(A, "OCR_MAX_TOTAL_PIXELS", 400_000_000)
    first = A.ocr_images("s.pdf", scanned_pdf(1, size=(320, 320)))
    monkeypatch.setattr(A, "OCR_MAX_TOTAL_BYTES", len(first["images"][0]) + 10)
    sized = A.ocr_images("s.pdf", scanned_pdf(4, size=(320, 320)))
    assert sized["read_pages"] == [1] and sized["capped"] is True


def test_ocr_pdf_errors_are_plain():
    with pytest.raises(ValueError, match="damaged"):
        A.ocr_images("s.pdf", b"%PDF-1.4 not really")
    with pytest.raises(ValueError, match="password"):
        A.ocr_images("s.pdf", pdf_bytes(encrypt=True))


def test_new_jobs_are_wired_into_main(monkeypatch, capsys):
    import sys as _sys
    for job, name, data, key in (("structured", "a.ics", ics("SUMMARY:Hi\r\nDTSTART:20261116"), "events"),
                                 ("ocr_images", "p.png", encoded(Image.new("RGB", (40, 40)), "PNG"), "images")):
        monkeypatch.setattr(A, "_limit_self", lambda: None)
        monkeypatch.setattr(_sys, "argv", ["child", job, name, ""])
        monkeypatch.setattr(_sys, "stdin", type("S", (), {"buffer": io.BytesIO(data)})())
        A.main()
        assert key in json.loads(capsys.readouterr().out)


# ------------------------------------------------- unsupported ciphers and the time budget

class DependencyError(Exception):
    """Stands in for pypdf.errors.DependencyError, which is matched by class name."""


class _Reader:
    is_encrypted = True

    def __init__(self, *_args, **_kwargs):
        pass

    def decrypt(self, password):
        return 1


def test_a_dependency_error_from_decrypt_says_the_cipher_is_unsupported():
    class NeedsCryptography(_Reader):
        def decrypt(self, password):
            raise DependencyError("cryptography>=3.1 is required for AES algorithm")
    with pytest.raises(ValueError, match="encrypted in a way Wisp can't open"):
        A._unlock(NeedsCryptography())

    class Locked(_Reader):
        def decrypt(self, password):
            return 0
    with pytest.raises(ValueError, match="password-protected"):
        A._unlock(Locked())


@pytest.mark.parametrize("job", ["pdf_pages", "ocr_images"])
def test_a_dependency_error_while_reading_pages_is_not_called_damaged(monkeypatch, job):
    import pypdf

    def call():
        return A.pdf_pages(b"%PDF-1.7") if job == "pdf_pages" else A.ocr_images("s.pdf", b"%PDF-1.7")

    class BadImages(_Reader):
        class _Page:
            @property
            def images(self):
                raise DependencyError("cryptography is required")
        pages = [_Page()]

    class BadPages(_Reader):
        @property
        def pages(self):
            raise DependencyError("cryptography is required")

    class Damaged(_Reader):
        @property
        def pages(self):
            raise RuntimeError("broken")
    for reader, expected in ((BadImages, "encrypted in a way"), (BadPages, "encrypted in a way"),
                             (Damaged, "damaged")):
        monkeypatch.setattr(pypdf, "PdfReader", reader)
        with pytest.raises(ValueError, match=expected):
            call()


def test_ocr_images_stops_for_time_and_says_which_pages_it_read(monkeypatch):
    pdf = scanned_pdf(6, size=(400, 500))
    full = A.ocr_images("s.pdf", pdf)
    assert full["read_pages"] == [1, 2, 3, 4, 5, 6] and "stopped_early" not in full
    monkeypatch.setattr(A, "OCR_TIME_BUDGET_S", 1e-9)
    partial = A.ocr_images("s.pdf", pdf)
    assert partial["pages"] == 6 and partial["read_pages"] == [1]       # the first page is always tried
    assert len(partial["images"]) == 1 and partial["stopped_early"] is True


def test_enlargement_stays_bounded():
    page = Image.new("RGB", (150, 60), "white")
    assert max(image_of(A._recognition_jpeg(page, A.OCR_PAGE_TEXT_SIDE)).size) == 450       # 3x, never more
    tall = Image.new("RGB", (2000, 3000), "white")
    assert max(image_of(A._recognition_jpeg(tall, A.OCR_PAGE_TEXT_SIDE)).size) == A.OCR_PAGE_TEXT_SIDE
    assert max(image_of(A._recognition_jpeg(Image.new("RGB", (3000, 2000), "white"))).size) == A.OCR_TEXT_SIDE
