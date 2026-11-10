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


@pytest.fixture(autouse=True)
def _no_cpu_cap_on_the_test_process(monkeypatch):
    """`A.main()` caps its own CPU time, which is right for the child process. A test that calls
    `main()` in this process would put that cap on the whole test run and get it killed later."""
    monkeypatch.setattr(A, "_limit_self", lambda: None)


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
    assert capped["pages"] == 4 and capped["read_pages"] == [1, 2]
    monkeypatch.setattr(A, "MAX_OCR_PAGES", 40)
    monkeypatch.setattr(A, "OCR_MAX_TOTAL_PIXELS", 320 * 320 * 2 + 10)
    budget = A.ocr_images("s.pdf", scanned_pdf(4, size=(320, 320)))
    assert budget["read_pages"] == [1, 2] and budget["pages"] == 4


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


# ================================================== recordings and attached emails

import hashlib
import math
import subprocess
import sys
import wave
from email import message_from_bytes
from email.message import EmailMessage


def _tool_works(argv) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def tone_samples(seconds: float, rate: int = 16000, quiet=(), amplitude=8000) -> bytes:
    """16-bit mono PCM: a 440 Hz tone, with a silent stretch for each (start, end) in `quiet` (seconds)."""
    one = bytearray()
    for i in range(rate):
        one += struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / rate)))
    whole, rest = divmod(seconds, 1)
    out = bytearray(bytes(one) * int(whole)) + bytes(one[:int(rest * rate) * 2])
    for start, end in quiet:
        out[int(start * rate) * 2:int(end * rate) * 2] = b"\x00" * (int(end * rate) * 2 - int(start * rate) * 2)
    return bytes(out)


def wav_file(seconds: float, rate: int = 16000, **kwargs) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(tone_samples(seconds, rate, **kwargs))
    return out.getvalue()


def encode_with_afconvert(wav: bytes, tmp_path, fmt: str, codec: str) -> bytes | None:
    """The recording as another format, made by the system's own tool; None where it can't do that here."""
    source, target = tmp_path / "in.wav", tmp_path / "out.bin"
    source.write_bytes(wav)
    if not _tool_works(["/usr/bin/afconvert", "-f", fmt, "-d", codec, str(source), str(target)]) \
            or not target.is_file():
        return None
    return target.read_bytes()


_AFCONVERT_WHY: list = []


def _afconvert_probe() -> str:
    """"" when the audio job really works here (afconvert is run once on a tiny WAV through the same child
    path the tests use), else a plain reason. The CI sandbox refuses to exec /usr/bin/afconvert."""
    if _AFCONVERT_WHY:
        return _AFCONVERT_WHY[0]
    why = ""
    if not os.path.exists("/usr/bin/afconvert"):
        why = "needs macOS's afconvert"
    elif not _child_can_start():
        why = "needs a child process"
    else:
        try:
            probe = run(D.run_attachment_child("audio", "probe.wav", wav_file(0.2), timeout=60))
            if not isinstance(probe, dict) or "seconds" not in probe:
                why = "afconvert did not give a reading of a tiny WAV here"
        except (OSError, PermissionError) as error:
            why = f"afconvert cannot be run here ({type(error).__name__})"
        except Exception as error:  # noqa: BLE001 - a refused exec, a non-zero exit, "couldn't be read"
            why = f"afconvert cannot read a tiny WAV here ({type(error).__name__}: {str(error)[:60]})"
    _AFCONVERT_WHY.append(why)
    return why


AFCONVERT = pytest.mark.skipif(bool(_afconvert_probe()), reason=_afconvert_probe() or "afconvert works")
M4A_HEAD = b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 40


def assert_window(b64: str) -> float:
    """The window is a complete 16 kHz mono 16-bit WAV of at most 29.5 s; returns its length in seconds."""
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        seconds = wav.getnframes() / 16000
    assert 0 < seconds <= 29.5
    return seconds


def hear(name, data, timeout=90):
    return run(D.run_attachment_child("audio", name, data, timeout=timeout))


# ------------------------------------------------------------------ inspect: audio and email

