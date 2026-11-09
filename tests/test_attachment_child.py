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
    assert A.inspect("", b"") == {"real_type": "", "family": "", "warnings": []}
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