@pytest.mark.parametrize("magic,kind", [
    (b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00", "M4A"), (b"\x00\x00\x00\x1cftypM4B \x00\x00\x00\x00", "M4B"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "WAV"), (b"ID3\x04\x00\x00\x00\x00\x00\x00", "MP3"),
    (b"\xff\xfb\x90\x00\x00\x00", "MP3"), (b"caff\x00\x01\x00\x00", "CAF"), (b"OggS\x00\x02", "Ogg"),
    (b"#!AMR\n\x3c", "AMR"), (b"FORM\x00\x00\x10\x00AIFFCOMM", "AIFF"), (b"FORM\x00\x00\x10\x00AIFCFVER", "AIFF"),
    (b"\xff\xf1\x50\x80\x02\x1f\xfc", "AAC")])
def test_audio_is_recognised_by_its_bytes(magic, kind):
    result = A.inspect("memo", magic + b"\x00" * 40)
    assert result["family"] == "audio" and result["real_type"] == f"an audio recording ({kind})"
    assert result["risk"]["level"] == ""


def test_video_brands_stay_video_and_audio_look_alikes_are_not_audio():
    assert A.inspect("clip", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40)["family"] == "video"
    assert A.inspect("clip", b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 40)["family"] == "video"
    assert A.inspect("a.mp3", b"\xff\xfb\xf0\x00" + b"\x00" * 40)["family"] != "audio"        # bitrate 15 is invalid
    assert A.inspect("a.mp3", b"\xff\xfb\x9c\x00" + b"\x00" * 40)["family"] != "audio"        # sample rate 3 is invalid
    assert A.inspect("a", b"\xff\xd8\xff\xe0" + b"\x00" * 40)["family"] == "image"
    assert A.inspect("run.sh", b"#!/bin/sh\necho hi\n")["family"] == "script"


def test_a_program_is_still_a_program_whatever_its_audio_name_and_audio_with_a_wrong_name_is_flagged():
    program = A.inspect("voice memo.m4a", MZ)
    assert program["family"] == "program" and program["risk"]["level"] == "high"
    assert A.inspect("voice.m4a.exe", M4A_HEAD)["risk"]["level"] == "high"
    mismatch = A.inspect("invoice.pdf", M4A_HEAD)
    assert mismatch["family"] == "audio" and mismatch["risk"]["level"] == "caution"
    assert warned(mismatch, "really an audio recording")
    assert A.inspect("memo.m4a", M4A_HEAD)["warnings"] == []


def test_an_attached_email_is_recognised_by_its_headers():
    head = b"From: Ann <ann@example.com>\r\nTo: bob@example.com\r\nSubject: Hello\r\n\r\nbody"
    result = A.inspect("forwarded.eml", head)
    assert result["family"] == "email" and result["real_type"] == "an email message" and result["risk"]["level"] == ""
    assert A.inspect("x", b"Received: from a\nMessage-ID: <1@x>\nX-Other: y\n\nhi")["family"] == "email"
    assert A.inspect("x", b"From me@example.com Mon Nov  9 00:00:00 2026\nFrom: me@example.com\nDate: today\n\nx"
                     )["family"] == "email"
    assert A.inspect("x", b"Subject: only one known header\n\nbody")["family"] == ""
    assert A.inspect("x", b"Dear Ann,\nFrom: here\nTo: there\n")["family"] == ""
    assert A.inspect("x", b"From: a\nTo: b\x00\x00\n")["family"] == ""
    assert A.inspect("invoice.pdf", head)["risk"]["level"] == "caution"
    assert A.inspect("x.csv", head)["risk"]["level"] == ""


@CHILD
def test_inspect_in_the_child_knows_audio_and_email():
    assert run(D.run_attachment_child("inspect", "memo.m4a", M4A_HEAD, timeout=30))["family"] == "audio"
    mail = b"From: a@example.com\nTo: b@example.com\nSubject: s\n\nbody\n"
    assert run(D.run_attachment_child("inspect", "m.eml", mail, timeout=30))["family"] == "email"


# ------------------------------------------------------------------ audio, for real

@CHILD
@AFCONVERT
def test_a_wav_is_heard_in_windows_that_cover_it():
    result = hear("memo.wav", wav_file(5.0))
    assert result["seconds"] == pytest.approx(5.0, abs=0.05) and result["stopped_early"] is False
    assert len(result["windows"]) == 1 and assert_window(result["windows"][0]) == pytest.approx(5.0, abs=0.05)
    assert result["heard"] == pytest.approx(5.0, abs=0.05)


@CHILD
@AFCONVERT
def test_other_sample_rates_and_the_file_name_do_not_matter(tmp_path):
    result = hear("whatever.pdf", wav_file(2.0, rate=44100))                # a wrong name changes nothing
    assert result["seconds"] == pytest.approx(2.0, abs=0.05)
    caf = encode_with_afconvert(wav_file(2.0), tmp_path, "caff", "LEI16")
    aiff = encode_with_afconvert(wav_file(2.0), tmp_path, "AIFF", "BEI16")
    for name, data in (("a.caf", caf), ("a.aiff", aiff)):
        assert data is not None and hear(name, data)["seconds"] == pytest.approx(2.0, abs=0.05)


@CHILD
@AFCONVERT
def test_an_m4a_is_heard_too(tmp_path):
    m4a = encode_with_afconvert(wav_file(4.0), tmp_path, "m4af", "aac")
    if m4a is None:
        pytest.skip("this environment's afconvert cannot make AAC")
    assert A.inspect("memo.m4a", m4a)["family"] == "audio"
    result = hear("memo.m4a", m4a)
    assert result["seconds"] == pytest.approx(4.0, abs=0.2) and len(result["windows"]) == 1
    assert_window(result["windows"][0])


@CHILD
@AFCONVERT
def test_a_70_second_recording_is_three_windows_cut_in_the_planted_silence():
    result = hear("long.wav", wav_file(70.0, quiet=[(27.0, 27.6)]))
    lengths = [assert_window(w) for w in result["windows"]]
    assert len(lengths) == 3 and result["stopped_early"] is False
    assert sum(lengths) == pytest.approx(result["heard"], abs=0.01)
    assert result["heard"] == pytest.approx(70.0, abs=0.05) and result["seconds"] == pytest.approx(70.0, abs=0.05)
    assert 27.0 <= lengths[0] <= 27.6                                    # the cut is inside the silence


@CHILD
@AFCONVERT
def test_a_700_second_recording_stops_at_600_seconds_and_says_so():
    result = hear("long.wav", wav_file(700.0), timeout=120)
    assert result["seconds"] == pytest.approx(700.0, abs=0.1)
    assert result["heard"] == pytest.approx(600.0, abs=0.05) and result["stopped_early"] is True
    lengths = [assert_window(w) for w in result["windows"]]
    assert sum(lengths) == pytest.approx(result["heard"], abs=0.01)


@CHILD
@AFCONVERT
def test_text_renamed_to_a_recording_and_a_tiny_one_get_plain_sentences():
    with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
        hear("note.m4a", b"This is only a text file, not a recording at all.")
    with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
        hear("note.m4a", b"")
    with pytest.raises(ValueError, match=r"^That recording is empty\.$"):
        hear("blip.wav", wav_file(0.1))
    with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
        hear("x.m4a", MZ)                                                  # a program is never given to the decoder
    with pytest.raises(ValueError, match=r"^That recording (couldn't be read|is empty)\.$"):
        hear("x.wav", wav_file(3.0)[:40] + os.urandom(500))                # a damaged header


@CHILD
def test_the_audio_job_gets_a_private_work_directory_that_is_removed():
    made = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path
    with patch.object(D.tempfile, "mkdtemp", spy):
        with pytest.raises(ValueError):
            hear("a.wav", b"nothing")
    assert len(made) == 1 and not os.path.exists(made[0])


def test_the_audio_job_without_a_work_directory_is_refused():
    with pytest.raises(ValueError, match="can't read that kind of recording"):
        A.audio("a.wav", wav_file(1.0), "")


def test_quietest_cut_picks_the_silent_stretch_inside_the_region():
    import array
    samples = array.array("h", [5000] * (30 * 16000))
    for i in range(int(27.0 * 16000), int(27.5 * 16000)):
        samples[i] = 0
    for i in range(int(20.0 * 16000), int(21.0 * 16000)):                # quieter, but outside the last 4 s
        samples[i] = 0
    cut = A._quietest_cut(samples, int(25.5 * 16000), int(29.5 * 16000))
    assert 27.0 * 16000 <= cut <= 27.5 * 16000


# ------------------------------------------------------------------ attached emails

def build_mail(body="Hello there.", *, html=None, files=(), subject="Quarterly numbers") -> bytes:
    message = EmailMessage()
    message["From"] = "Ann Lee <ann@example.com>"
    message["To"] = "bob@example.com, Cy <cy@example.com>"
    message["Cc"] = "dee@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 09 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = "<abc@example.com>"
    if body is not None:
        message.set_content(body)
    if html is not None:
        if body is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    for name, mime, content in files:
        major, minor = mime.split("/")
        message.add_attachment(content, maintype=major, subtype=minor, filename=name)
    return message.as_bytes()


def read_eml(data: bytes, timeout=60):
    return run(D.run_attachment_child("eml", "forwarded.eml", data, timeout=timeout))


def read_part(data: bytes, index: int, timeout=60):
    return run(D.run_attachment_child("eml_part", "forwarded.eml", data, timeout=timeout, part=index))


@CHILD
def test_a_plain_email_comes_back_with_clean_headers_and_its_body():
    result = read_eml(build_mail("Hello there.\r\nSecond line."))
    assert result["from"] == "Ann Lee <ann@example.com>" and result["cc"] == "dee@example.com"
    assert result["to"] == "bob@example.com, Cy <cy@example.com>" and result["subject"] == "Quarterly numbers"
    assert result["date"].startswith("Mon, 09 Nov 2026") and result["body"] == "Hello there.\nSecond line."
    assert result["body_cut"] is False and result["files"] == [] and result["more_files"] == 0


@CHILD
def test_an_html_only_email_is_turned_to_text_by_the_document_readers_code():
    html = "<html><body><p>Total <b>due</b>: $240</p><script>evil()</script><style>p{}</style><div>Thanks</div>" \
           "<img src='http://tracker.example/x.gif'></body></html>"
    result = read_eml(build_mail(None, html=html))
    assert "Total due: $240" in result["body"] and "Thanks" in result["body"]
    assert "evil" not in result["body"] and "tracker" not in result["body"] and "<" not in result["body"]


@CHILD
def test_multipart_alternative_prefers_the_plain_text():
    result = read_eml(build_mail("Plain version", html="<p>HTML version</p>"))
    assert result["body"] == "Plain version" and result["files"] == []


@CHILD
def test_an_empty_plain_part_falls_back_to_the_html():
    result = read_eml(build_mail("   ", html="<p>Only the web version has words</p>"))
    assert result["body"] == "Only the web version has words"


@CHILD
def test_attachments_are_listed_with_decoded_names_and_sizes():
    content = os.urandom(3000)
    raw = build_mail("See attached", files=[("report.pdf", "application/pdf", content),
                                            ("notes.txt", "text/plain", "line one\nline two\n".encode())])
    # An RFC 2231 name, as many mail programs write it for non-ASCII names.
    raw = raw.replace(b'filename="report.pdf"', b"filename*=UTF-8''r%C3%A9sum%C3%A9%20Q3%20%E2%80%AEfdp.pdf")
    result = read_eml(raw)
    assert result["body"] == "See attached"
    assert [f["index"] for f in result["files"]] == [0, 1]
    first, second = result["files"]
    assert first["name"] == "résumé Q3 fdp.pdf" and first["mime"] == "application/pdf" and first["size"] == 3000
    assert second["name"] == "notes.txt" and second["mime"] == "text/plain" and second["size"] == 18


@CHILD
def test_encoded_word_names_and_path_parts_are_cleaned():
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", b"12345")])
    raw = raw.replace(b'filename="a.bin"', b'filename="=?utf-8?q?..=2F..=2Fevil=0A.txt?="')
    assert read_eml(raw)["files"][0]["name"] == "evil .txt"
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", b"12345")])
    raw = raw.replace(b'filename="a.bin"', b'filename="C:\\\\Users\\\\me\\\\Desktop\\\\x.exe"')
    assert read_eml(raw)["files"][0]["name"] == "x.exe"


@CHILD
def test_an_inline_image_counts_as_a_file_and_unnamed_parts_get_a_plain_name():
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "a@example.com", "b@example.com", "pic"
    message.set_content("text")
    message.add_alternative("<p>hi <img src='cid:logo'></p>", subtype="html")
    message.get_payload()[1].add_related(b"\x89PNG\r\n\x1a\n" + b"0" * 50, "image", "png", cid="<logo>")
    result = read_eml(message.as_bytes())
    assert result["body"] == "text"
    assert [(f["mime"], f["size"]) for f in result["files"]] == [("image/png", 58)]
    assert result["files"][0]["name"] == "image.png"


@CHILD
def test_an_email_inside_the_email_is_listed_not_opened():
    inner = build_mail("Inner secret body", subject="Re: lunch / plans",
                       files=[("deep.pdf", "application/pdf", b"%PDF-1")])
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = "a@example.com", "b@example.com", "Fwd"
    outer.set_content("forwarded below")
    outer.add_attachment(message_from_bytes(inner), disposition="attachment")
    raw = outer.as_bytes()
    result = read_eml(raw)
    assert result["body"] == "forwarded below" and "Inner secret" not in json.dumps(result)
    assert len(result["files"]) == 1
    entry = result["files"][0]
    assert entry["name"] == "Re: lunch - plans.eml"
    assert entry["mime"] == "message/rfc822" and entry["size"] > 100
    part = read_part(raw, 0)
    assert part["name"] == entry["name"] and part["size"] == entry["size"]
    assert b"Inner secret body" in base64.b64decode(part["data_b64"])


@CHILD
def test_eml_part_gives_back_exactly_the_attached_bytes():
    blobs = [os.urandom(5000), b"plain,csv\n1,2\n", os.urandom(77)]
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", blobs[0]), ("t.csv", "text/csv", blobs[1]),
                                 ("b.dat", "application/x-thing", blobs[2])])
    listed = read_eml(raw)["files"]
    assert [f["size"] for f in listed] == [len(b) for b in blobs]
    for index, blob in enumerate(blobs):
        part = read_part(raw, index)
        data = base64.b64decode(part["data_b64"])
        assert hashlib.sha256(data).hexdigest() == hashlib.sha256(blob).hexdigest()
        assert part["size"] == len(blob) and part["name"] == listed[index]["name"]
        assert part["mime"] == listed[index]["mime"]


@CHILD
def test_eml_part_refuses_a_missing_index():
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", b"1")])
    for index in (1, 99, -1):
        with pytest.raises(ValueError, match="isn't in the email"):
            read_part(raw, index)
    with pytest.raises(ValueError, match="isn't in the email"):
        run(D.run_attachment_child("eml_part", "m.eml", raw, timeout=30))        # no index at all


def test_eml_part_refuses_a_file_over_the_limit(monkeypatch):
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", b"0" * 5000)])
    assert A.MAX_EML_PART_BYTES == 24 * 1024 * 1024
    monkeypatch.setattr(A, "MAX_EML_PART_BYTES", 1000)
    with pytest.raises(ValueError, match=r"^That file inside the email is too large\.$"):
        A.eml_part("m.eml", raw, 0)


def test_a_long_list_of_files_is_cut_at_fifty_and_counted():
    raw = build_mail("x", files=[(f"f{n}.bin", "application/octet-stream", b"x" * n) for n in range(60)])
    result = A.eml("m.eml", raw)
    assert len(result["files"]) == 50 and result["more_files"] == 10 and result["files"][49]["index"] == 49
    assert A.eml_part("m.eml", raw, 49)["name"] == "f49.bin"
    with pytest.raises(ValueError, match="isn't in the email"):
        A.eml_part("m.eml", raw, 55)                 # never listed, so never fetched


def test_a_very_long_body_is_cut_and_says_so():
    result = A.eml("m.eml", build_mail("word " * 100_000))
    assert len(result["body"]) == 200_000 and result["body_cut"] is True


def test_headers_are_cleaned_and_cut():
    raw = (b"From: =?utf-8?b?QW5u4oCuZ25pcw==?= <a@example.com>\nTo: " + b"x" * 5000 + b"@example.com\n"
           b"Subject: tab\there\x07bell\nDate: today\n\nbody\n")
    result = A.eml("m.eml", raw)
    assert result["from"] == "Anngnis <a@example.com>" and len(result["to"]) == 300
    assert result["subject"] == "tab here bell"


@CHILD
def test_a_file_that_is_not_an_email_is_a_plain_sentence():
    for data in (b"", b"just some words\nand more words\n", os.urandom(2000), b"\x00" * 100):
        with pytest.raises(ValueError, match=r"^That email file couldn't be read\.$"):
            read_eml(data)


def _hostile(name, data, timeout=60):
    started = time.monotonic()
    try:
        result = run(D.run_attachment_child("eml", name, data, timeout=timeout))
    except ValueError as error:
        result = str(error)
    assert time.monotonic() - started < 40
    return result


@CHILD
def test_hostile_emails_end_in_a_result_or_a_plain_sentence_never_a_hang():
    head = b"From: a@example.com\nTo: b@example.com\nSubject: s\n"
    cases = {
        "truncated base64": head + b"Content-Type: application/octet-stream; name=x.bin\n"
                            b"Content-Transfer-Encoding: base64\n\nQUJDREVGR0hJSktMTU5P" + b"AAAA" * 3 + b"A",
        "boundary never closes": head + b'Content-Type: multipart/mixed; boundary="zz"\n\n--zz\n'
                                 b"Content-Type: text/plain\n\nbody that never ends\n--zz\nContent-Type: text/plain\n\nmore",
        "no boundary at all": head + b"Content-Type: multipart/mixed\n\nsome text\n",
        "5 MB header": b"From: a@example.com\nTo: b@example.com\nSubject: " + b"A" * (5 * 1024 * 1024)
                       + b"\n\nbody\n",
        "5 MB of encoded words": b"From: a@example.com\nTo: b@example.com\nSubject: " + b"=?utf-8?q?A?= " * 400_000
                                 + b"\n\nbody\n",
        "bad charset": head + b"Content-Type: text/plain; charset=nonsense-9\n\ncaf\xe9\n",
        "claims a huge size": head + b"Content-Type: application/octet-stream; name=big.bin; size=99999999999999\n"
                              b"Content-Length: 99999999999999\nContent-Transfer-Encoding: base64\n\nQUJD\n",
    }
    results = {label: _hostile(label, data) for label, data in cases.items()}
    for label, result in results.items():
        assert isinstance(result, dict) or result.startswith("That "), label
    assert isinstance(results["truncated base64"], dict) and results["truncated base64"]["files"][0]["size"] > 0
    assert results["claims a huge size"]["files"][0]["size"] == 3
    assert "caf" in results["bad charset"]["body"]
    assert results["5 MB header"]["subject"] == "A" * 300


@CHILD
def test_a_message_nested_ten_thousand_levels_deep_is_handled():
    parts = [b"From: a@example.com\nTo: b@example.com\nSubject: deep\n"]
    for level in range(10_000):
        parts.append(f'Content-Type: multipart/mixed; boundary="b{level}"\n\n--b{level}\n'.encode())
    parts.append(b"Content-Type: text/plain\n\nbottom\n")
    result = _hostile("deep", b"".join(parts))
    assert isinstance(result, dict) or result == "That email file couldn't be read."


def test_the_walk_is_capped_in_parts_and_in_depth():
    many = b"From: a@example.com\nTo: b@example.com\nContent-Type: multipart/mixed; boundary=q\n\n" + b"".join(
        b"--q\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=f%d.bin\n\nx\n" % n
        for n in range(500)) + b"--q--\n"
    assert len(A._eml_parts(message_from_bytes(many))) == A.MAX_EML_PARTS
    deep = [b"From: a@example.com\nTo: b@example.com\n"]
    for level in range(20):
        deep.append(f'Content-Type: multipart/mixed; boundary="b{level}"\n\n--b{level}\n'.encode())
    deep.append(b"Content-Type: text/plain\n\nbottom\n")
    assert A._eml_parts(message_from_bytes(b"".join(deep))) == []          # the text part is below the depth cap


@CHILD
def test_the_eml_jobs_get_no_work_directory_and_part_is_the_last_argument():
    captured = []
    real = D.run_child

    async def spy(code, argv, data, *, timeout, fallback=None):
        captured.append(argv)
        return await real(code, argv, data, timeout=timeout, fallback=fallback)
    raw = build_mail("x", files=[("a.bin", "application/octet-stream", b"1")])
    with patch.object(D, "run_child", spy):
        read_eml(raw)
        read_part(raw, 0)
    assert captured[0] == ["eml", "forwarded.eml"] and captured[1] == ["eml_part", "forwarded.eml", "", "0"]


# ================================================== invisible characters, more risky kinds, archives of programs

INVISIBLES = ["\u200b", "\u2060", "\u00ad", "\U000E0041", "\ufe0f", "\u180e", "\u3164", "\u200d", "\u034f"]


@pytest.mark.parametrize("invisible", INVISIBLES)
@pytest.mark.parametrize("suffix", ["command", "app", "sh", "scpt", "workflow", "jar", "exe", "terminal"])
def test_an_invisible_character_inside_a_risky_suffix_is_still_high(suffix, invisible):
    name = f"Invoice.{suffix[:1]}{invisible}{suffix[1:]}"
    result = A.inspect(name, b"hello")
    assert result["risk"]["level"] == "high", name
    assert A._suffix(name) == suffix


@pytest.mark.parametrize("invisible", INVISIBLES)
def test_an_invisible_character_in_a_harmless_name_is_a_caution_with_a_reason(invisible):
    risk = A.inspect(f"Rep{invisible}ort.pdf", pdf_bytes())["risk"]
    assert risk["level"] == "caution" and any("hidden characters" in r for r in risk["reasons"])
    assert any("hidden characters" in w for w in A.inspect(f"Rep{invisible}ort.pdf", pdf_bytes())["warnings"])


def test_a_hidden_character_between_the_dots_does_not_hide_a_tucked_program_type():
    assert A.inspect("invoice.exe\u200b.pdf", b"hello")["risk"]["level"] == "high"
    assert A.inspect("run.exe\u00a0", b"hello")["risk"]["level"] == "high"


@pytest.mark.parametrize("name", ["a.fileloc", "a.mobileconfig", "a.shortcut", "a.wflow", "a.saver", "a.prefpane",
                                  "a.qlgenerator", "a.mdimporter", "a.action", "a.osax", "a.dylib", "a.terminal",
                                  "a.command", "a.tool", "a.scptd", "a.applescript", "a.jnlp", "a.url", "a.webloc",
                                  "a.inetloc", "a.sh", "a.zsh", "a.js"])
def test_more_kinds_that_run_or_open_something_are_high(name):
    assert risk_of(name)["level"] == "high"


@pytest.mark.parametrize("name", ["a.py", "a.pl", "a.rb", "a.bash"])
def test_a_script_is_at_least_a_caution(name):
    risk = risk_of(name, b"print('hi')\n")
    assert risk["level"] in ("caution", "high") and any("script" in r for r in risk["reasons"])
    assert A.inspect(name, b"print('hi')\n").get("family") == ""        # reading is unchanged


@pytest.mark.parametrize("entry", ["Tool.app/Contents/Info.plist", "x/Setup.pkg/Contents/a", "Thing.bundle/a",
                                   "F.framework/Versions/A/F", "k.kext/Contents/x", "p.plugin/x",
                                   "P.prefPane/Contents/x", "s.saver/x", "w.workflow/Contents/document.wflow2",
                                   "a.action/x", "Tool.app/"])
def test_an_archive_holding_a_bundle_is_a_caution(entry):
    result = A.inspect("stuff.zip", zipped({"readme.txt": b"hi", entry: b"x"}))
    assert result["risk"]["level"] in ("caution", "high")
    assert result["holds_bundle"] is True
    if result["risk"]["level"] == "caution":
        assert result["risk"]["reasons"] == ["It holds an app."]


def test_an_archive_holding_a_script_is_a_caution_and_plain_documents_stay_clean():
    result = A.inspect("stuff.zip", zipped({"notes/run.py": b"print(1)"}))
    assert result["risk"] == {"level": "caution", "reasons": ["It holds a program or script."]}
    plain = A.inspect("docs.zip", zipped({"a.pdf": b"%PDF", "b/c.txt": b"hi", "d.docx": b"PK"}))
    assert plain["risk"] == {"level": "", "reasons": []} and plain["holds_bundle"] is False


# ------------------------------------------------------- attached emails: nothing dropped silently

def nested_mail(depth: int, leaf: bytes = b"%PDF-1") -> bytes:
    """A message whose files sit at the bottom of `depth` multiparts."""
    inner = (b"Content-Type: application/pdf\r\nContent-Disposition: attachment; filename=deep.pdf\r\n"
             b"Content-Transfer-Encoding: 7bit\r\n\r\n" + leaf + b"\r\n")
    for level in range(depth):
        boundary = f"B{level}".encode()
        inner = (b"Content-Type: multipart/mixed; boundary=" + boundary + b"\r\n\r\n--" + boundary + b"\r\n"
                 + inner + b"\r\n--" + boundary + b"--\r\n")
    return b"From: a@example.com\r\nTo: b@example.com\r\nSubject: nested\r\nMIME-Version: 1.0\r\n" + inner


@CHILD
def test_an_html_body_over_the_cap_says_it_was_cut():
    # Markup past the 2 MB read limit hides the closing words: little text survives, yet text was dropped.
    html = "<html><body><p>start</p>" + "<span></span>" * 200_000 + "<p>the-real-ending</p></body></html>"
    result = read_eml(build_mail(None, html=html), timeout=90)
    assert result["body"] == "start" and result["body_cut"] is True


@CHILD
def test_a_plain_body_cut_at_the_byte_cap_says_so_even_when_little_text_survives():
    # Invisible characters are removed from the body, so 800 KB of them leave almost nothing:
    # the real words after them were still dropped.
    body = "start " + "‎" * 300_000 + " the-real-ending"
    result = read_eml(build_mail(body))
    assert result["body"].startswith("start") and "the-real-ending" not in result["body"]
    assert len(result["body"]) <= 200_000 and result["body_cut"] is True


@CHILD
def test_a_body_that_fits_is_not_cut():
    result = read_eml(build_mail("short and complete"))
    assert result["body_cut"] is False and result["walk_cut"] is False


@CHILD
def test_multiparts_nested_deeper_than_the_limit_say_the_walk_stopped():
    result = read_eml(nested_mail(12))
    assert result["walk_cut"] is True and result["files"] == [] and result["more_files"] >= 1
    shallow = read_eml(nested_mail(A.MAX_EML_DEPTH))
    assert shallow["walk_cut"] is False and [f["name"] for f in shallow["files"]] == ["deep.pdf"]


@CHILD
def test_parts_past_the_part_cap_are_reported_not_silently_dropped():
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "a@example.com", "b@example.com", "many"
    message.set_content("main body")
    for number in range(A.MAX_EML_PARTS + 20):
        message.add_attachment(f"note {number}", subtype="plain", disposition="inline")
    for number in range(5):
        message.add_attachment(b"%PDF-1", maintype="application", subtype="pdf", filename=f"late{number}.pdf")
    result = read_eml(message.as_bytes(), timeout=90)
    assert result["walk_cut"] is True
    assert result["files"] == [] and result["more_files"] >= 1        # a lower bound: the late files were never read


@CHILD
def test_an_html_body_with_a_nul_is_still_turned_into_text():
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "a@example.com", "b@example.com", "nul"
    message.set_content("<html><body>\x00<p>Hello\x00 there\x01 friend</p></body></html>", subtype="html",
                        cte="8bit")
    result = read_eml(message.as_bytes())
    assert "Hello" in result["body"] and "friend" in result["body"] and "\x00" not in result["body"]


def test_when_the_html_reader_still_raises_a_crude_text_is_used_and_the_body_is_marked_cut(monkeypatch):
    import service.document_extract as extract

    def refuse(_data):
        raise ValueError("no")
    monkeypatch.setattr(extract, "_html", refuse)
    text, fell_back = A._html_text("<style>p{}</style><p>Tea &amp; <b>cake</b></p>\x00<script>x()</script>")
    assert text == "Tea & cake" and fell_back is True


@CHILD
def test_eml_part_cannot_fetch_a_file_the_list_did_not_show():
    files = [(f"f{number}.bin", "application/octet-stream", bytes([number]) * 4) for number in range(60)]
    raw = build_mail("many files", files=files)
    listed = read_eml(raw, timeout=90)
    assert len(listed["files"]) == A.MAX_EML_FILES and listed["more_files"] == 10
    last = read_part(raw, A.MAX_EML_FILES - 1, timeout=90)
    assert last["name"] == "f49.bin"
    with pytest.raises(ValueError, match=r"^That file isn't in the email\.$"):
        read_part(raw, A.MAX_EML_FILES, timeout=90)
    with pytest.raises(ValueError, match=r"^That file isn't in the email\.$"):
        read_part(raw, 59, timeout=90)


_BOMB_BASE = b"From: a@example.com\r\nTo: b@example.com\r\nSubject: bomb\r\nMIME-Version: 1.0\r\n"
_FIVE_MB = 5 * 1024 * 1024


def _bomb_mail(kind: str) -> bytes:
    if kind == "semicolons":
        header = b"Content-Disposition: attachment; " + b";" * _FIVE_MB
    elif kind == "pairs":
        header = b"Content-Disposition: attachment; " + b"a=b;" * (_FIVE_MB // 4)
    elif kind == "rfc2231":
        header = b"Content-Disposition: attachment;\r\n " + \
                 b";\r\n ".join(b"filename*%d*=aaaaaaaa" % number for number in range(_FIVE_MB // 30))
    elif kind == "boundary":
        return _BOMB_BASE + b"Content-Type: multipart/mixed; boundary=" + b"x" * _FIVE_MB + b"\r\n\r\nbody\r\n"
    else:
        header = b"\r\n".join(b"X-Filler-%d: v" % number for number in range(100_000))
    return _BOMB_BASE + header + b"\r\nContent-Type: text/plain\r\n\r\nhello\r\n"


@CHILD
@pytest.mark.parametrize("kind", ["semicolons", "pairs", "rfc2231", "boundary", "lines"])
def test_a_header_bomb_is_read_in_seconds_and_marked(kind):
    data = _bomb_mail(kind)
    started = time.monotonic()
    result = read_eml(data, timeout=60)
    assert time.monotonic() - started < 3.0
    assert result["subject"] == "bomb"
    if kind != "lines":
        assert result["walk_cut"] is True and result["files"] == []


@CHILD
def test_a_header_bomb_on_an_inner_part_is_neutralised_too():
    inner = b"Content-Type: application/pdf\r\nContent-Disposition: attachment; " + b";" * _FIVE_MB + \
            b"\r\n\r\n%PDF-1\r\n"
    data = (_BOMB_BASE + b"Content-Type: multipart/mixed; boundary=BB\r\n\r\n--BB\r\nContent-Type: text/plain\r\n\r\n"
            b"words\r\n--BB\r\n" + inner + b"--BB--\r\n")
    started = time.monotonic()
    result = read_eml(data, timeout=60)
    assert time.monotonic() - started < 3.0
    assert result["body"] == "words" and result["walk_cut"] is True
    assert len(result["files"]) == 1 and result["files"][0]["mime"] == "application/pdf"


def test_an_ordinary_long_header_is_left_alone():
    name = "n" * 5000 + ".pdf"
    raw = build_mail("x", files=[(name, "application/pdf", b"%PDF-1")])
    result = A.eml("m.eml", raw)
    assert result["walk_cut"] is False and result["files"][0]["name"].startswith("nnnn")


# ------------------------------------------------------------------ audio: what may reach the decoder

@pytest.mark.parametrize("brand", [b"isom", b"mp42", b"qt  ", b"avc1"])
def test_a_video_renamed_to_a_recording_never_reaches_the_decoder(brand, monkeypatch):
    def forbidden(*_args):
        raise AssertionError("afconvert must not see these bytes")
    monkeypatch.setattr(A, "_convert_to_wav", forbidden)
    video = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + b"junk" * 100
    for name in ("clip.m4a", "clip.mp3", "clip", "clip.wav"):
        with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
            A.audio(name, video, "/nonexistent")


@pytest.mark.parametrize("data", [b"%PDF-1.4 text", b"PK\x03\x04rest", b"\x89PNG\r\n\x1a\nrest", b"#!/bin/sh\necho\n",
                                  b"Rar!\x1a\x07\x00", b"From: a@example.com\nTo: b@example.com\n\nhi"])
def test_other_kinds_named_as_recordings_are_refused_before_the_decoder(data, monkeypatch):
    monkeypatch.setattr(A, "_convert_to_wav", lambda *_a: (_ for _ in ()).throw(AssertionError("decoder reached")))
    with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
        A.audio("voice memo.m4a", data, "/nonexistent")


def test_unrecognised_bytes_reach_the_decoder_only_under_an_audio_name(monkeypatch):
    reached = []

    def stop(data, workdir):
        reached.append(True)
        raise ValueError(A.AUDIO_UNREADABLE)
    monkeypatch.setattr(A, "_convert_to_wav", stop)
    for name in ("note.txt", "note", "note.pdf", "note.html"):
        with pytest.raises(ValueError):
            A.audio(name, b"unknown bytes here", "/nonexistent")
    assert reached == []
    for name in ("memo.m4a", "memo.flac", "MEMO.OPUS", "memo.mp3"):
        with pytest.raises(ValueError):
            A.audio(name, b"unknown bytes here", "/nonexistent")
    assert len(reached) == 4


@CHILD
def test_a_video_renamed_m4a_is_refused_by_the_real_child():
    video = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"junk" * 200
    with pytest.raises(ValueError, match=r"^That recording couldn't be read\.$"):
        hear("holiday.m4a", video)


# ------------------------------------------------------------------ audio: windows, tails and the WAV layout

def riff(chunks: list[tuple[bytes, bytes, int | None]]) -> bytes:
    """A RIFF/WAVE file of (id, body, declared size or None for the true size) chunks, each padded to even."""
    out = b"RIFF\x00\x00\x00\x00WAVE"
    for chunk_id, body, declared in chunks:
        out += chunk_id + struct.pack("<I", len(body) if declared is None else declared) + body
        out += b"\x00" * (len(body) & 1)
    return out


FMT16K = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)


def layout_of(tmp_path, data: bytes, cut=False):
    path = tmp_path / "output.wav"
    path.write_bytes(data)
    return A._wav_layout(path, cut)


def test_the_data_chunk_is_found_by_walking_chunks_not_by_searching(tmp_path):
    samples = b"\x01\x00" * 100
    trap = riff([(b"fmt ", FMT16K, None), (b"LIST", b"INFOdata\x04\x00\x00\x00zzzz", None), (b"data", samples, None)])
    offset, frames = layout_of(tmp_path, trap)
    assert frames == 100 and trap[offset:offset + 2] == b"\x01\x00"
    padded = riff([(b"FLLR", b"x" * 31, None), (b"fmt ", FMT16K, None), (b"JUNK", b"j" * 7, None),
                   (b"data", samples, None)])
    offset, frames = layout_of(tmp_path, padded)
    assert frames == 100 and padded[offset:offset + 2] == b"\x01\x00"


@pytest.mark.parametrize("declared", [0, 0xFFFFFFFF, 10_000_000])
def test_a_streamed_or_wrong_data_size_uses_the_file_size(tmp_path, declared):
    data = riff([(b"fmt ", FMT16K, None), (b"data", b"\x02\x00" * 250, declared)])
    assert layout_of(tmp_path, data)[1] == 250


def test_a_stopped_converter_uses_the_file_size_whatever_the_header_says(tmp_path):
    data = riff([(b"fmt ", FMT16K, None), (b"data", b"\x02\x00" * 250, 20)])
    assert layout_of(tmp_path, data, cut=False)[1] == 10
    assert layout_of(tmp_path, data, cut=True)[1] == 250


def test_a_wav_that_is_not_16k_mono_16bit_or_has_no_data_is_unreadable(tmp_path):
    stereo = struct.pack("<HHIIHH", 1, 2, 16000, 64000, 4, 16)
    for bad in (riff([(b"fmt ", stereo, None), (b"data", b"\x00\x00" * 10, None)]),
                riff([(b"data", b"\x00\x00" * 10, None)]),
                riff([(b"fmt ", FMT16K, None)]),
                b"RIFF\x00\x00\x00\x00WAVE",
                b"not a riff file at all"):
        with pytest.raises(ValueError):
            layout_of(tmp_path, bad)


def test_the_chunk_walk_is_bounded(tmp_path):
    many = [(b"JUNK", b"", None)] * (A._WAV_MAX_CHUNKS + 5)
    data = riff([(b"fmt ", FMT16K, None)] + many + [(b"data", b"\x00\x00" * 10, None)])
    with pytest.raises(ValueError):
        layout_of(tmp_path, data)


def loud(seconds: float, quiet=()):
    import array
    samples = array.array("h", [3000] * int(seconds * 16000))
    for start, end in quiet:
        for index in range(int(start * 16000), min(len(samples), int(end * 16000))):
            samples[index] = 0
    return samples


def test_exactly_one_window_length_is_one_window_and_just_over_is_two():
    ranges, left = A._plan_windows(loud(29.5))
    assert ranges == [(0, 29.5 * 16000)] and left == 0
    ranges, left = A._plan_windows(loud(29.6, quiet=[(27.0, 27.5)]))
    assert len(ranges) == 2 and left == 0 and ranges[-1][1] == len(loud(29.6))
    assert 27.0 * 16000 <= ranges[0][1] <= 27.5 * 16000


def test_all_silent_audio_is_still_cut_and_covered():
    silent = loud(70.0, quiet=[(0, 70)])
    ranges, left = A._plan_windows(silent)
    assert left == 0 and ranges[0][0] == 0 and ranges[-1][1] == len(silent)
    assert all(end - start <= 29.5 * 16000 for start, end in ranges) and len(ranges) == 3
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))


def test_a_cut_search_larger_than_the_audio_left_does_not_fail():
    samples = loud(1.0)
    cut = A._quietest_cut(samples, 0, 5 * 16000)
    assert isinstance(cut, int) and 0 <= cut <= 5 * 16000
    assert A._quietest_cut(loud(0.05), 0, 2 * 16000) >= 0


def test_a_tail_too_short_to_hold_a_word_is_dropped_and_reported_when_it_cannot_be_folded():
    # The quietest stretch ends near the end of the first window, so the 0.19 s that follows is under the
    # minimum, and 29.35 + 0.19 would be over the window length.
    samples = loud(29.54, quiet=[(29.25, 29.5)])
    ranges, left = A._plan_windows(samples)
    assert len(ranges) == 1 and ranges[0][1] < len(samples)
    assert left == len(samples) - ranges[0][1] and 0 < left < 0.2 * 16000


def test_a_tail_never_fits_a_full_window_because_windows_are_only_cut_when_more_than_one_remains():
    # The fold rule is kept for safety; with the cut rule it cannot apply, and this pins that down: the last
    # window plus its tail always exceeds the window length.
    for total in (29.51, 29.54, 29.55, 59.04, 59.05):
        samples = loud(total, quiet=[(total - 0.29, total - 0.04)])
        ranges, left = A._plan_windows(samples)
        assert sum(e - s for s, e in ranges) + left == len(samples)
        assert all(e - s <= 29.5 * 16000 for s, e in ranges) and left < 0.2 * 16000


def pcm_wav(seconds: float, *, width=2, channels=1, rate=16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        frames = int(seconds * rate)
        if width == 1:
            body = bytes(128 + int(60 * math.sin(i / 7)) for i in range(frames))
        elif width == 3:
            body = b"".join(int(2_000_000 * math.sin(i / 7)).to_bytes(3, "little", signed=True)
                            for i in range(frames))
        else:
            body = b"".join(struct.pack("<h", int(8000 * math.sin(i / 7))) for i in range(frames))
        wav.writeframes(body * channels if channels == 1 else
                        b"".join(body[i * width:(i + 1) * width] * channels for i in range(frames)))
    return out.getvalue()


@CHILD
@AFCONVERT
def test_exactly_29_5_seconds_is_one_window_and_29_6_is_covered():
    one = hear("memo.wav", wav_file(29.5))
    assert len(one["windows"]) == 1 and one["stopped_early"] is False and one["dropped_tail"] == 0
    assert assert_window(one["windows"][0]) == pytest.approx(29.5, abs=0.01)
    two = hear("memo.wav", wav_file(29.6))
    lengths = [assert_window(w) for w in two["windows"]]
    assert two["stopped_early"] is False and two["dropped_tail"] < 0.2
    assert sum(lengths) + two["dropped_tail"] == pytest.approx(two["seconds"], abs=0.02)
    assert two["heard"] == pytest.approx(sum(lengths), abs=0.01)


@CHILD
@AFCONVERT
def test_a_tail_that_cannot_be_folded_is_reported_and_heard_is_honest():
    result = hear("memo.wav", wav_file(29.54, quiet=[(29.25, 29.5)]))
    lengths = [assert_window(w) for w in result["windows"]]
    assert 0 < result["dropped_tail"] < 0.2 and result["stopped_early"] is False
    assert result["heard"] == pytest.approx(sum(lengths), abs=0.01)
    assert result["heard"] + result["dropped_tail"] == pytest.approx(result["seconds"], abs=0.02)


@CHILD
@AFCONVERT
def test_all_silent_audio_is_heard_in_windows():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * (16000 * 40))
    result = hear("silence.wav", out.getvalue())
    lengths = [assert_window(w) for w in result["windows"]]
    assert sum(lengths) == pytest.approx(40.0, abs=0.05) and result["stopped_early"] is False


@CHILD
@AFCONVERT
@pytest.mark.parametrize("width,channels,rate", [(1, 1, 8000), (3, 1, 44100), (2, 2, 44100), (3, 2, 48000)])
def test_other_sample_sizes_and_channel_counts_are_converted(width, channels, rate):
    result = hear("memo.wav", pcm_wav(3.0, width=width, channels=channels, rate=rate))
    assert result["seconds"] == pytest.approx(3.0, abs=0.1) and len(result["windows"]) == 1
    assert result["stopped_early"] is False
    assert_window(result["windows"][0])
