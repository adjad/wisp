"""Files from Messages, audio, emails attached to emails, and the seams the later workers call
(find, fetch, index_one, whole_text) in service/mail_attachments.py.

The same World as tests/test_mail_attachments.py (no Mail, no Gmail, no model, no app). Where the real
function exists on both sides it is used: the child's `inspect`, the store, the PDF and Word extraction in
the `index_one` tests. The pieces other workers write (`attachment_audio`, the child's `eml` jobs, the
bridge's Messages reads, `attachment_store.note_seen`) are faked to their fixed contracts."""
from __future__ import annotations

import base64
import io
import sys
import types
from typing import NamedTuple

import pytest

from service import attachment_child, attachment_store, documents, large_document, mail_attachments as ma, vision, voice
from service.assistant.hub import hub
from service.mail_memory import bridge
from service.tools import imessage_tools
from tests.test_documents import pdf_with_text
from tests.test_mail_attachments import DAY, MZ, NOW, PDF, PNG, World, gmsg, row

REAL_EXTRACT = documents.extract_async           # before any fixture replaces it
JPEG_URL = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 32).decode()
RECEIPT = "INVOICE 4471\nACME SUPPLY CO\nWidgets\t12\t106.71\nTOTAL DUE\t1280.50\nPay by 2026-11-30"
EML = b"From: Dana <dana@x.com>\r\nSubject: Fwd: lease\r\n\r\nsee attached"


class Heard(NamedTuple):
    text: str
    heard_s: float
    total_s: float
    complete: bool


def message_row(id_="11", name="lease.pdf", ts=NOW - DAY, chat="Family", sender="+15551230000", from_me=False,
                mime="application/pdf", size=50_000, on_disk=True):
    return {"id": id_, "ts": ts, "chat": chat, "sender": "" if from_me else sender, "from_me": from_me,
            "name": name, "mime": mime, "size": size, "on_disk": on_disk}


def eml_info(files=(), body="Please see the lease.", more=0):
    return {"from": "Dana <dana@x.com>", "to": "me@x.com", "cc": "", "subject": "Lease", "date": "Mon, 9 Nov 2026",
            "body": body, "body_cut": False, "files": list(files), "more_files": more}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    w = World(monkeypatch)
    w.app = False
    w.message_rows: list[dict] = []
    w.message_files: dict[str, bytes | str | Exception] = {}
    w.message_index_error: Exception | None = None
    w.message_index_args: list[tuple] = []
    w.families: dict[str, str] = {}
    w.ocr = {"images": [JPEG_URL], "overview": JPEG_URL, "pages": 1, "read_pages": [1]}
    w.recognised = [(RECEIPT, 0.93)]
    w.eml: dict[str, object] = {}
    w.parts: dict[tuple[str, int], dict] = {}
    w.heard = Heard("Call me back about the lease.", 130.0, 130.0, True)
    w.listen_error: Exception | None = None
    w.seen: list[dict] = []
    base_child = documents.run_attachment_child

    async def child(job, name, data, *, timeout, part=None):
        if job == "inspect":
            info = await base_child(job, name, data, timeout=timeout)
            if name in w.families:
                info = {**info, "family": w.families[name]}
            return info
        if job == "ocr_images":
            w.calls.append(("child", job, name))
            return w.ocr
        if job == "structured":
            w.calls.append(("child", job, name))
            return attachment_child.structured(name, data)
        if job == "eml":
            w.calls.append(("child", job, name))
            result = w.eml.get(name)
            if result is None or isinstance(result, Exception):
                raise result or ValueError("That email couldn't be read.")
            return result
        if job == "eml_part":
            w.calls.append(("child", job, name, part))
            return w.parts[(name, part)]
        return await base_child(job, name, data, timeout=timeout)

    async def recognize_text(jpeg, timeout=20.0):
        w.calls.append(("recognise", jpeg[:3]))
        return w.recognised[min(len(w.kinds("recognise")), len(w.recognised)) - 1]

    async def message_index(terms, handles, since, until, limit=200, timeout=10.0):
        w.message_index_args.append((list(terms), list(handles), since, until))
        if w.message_index_error:
            raise w.message_index_error
        return list(w.message_rows), False

    async def message_read(attachment_id, timeout=45.0):
        w.calls.append(("mread", attachment_id))
        item = w.message_files.get(attachment_id)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return None, "", item
        if item is None:
            return None, "", "not_found"
        return item, "x", ""

    async def listen(name, data, *, client, model, deadline):
        w.calls.append(("listen", name))
        if w.listen_error:
            raise w.listen_error
        return w.heard

    fake_audio = types.ModuleType("service.attachment_audio")
    fake_audio.Heard = Heard
    fake_audio.listen = listen
    fake_audio.how_much = lambda heard: (f"Heard all {heard.total_s:.0f} seconds." if heard.complete
                                         else f"Heard the first {heard.heard_s:.0f} of {heard.total_s:.0f} seconds.")
    import service
    monkeypatch.setitem(sys.modules, "service.attachment_audio", fake_audio)
    monkeypatch.setattr(service, "attachment_audio", fake_audio, raising=False)
    monkeypatch.setattr(attachment_store, "note_seen", lambda digest, **kw: w.seen.append({"digest": digest, **kw}) or True,
                        raising=False)
    monkeypatch.setattr(documents, "run_attachment_child", child)
    monkeypatch.setattr(bridge, "recognize_text", recognize_text, raising=False)
    monkeypatch.setattr(bridge, "read_message_attachment_index", message_index, raising=False)
    monkeypatch.setattr(bridge, "read_message_attachment", message_read, raising=False)
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: w.app))
    monkeypatch.setattr(ma, "_ocr_resting_until", 0.0)
    monkeypatch.setattr(imessage_tools, "_name_handles", {})
    monkeypatch.setattr(imessage_tools, "_contacts", {})
    return w


def fenced(text: str) -> list[str]:
    begin, end = ma.untrusted.begin_marker("email attachment"), ma.untrusted.end_marker("email attachment")
    out, pos = [], 0
    while (a := text.find(begin, pos)) != -1:
        b = text.index(end, a)
        out.append(text[a + len(begin):b])
        pos = b + len(end)
    return out


# ---------------------------------------------------------------- Messages

async def test_messages_list_only_searches_only_messages(world):
    world.message_rows = [message_row("11", "lease.pdf"), message_row("12", "photo.jpg", mime="image/jpeg", size=900_000),
                          message_row("13", "notes.txt", ts=NOW - 2 * DAY, from_me=True, mime="text/plain", size=300)]
    world.apple_rows = [row()]
    world.gmail = {"me@gmail.com": [gmsg()]}
    out = await ma.run(query="lease", source="messages", list_only=True)
    assert world.index_args == [] and world.kinds("gsearch") == []          # no Apple Mail, no Gmail
    assert world.message_index_args[0][0] == ["lease"]
    assert "Gmail" not in out and "Nothing was opened." in out
    assert "Files in 2 messages, newest first." in out
    assert "Message from +15551230000 in Family, " in out and "Message you sent in Family, " in out
    assert "lease.pdf" in out and "photo.jpg" in out and "notes.txt" in out
    assert world.kinds("model") == [] and world.kinds("mread") == []        # listing opens nothing
    assert "email" not in out.lower()


async def test_one_message_with_two_files_is_one_group(world):
    world.message_rows = [message_row("11", "a.pdf"), message_row("12", "b.pdf", ts=world.message_rows and 0 or NOW - DAY)]
    out = await ma.run(source="messages", list_only=True)
    assert out.count("Message from") == 1 and "a.pdf" in out and "b.pdf" in out


async def test_messages_read_fetches_through_the_bridge_and_keeps_under_messages(world):
    world.message_rows = [message_row("11", "lease.pdf")]
    world.message_files["11"] = PDF + b" the rent is 2100"
    out = await ma.run(query="lease", source="messages")
    assert "the rent is 2100" in out and "read from Messages" in out
    assert world.kinds("mread") == [("mread", "11")] and world.kinds("local") == []
    assert world.seen[-1]["source"] == "messages" and world.seen[-1]["account"] == "messages"
    assert world.seen[-1]["name"] == "lease.pdf" and world.seen[-1]["subject"] == "Family"
    assert attachment_store.forget_account("messages") == 1
    # the fence kind is the email attachment kind; the wording says Messages
    assert fenced(out) and "email" not in out.lower().replace("email attachment", "")


async def test_a_follow_up_about_a_texted_file_uses_the_saved_reading(world):
    world.message_rows = [message_row("11", "lease.pdf")]
    world.message_files["11"] = PDF + b" the rent is 2100"
    await ma.run(source="messages")
    world.calls.clear()
    out = await ma.run(source="messages", question="what is the rent")
    assert "the rent is 2100" in out and world.kinds("extract") == []


async def test_a_contact_word_becomes_handles_and_leaves_the_terms(world, monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"bob smith": ["+1 555 123 0000", "bob@x.com"]})
    monkeypatch.setattr(imessage_tools, "_contacts", {"5551230000": "Bob Smith"})
    world.message_rows = [message_row("11", "lease.pdf", sender="+15551230000")]
    out = await ma.run(query="bob lease", source="messages", list_only=True)
    terms, handles, _s, _u = world.message_index_args[0]
    assert terms == ["lease"] and handles == ["+1 555 123 0000", "bob@x.com"]
    assert "Message from Bob Smith in Family, " in out


async def test_a_word_that_is_not_a_contact_stays_a_term(world, monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"bob smith": ["+15551230000"]})
    await ma.run(query="bobsled plan", source="messages", list_only=True)
    assert world.message_index_args[0][:2] == (["bobsled", "plan"], [])


async def test_group_headers_in_both_directions(world, monkeypatch):
    monkeypatch.setattr(imessage_tools, "_contacts", {"5551230000": "Bob Smith"})
    world.message_rows = [message_row("11", sender="+15551230000", chat="Trip"),
                          message_row("12", from_me=True, ts=NOW - 3 * DAY, chat="+15551230000")]
    out = await ma.run(source="messages", list_only=True)
    assert "Message from Bob Smith in Trip, " in out
    assert "Message you sent in Bob Smith, " in out


@pytest.mark.parametrize("why,sentence", [
    ("not_on_disk", "Messages hasn't downloaded it to this Mac."),
    ("too_large", "It is larger than Wisp can read (32 MB)."),
    ("not_found", "Messages no longer has it."),
    ("no_messages_db", "Wisp can't read Messages; check Full Disk Access."),
])
async def test_every_messages_fetch_failure_has_a_plain_sentence(world, why, sentence):
    world.message_rows = [message_row("11")]
    world.message_files["11"] = why
    out = await ma.run(source="messages")
    assert f"Not read: {sentence}" in out


async def test_messages_app_not_connected(world):
    world.message_rows = [message_row("11")]
    world.message_files["11"] = bridge.BridgeError("app_not_connected")
    assert "Not read: The Wisp app isn't connected." in await ma.run(source="messages")


async def test_a_file_the_app_says_is_not_on_disk_is_not_asked_for(world):
    world.message_rows = [message_row("11", on_disk=False)]
    out = await ma.run(source="messages")
    assert "Messages hasn't downloaded it to this Mac." in out and world.kinds("mread") == []
    assert "not downloaded by Messages" in await ma.run(source="messages", list_only=True)


async def test_the_local_model_check_comes_before_any_messages_byte(world):
    world.message_rows = [message_row("11")]
    world.message_files["11"] = PDF
    world.model_error = vision.VisionUnavailable("no")
    out = await ma.run(source="messages")
    assert out == ma._LOCAL_ONLY and world.kinds("mread") == []


async def test_messages_search_problems_and_empty_results(world):
    world.message_index_error = bridge.BridgeError("no_messages_db")
    out = await ma.run(query="lease", source="messages")
    assert out.startswith("I didn't find any files in Messages matching lease.")
    assert "Messages couldn't be searched: Wisp can't read Messages; check Full Disk Access." in out
    assert "Gmail" not in out
    world.message_index_error = bridge.BridgeError("app_not_connected")
    assert "the Wisp app isn't connected" in await ma.run(source="messages")


async def test_messages_name_filter_and_tiny_pictures(world):
    world.message_rows = [message_row("11", "logo.png", mime="image/png", size=4_000),
                          message_row("12", "lease.pdf", ts=NOW - 2 * DAY),
                          message_row("13", "menu.pdf", ts=NOW - 3 * DAY)]
    out = await ma.run(source="messages", list_only=True)
    assert "logo.png" not in out and "1 small picture" in out and "lease.pdf" in out
    named = await ma.run(source="messages", name="menu", list_only=True)
    assert "menu.pdf" in named and "lease.pdf" not in named
    tiny = await ma.run(source="messages", name="logo", list_only=True)
    assert "logo.png" in tiny


async def test_a_texted_video_is_not_read_and_the_sentence_says_messages(world):
    world.message_rows = [message_row("11", "clip.mp4", mime="video/mp4")]
    world.message_files["11"] = b"\x00\x00\x00\x18ftypmp42" + b"0" * 40
    out = await ma.run(source="messages")
    assert "Wisp doesn't read videos in Messages." in out


async def test_an_unknown_source_behaves_as_mail(world):
    world.apple_rows = [row()]
    world.local_files[(1, "1.2")] = PDF + b" words"
    out = await ma.run(source="whatever", list_only=True)
    assert "Apple Mail" in out and world.message_index_args == []


# ---------------------------------------------------------------- audio

def memo(world, name="memo.m4a", data=b"\x00\x00\x00\x20ftypM4A " + b"0" * 60):
    world.apple_rows = [row(name=name, rfc="v@x.com")]
    world.local_files[(1, "1.2")] = data
    world.families[name] = "audio"


async def test_a_voice_memo_is_transcribed_and_kept(world):
    memo(world)
    out = await ma.run()
    assert "Call me back about the lease." in out
    assert "Transcribed on this Mac; some words may be wrong. Heard all 130 seconds." in out
    assert len(world.kinds("listen")) == 1 and world.kinds("extract") == []
    stored = attachment_store.get(attachment_store.digest(world.local_files[(1, "1.2")]))
    assert stored and stored.kind == "audio" and stored.how == "transcribed"
    assert world.seen and world.seen[-1]["source"] == "mail" and world.seen[-1]["name"] == "memo.m4a"
    world.calls.clear()
    again = await ma.run()
    assert "Call me back about the lease." in again and "Transcribed on this Mac" in again
    assert world.kinds("listen") == []


async def test_audio_is_found_by_its_suffix_when_the_child_says_nothing(world):
    memo(world)
    world.families["memo.m4a"] = ""                 # the bytes say nothing: the name decides
    assert "Call me back" in await ma.run()


async def test_a_file_the_child_calls_a_video_is_a_video_even_when_named_m4a(world):
    memo(world)
    world.families["memo.m4a"] = "video"
    out = await ma.run()
    assert "Wisp doesn't read videos in email." in out and "Call me back" not in out
    assert world.kinds("listen") == []


async def test_an_incomplete_listen_says_how_much_and_is_not_kept(world):
    memo(world)
    world.heard = Heard("Only the start.", 600.0, 1500.0, False)
    out = await ma.run()
    assert "Only the start." in out and "Heard the first 600 of 1500 seconds." in out
    assert "Read the whole file." not in out
    assert attachment_store.get(attachment_store.digest(world.local_files[(1, "1.2")])) is None
    world.calls.clear()
    await ma.run()
    assert len(world.kinds("listen")) == 1          # nothing was saved, so it listens again


async def test_no_speech(world):
    memo(world)
    world.heard = Heard("  ", 12.0, 12.0, True)
    assert "Not read: No speech could be heard in it." in await ma.run()


async def test_audio_with_a_model_that_cannot_listen(world):
    memo(world)
    world.listen_error = voice.VoiceUnavailable("no")
    assert "Not read: The model Ask is using on this Mac can't listen to audio." in await ma.run()


async def test_a_busy_model_is_a_timeout_sentence(world):
    memo(world)
    world.listen_error = TimeoutError()
    assert "Not read: Reading it took too long." in await ma.run()


async def test_audio_the_child_refuses_gives_its_plain_sentence(world):
    memo(world)
    world.listen_error = ValueError("That recording is too damaged to play.")
    assert "Not read: That recording is too damaged to play." in await ma.run()


async def test_a_texted_memo_is_transcribed_too(world):
    world.message_rows = [message_row("11", "Audio Message.caf", mime="audio/x-caf")]
    world.message_files["11"] = b"caff\x00\x01" + b"0" * 60
    world.families["Audio Message.caf"] = "audio"
    out = await ma.run(source="messages")
    assert "Call me back about the lease." in out and "read from Messages" in out


def test_expected_kinds_for_the_new_branches():
    f = ma._File("x")
    assert ma._expected_kind(f, "memo.m4a", "m4a", "") == "audio"
    assert ma._expected_kind(f, "memo.m4a", "m4a", "video") == ""
    assert ma._expected_kind(f, "memo.bin", "bin", "audio") == "audio"
    assert ma._expected_kind(f, "fwd.eml", "eml", "") == "eml"
    assert ma._expected_kind(f, "fwd.dat", "dat", "email") == "eml"


# ---------------------------------------------------------------- an email attached to an email

def eml_world(world, files, name="Fwd.eml", body="Please see the lease."):
    world.apple_rows = [row(name=name, rfc="f@x.com")]
    world.local_files[(1, "1.2")] = EML
    world.eml[name] = eml_info(files, body=body)
    for entry in files:
        world.parts[(name, entry["index"])] = {"name": entry["name"], "mime": entry["mime"], "size": entry["size"],
                                               "data_b64": base64.b64encode(entry.get("bytes", b"")).decode()}


def inner_file(index, name, mime, data):
    return {"index": index, "name": name, "mime": mime, "size": len(data), "bytes": data}


async def test_an_attached_email_is_read_with_its_files(world):
    pdf = PDF + b" lease rent is 2100 a month"
    files = [inner_file(0, "lease.pdf", "application/pdf", pdf), inner_file(1, "setup.exe", "application/x-msdownload", MZ),
             inner_file(2, "original.eml", "message/rfc822", EML), inner_file(3, "dot.png", "image/png", PNG)]
    eml_world(world, files)
    world.texts["lease.pdf"] = "The lease rent is 2100 a month."
    out = await ma.run()
    assert "From: Dana <dana@x.com>" in out and "Subject: Lease" in out and "Please see the lease." in out
    assert "An email attached to an email. Its files were read in memory; none was run or saved as a file." in out
    assert "Nothing inside it was saved" not in out
    assert "Files in this email (4):" in out and "setup.exe" in out and "original.eml (" in out
    assert "an email inside it, not opened" in out
    assert "The lease rent is 2100 a month." in out                       # the inner PDF, read
    assert "Inspected only. Wisp never opens, unpacks or runs programs" in out   # the inner program
    assert out.count("read from the attached email") == 2
    parts = [c[3] for c in world.kinds("child") if c[1] == "eml_part"]
    assert sorted(parts) == [0, 1]          # never the inner email, never the tiny picture
    assert len(out) <= ma.RESULT_BUDGET_CHARS
    blocks = fenced(out)
    assert any("Please see the lease." in b and "rent is 2100" not in b for b in blocks)      # outer in its fence
    assert any("The lease rent is 2100 a month." in b and "Please see" not in b for b in blocks)   # inner in its own
    assert not any("MZ" in b for b in blocks)


async def test_the_outer_body_is_kept_but_not_its_files_listing(world):
    files = [inner_file(0, "a.txt", "text/plain", b"alpha inner words")]
    eml_world(world, files)
    await ma.run()
    stored = attachment_store.get(attachment_store.digest(EML))
    assert stored and stored.kind == "eml" and "Please see the lease." in stored.text
    assert "Files in this email" not in stored.text and "alpha inner words" not in stored.text
    assert world.seen and any(s["name"] == "a.txt" for s in world.seen)


async def test_an_attached_email_read_again_still_shows_its_files(world):
    files = [inner_file(0, "a.txt", "text/plain", b"alpha inner words")]
    eml_world(world, files)
    await ma.run()
    again = await ma.run()
    assert "Please see the lease." in again and "alpha inner words" in again


async def test_at_most_three_inner_files_are_read(world):
    files = [inner_file(i, f"f{i}.txt", "text/plain", f"inner text {i}".encode()) for i in range(5)]
    eml_world(world, files)
    out = await ma.run()
    assert out.count("read from the attached email") == 3
    assert "Files in this email (5):" in out and "f4.txt" in out
    assert sorted(c[3] for c in world.kinds("child") if c[1] == "eml_part") == [0, 1, 2]


async def test_with_little_room_the_files_are_listed_not_read(world):
    eml_world(world, [inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
    world.apple_rows = [row(i + 1, f"m{i}.eml", rfc=f"r{i}@x.com", attachment_id=f"1.{i}") for i in range(8)]
    for i in range(8):
        world.local_files[(i + 1, f"1.{i}")] = EML + str(i).encode()
        world.eml[f"m{i}.eml"] = eml_info([inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
        world.parts[(f"m{i}.eml", 0)] = world.parts[("Fwd.eml", 0)]
    out = await ma.run()
    assert len(out) <= ma.RESULT_BUDGET_CHARS
    # what earlier files leave unused flows to later ones, but the first, with the least room, only lists
    assert not [c for c in world.kinds("child") if c[1] == "eml_part" and c[2] == "m0.eml"]
    assert "Files in this email (1):" in out and "listed, not read" in out


async def test_an_inner_file_that_fails_gets_its_own_reason(world):
    files = [inner_file(0, "a.txt", "text/plain", b"alpha inner words"), inner_file(1, "b.txt", "text/plain", b"")]
    eml_world(world, files)
    out = await ma.run()
    assert "alpha inner words" in out and "Not read: The file is empty." in out


async def test_an_email_the_child_refuses_is_read_as_plain_text(world):
    world.apple_rows = [row(name="odd.eml", rfc="o@x.com")]
    world.local_files[(1, "1.2")] = EML
    out = await ma.run()                 # no job result registered: the fake child refuses it
    assert "Read as plain text." in out and "Fwd: lease" in out
    assert "An email attached to an email" not in out
    again = await ma.run()
    assert "Fwd: lease" in again


async def test_an_inner_picture_is_recognised_and_a_tiny_one_skipped(world):
    world.app = True
    big = PNG + b"0" * 30_000
    files = [inner_file(0, "scan.png", "image/png", big), inner_file(1, "dot.png", "image/png", PNG)]
    eml_world(world, files)
    out = await ma.run()
    assert "Text in the picture:" in out and "TOTAL DUE" in out
    assert out.count("read from the attached email") == 1
    assert [c[3] for c in world.kinds("child") if c[1] == "eml_part"] == [0]


async def test_an_email_inside_an_attached_email_is_only_listed_even_if_read_directly(world):
    inner = ma._File("deep.eml", nested=True, data=EML)
    reading = await ma._read_file(ma._Ctx(deadline=ma.time.monotonic() + 60, gmail_source=None, gmail_accounts=[],
                                         passes=None), ma._Email("k", NOW), inner, 2000, depth=1)
    assert reading.note == "An email inside it, not opened." and not reading.text


# ---------------------------------------------------------------- find

async def test_find_returns_what_run_used_to_choose(world):
    world.apple_rows = [row(1, "a.pdf", rfc="a@x.com", ts=NOW - DAY), row(2, "b.pdf", rfc="b@x.com", ts=NOW - 2 * DAY,
                                                                         attachment_id="1.3"),
                        row(3, "logo.png", rfc="c@x.com", ts=NOW - 3 * DAY, size=2_000, attachment_id="1.4"),
                        row(4, "future.pdf", rfc="d@x.com", ts=NOW + 10 * DAY, attachment_id="1.5")]
    world.gmail = {"me@gmail.com": [gmsg(rfc="<a@x.com>", files=(("a.pdf", 50_000),)),
                                    gmsg(rfc="<g@x.com>", ts=NOW - 4 * DAY, mid="m2", files=(("g.pdf", 10_000),))]}
    emails, notes = await ma.find(count=2)
    assert [e.ts for e in emails] == sorted((e.ts for e in emails), reverse=True)
    assert len(emails) == 2 and emails[0].apple and emails[0].gmail == ["me@gmail.com"]     # a.pdf merged once
    assert [f.name for f in emails[1].files] == ["b.pdf"]
    assert "1 email dated more than a day and a half ahead (scheduled mail) not counted as recent." in notes
    assert "1 small picture under 20 KB (logos and icons) skipped; name one to read it." in notes
    assert "1 more email with attachments (showing 2, newest first)." in notes
    # and run() lists exactly those emails
    out = await ma.run(count=2, list_only=True)
    assert out.count("Email ") == 2 and "a.pdf" in out and "b.pdf" in out and "g.pdf" not in out


async def test_find_honours_most_name_dates_and_explicit_future(world):
    world.apple_rows = [row(i, f"f{i}.pdf", rfc=f"r{i}@x.com", ts=NOW - i * DAY, attachment_id=f"1.{i}")
                        for i in range(1, 8)]
    emails, notes = await ma.find(count=40, most=40)
    assert len(emails) == 7 and not [n for n in notes if "more email" in n]
    emails, _ = await ma.find(count=40, most=3)
    assert len(emails) == 3
    named, _ = await ma.find(name="f5", count=40)
    assert [f.name for e in named for f in e.files] == ["f5.pdf"]
    world.apple_rows.append(row(9, "later.pdf", rfc="z@x.com", ts=NOW + 10 * DAY, attachment_id="1.9"))
    future, notes = await ma.find(start=NOW, end=NOW + 20 * DAY)
    assert [f.name for e in future for f in e.files if e.ts > NOW] == ["later.pdf"]


async def test_find_for_messages_and_no_model_or_bytes(world):
    world.message_rows = [message_row("11"), message_row("12", name="b.pdf", ts=NOW - 5 * DAY)]
    emails, notes = await ma.find(source="messages")
    assert len(emails) == 2 and emails[0].source == "messages" and emails[0].files[0].message_id == "11"
    assert world.kinds("model") == [] and world.kinds("mread") == [] and world.kinds("local") == []


# ---------------------------------------------------------------- the seams

async def test_fetch_gives_bytes_or_raises_unreadable(world):
    world.apple_rows = [row()]
    world.local_files[(1, "1.2")] = PDF + b" body"
    emails, _ = await ma.find()
    data, where = await ma.fetch(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 30)
    assert data.endswith(b"body") and where == "Mail's saved copy"
    world.local_files[(1, "1.2")] = "not_found"
    with pytest.raises(ma.Unreadable):
        await ma.fetch(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 30)
    assert ma.Unreadable is ma._Unreadable


async def test_fetch_for_a_texted_file(world):
    world.message_rows = [message_row("11")]
    world.message_files["11"] = PDF + b" texted"
    emails, _ = await ma.find(source="messages")
    data, where = await ma.fetch(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 30)
    assert data.endswith(b"texted") and where == "Messages"


async def one(world, name, data, family=None, rfc="x@x.com"):
    world.apple_rows = [row(name=name, rfc=rfc)]
    world.local_files[(1, "1.2")] = data
    if family:
        world.families[name] = family
    emails, _ = await ma.find()
    return emails[0], emails[0].files[0]


async def test_index_one_outcomes(world):
    deadline = ma.time.monotonic() + 60
    email, f = await one(world, "notes.txt", b"remember the milk")
    assert await ma.index_one(email, f, deadline=deadline) == "kept"
    assert attachment_store.get(attachment_store.digest(b"remember the milk")).kind == "txt"
    seen = len(world.seen)
    assert await ma.index_one(email, f, deadline=deadline) == "known"
    assert len(world.seen) == seen + 1 and world.seen[-1]["name"] == "notes.txt"        # still noted
    for name, data, family in (("setup.exe", MZ, None), ("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"0" * 40, "video"),
                               ("memo.m4a", b"\x00\x00\x00\x20ftypM4A " + b"0" * 60, "audio")):
        email, f = await one(world, name, data, family)
        assert await ma.index_one(email, f, deadline=deadline) == "skipped", name
    email, f = await one(world, "invite.ics", b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert await ma.index_one(email, f, deadline=deadline) == "skipped"
    world.local_files[(1, "1.2")] = "not_found"
    assert await ma.index_one(email, f, deadline=deadline) == "unreadable"
    assert world.kinds("listen") == [] and world.kinds("vision") == [] and world.kinds("model") == []


async def test_index_one_pictures_and_scans(world):
    deadline = ma.time.monotonic() + 60
    big = PNG + b"0" * 30_000
    email, f = await one(world, "receipt.png", big)
    assert await ma.index_one(email, f, deadline=deadline) == "skipped"         # no app: nothing recognised
    world.app = True
    assert await ma.index_one(email, f, deadline=deadline) == "kept"
    assert "TOTAL DUE" in attachment_store.get(attachment_store.digest(big)).text
    world.recognised = [("", 0.0)]
    email, f = await one(world, "cat.png", big + b"1")
    assert await ma.index_one(email, f, deadline=deadline) == "skipped"         # not text: not described
    email, f = await one(world, "scan.pdf", PDF + b" scanned")
    world.texts["scan.pdf"] = ""

    async def scanned(name, data, *, timeout=45.0, large=False):
        raise ValueError(ma._SCANNED_PDF)
    documents.extract_async, saved = scanned, documents.extract_async
    try:
        assert await ma.index_one(email, f, deadline=deadline) == "skipped"
    finally:
        documents.extract_async = saved
    assert world.kinds("vision") == [] and world.kinds("model") == []


async def test_index_one_keeps_an_attached_emails_text_and_not_its_files(world):
    eml_world(world, [inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
    emails, _ = await ma.find()
    outcome = await ma.index_one(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 60)
    assert outcome == "kept"
    assert "Please see the lease." in attachment_store.get(attachment_store.digest(EML)).text
    assert [c for c in world.kinds("child") if c[1] == "eml_part"] == []


async def test_index_one_never_downloads_from_gmail(world):
    world.gmail = {"me@gmail.com": [gmsg(files=(("notes.txt", 50),))]}
    world.gmail_bytes["g-notes.txt"] = b"gmail only words"
    emails, _ = await ma.find()
    assert await ma.index_one(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 60) == "skipped"
    assert world.kinds("gbytes") == []


async def test_index_one_never_raises(world, monkeypatch):
    email, f = await one(world, "notes.txt", b"words")

    async def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(ma, "_fetch", boom)
    assert await ma.index_one(email, f, deadline=ma.time.monotonic() + 60) == "unreadable"
    assert await ma.index_one(email, f, deadline=0.0) in ("unreadable", "skipped")


async def test_index_one_uses_no_model_on_real_files(world, monkeypatch):
    """Every model entry point raises; the real child inspects and the real extractors read."""
    def trip(*a, **k):
        raise AssertionError("a model path was reached")

    async def atrip(*a, **k):
        raise AssertionError("a model path was reached")
    monkeypatch.setattr(vision, "read_images", atrip)
    monkeypatch.setattr(documents, "local_chat", atrip)
    monkeypatch.setattr(documents, "local_model", trip)
    monkeypatch.setattr(large_document, "condense", atrip)
    monkeypatch.setattr(ma, "_describe", atrip)
    monkeypatch.setattr(ma, "_shape", atrip)
    monkeypatch.setattr(ma, "_finish_text", atrip)
    monkeypatch.setattr(documents, "extract_async", REAL_EXTRACT)
    world.app = True
    deadline = ma.time.monotonic() + 90

    pdf = pdf_with_text("The lease ends on 2027-03-31 and the rent is 2100 dollars.")
    email, f = await one(world, "lease.pdf", pdf)
    assert await ma.index_one(email, f, deadline=deadline) == "kept"
    assert "lease ends" in attachment_store.get(attachment_store.digest(pdf)).text

    import docx
    doc = docx.Document()
    doc.add_paragraph("Quarterly review notes for the Acme account.")
    buffer = io.BytesIO()
    doc.save(buffer)
    email, f = await one(world, "notes.docx", buffer.getvalue(), rfc="d@x.com")
    assert await ma.index_one(email, f, deadline=deadline) == "kept"
    assert "Acme account" in attachment_store.get(attachment_store.digest(buffer.getvalue())).text

    picture = PNG + b"0" * 30_000
    email, f = await one(world, "receipt.png", picture, rfc="p@x.com")
    assert await ma.index_one(email, f, deadline=deadline) == "kept"
    assert "TOTAL DUE" in attachment_store.get(attachment_store.digest(picture)).text
    assert world.kinds("vision") == [] and world.kinds("chat") == [] and world.kinds("model") == []


# ---------------------------------------------------------------- whole_text

def ctx_for(world, **kw):
    return ma._Ctx(deadline=ma.time.monotonic() + 60, gmail_source=ma.gmail_source if hasattr(ma, "gmail_source") else None,
                   gmail_accounts=[], passes=large_document.PassBudget(deadline=ma.time.monotonic() + 60),
                   client=object(), model="m", **kw)


async def test_whole_text_of_a_document_is_unshaped_and_kept(world):
    long = "The notice period is ninety days. " * 800                          # far over any answer budget
    world.texts["contract.pdf"] = long
    email, f = await one(world, "contract.pdf", PDF)
    reading, text, digest = await ma.whole_text(ctx_for(world, question="who signs"), email, f)
    assert text == long and digest == attachment_store.digest(PDF)
    assert reading.where == "Mail's saved copy" and reading.type_text and not reading.problem
    assert world.kinds("chat") == []
    assert attachment_store.get(digest).text == long


async def test_whole_text_of_a_recalled_reading(world):
    world.texts["contract.pdf"] = "The notice period is ninety days."
    email, f = await one(world, "contract.pdf", PDF)
    await ma.whole_text(ctx_for(world), email, f)
    world.calls.clear()
    reading, text, digest = await ma.whole_text(ctx_for(world), email, f)
    assert text == "The notice period is ninety days." and world.kinds("extract") == []
    assert reading.where == "Mail's saved copy" and digest


async def test_whole_text_of_a_program_is_empty_and_says_why(world):
    email, f = await one(world, "setup.exe", MZ)
    reading, text, digest = await ma.whole_text(ctx_for(world), email, f)
    assert text == "" and "Inspected only" in reading.note and reading.risk_level == "high" and digest


async def test_whole_text_of_an_unreadable_file_and_a_non_text_picture(world):
    world.apple_rows = [row(name="x.pdf", rfc="u@x.com")]
    world.local_files[(1, "1.2")] = "not_found"
    emails, _ = await ma.find()
    reading, text, digest = await ma.whole_text(ctx_for(world), emails[0], emails[0].files[0])
    assert text == "" and reading.problem and digest == ""
    world.app = True
    world.recognised = [("", 0.0)]
    email, f = await one(world, "cat.png", PNG + b"0" * 30_000, rfc="c@x.com")
    reading, text, _d = await ma.whole_text(ctx_for(world), email, f)
    assert text == "" and "not read or described" in reading.note and world.kinds("vision") == []


async def test_whole_text_transcribes_a_recording_and_reads_an_attached_email(world):
    memo(world)
    email, f = await one(world, "memo.m4a", b"\x00\x00\x00\x20ftypM4A " + b"0" * 60, "audio")
    reading, text, _d = await ma.whole_text(ctx_for(world), email, f)
    assert text == "Call me back about the lease." and "Transcribed on this Mac" in reading.note
    eml_world(world, [inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
    emails, _ = await ma.find()
    reading, text, _d = await ma.whole_text(ctx_for(world), emails[0], emails[0].files[0])
    assert text.startswith("From: Dana") and "alpha inner words" not in text and reading.note.startswith("An email attached")


# ---------------------------------------------------------------- note_seen without the store's help

async def test_a_store_without_note_seen_changes_nothing(world, monkeypatch):
    monkeypatch.delattr(attachment_store, "note_seen")
    world.apple_rows = [row(name="notes.txt", rfc="n@x.com")]
    world.local_files[(1, "1.2")] = b"remember the milk"
    assert "remember the milk" in await ma.run()


async def test_a_note_seen_that_raises_changes_nothing(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(attachment_store, "note_seen", boom)
    world.apple_rows = [row(name="notes.txt", rfc="n@x.com")]
    world.local_files[(1, "1.2")] = b"remember the milk"
    assert "remember the milk" in await ma.run()
    assert attachment_store.get(attachment_store.digest(b"remember the milk"))


# ---------------------------------------------------------------- repairs RD1: attached emails

async def test_an_attached_email_that_was_read_in_full_says_how_many_files_it_read(world):
    eml_world(world, [inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
    out = await ma.run()
    assert "Read 1 of its 1 file." in out and "Only part of it was read" not in out
    assert "Read the whole file." in out


def outer_coverage(out: str) -> str:
    """The attached email's own Coverage line (the first one; the files read inside it come after)."""
    return out.split("Coverage:")[1].splitlines()[0]


@pytest.mark.parametrize("flag,sentence", [
    ({"body_cut": True}, "Its text was cut short."),
    ({"walk_cut": True}, "Some of its parts were too deep or too many to read."),
    ({"more_files": 3}, "3 more files not listed."),
])
async def test_an_attached_email_with_anything_dropped_never_says_read_the_whole_file(world, flag, sentence):
    eml_world(world, [inner_file(0, "a.txt", "text/plain", b"alpha inner words")])
    world.eml["Fwd.eml"].update(flag)
    out = await ma.run()
    assert sentence in out and "Only part of it was read." in out
    assert "Read the whole file." not in outer_coverage(out)
    assert "Read 1 of its " in out


async def test_an_attached_email_with_a_file_it_did_not_read_is_partial(world):
    files = [inner_file(0, "a.txt", "text/plain", b"alpha inner words"),
             inner_file(1, "original.eml", "message/rfc822", EML)]
    eml_world(world, files)
    out = await ma.run()
    assert "Read 1 of its 2 files." in out and "Only part of it was read." in out
    assert "Read the whole file." not in outer_coverage(out)


async def test_an_attached_email_whose_inner_file_fails_counts_it_as_not_read(world):
    files = [inner_file(0, "a.txt", "text/plain", b"alpha inner words"), inner_file(1, "b.txt", "text/plain", b"")]
    eml_world(world, files)
    out = await ma.run()
    assert "Read 1 of its 2 files." in out and "Read the whole file." not in outer_coverage(out)


def test_a_walk_cut_that_is_absent_means_false():
    info = eml_info([])
    assert ma._parse_eml(info).walk_cut is False
    assert ma._parse_eml({**info, "walk_cut": True}).walk_cut is True


def test_the_docstring_rule_matches_the_note():
    assert "nothing inside it is written to disk" not in ma.__doc__
    assert "kept like any other reading" in ma.__doc__
    assert ma._EML_NOTE.startswith("An email attached to an email. Its files were read in memory; none was run")


# ---------------------------------------------------------------- repairs RD1: index_one

async def test_index_one_never_reaches_gmail_for_a_merged_email_whose_local_read_fails(world, monkeypatch):
    from service import gmail_source
    world.apple_rows = [row(rfc="a@x.com")]
    world.gmail = {"me@gmail.com": [gmsg(rfc="<a@x.com>")]}
    world.gmail_bytes["g-report.pdf"] = PDF + b" gmail words"
    world.local_files[(1, "1.2")] = "unreadable"
    world.by_rfc["a@x.com"] = gmsg(rfc="<a@x.com>")

    async def explode(*a, **k):
        world.calls.append(("gbytes", "called"))
        raise AssertionError("Gmail was reached from index_one")
    monkeypatch.setattr(gmail_source, "attachment_bytes", explode)
    emails, _ = await ma.find()
    f = emails[0].files[0]
    assert f.has_gmail and f.has_local                     # a merged email, both references
    assert await ma.index_one(emails[0], f, deadline=ma.time.monotonic() + 60) == "unreadable"
    assert world.kinds("gbytes") == [] and world.kinds("gfind") == []
    assert f.has_gmail                                      # the caller's file is untouched


async def test_index_one_skips_a_messages_file_without_any_bridge_call(world):
    world.message_rows = [message_row("11", "lease.pdf")]
    world.message_files["11"] = PDF + b" the rent is 2100"
    emails, _ = await ma.find(source="messages")
    f = emails[0].files[0]
    assert f.has_messages
    assert await ma.index_one(emails[0], f, deadline=ma.time.monotonic() + 60) == "skipped"
    assert world.kinds("mread") == [] and world.kinds("local") == []
    # also a file only marked as texted, with no id
    g = ma._File("x.pdf", source="messages", on_disk=True)
    assert await ma.index_one(emails[0], g, deadline=ma.time.monotonic() + 60) == "skipped"


async def test_index_one_skips_a_file_with_no_local_copy_on_disk(world):
    world.apple_rows = [row(on_disk=False)]
    emails, _ = await ma.find()
    f = emails[0].files[0]
    assert await ma.index_one(emails[0], f, deadline=ma.time.monotonic() + 60) == "skipped"
    assert world.kinds("local") == [] and world.kinds("gbytes") == []
    bare = ma._File("y.pdf")
    assert await ma.index_one(emails[0], bare, deadline=ma.time.monotonic() + 60) == "skipped"


# ---------------------------------------------------------------- repairs RD1: text recognition rest

async def test_a_background_failure_of_recognition_sets_no_rest(world, monkeypatch):
    world.app = True

    async def silent(jpeg, timeout=20.0):
        raise bridge.BridgeError("app_not_connected")
    monkeypatch.setattr(bridge, "recognize_text", silent)
    big = PNG + b"0" * 30_000
    email, f = await one(world, "receipt.png", big)
    assert await ma.index_one(email, f, deadline=ma.time.monotonic() + 60) == "skipped"
    assert ma._ocr_resting_until == 0.0
    # the same failure in the foreground does set it
    ctx = ma._Ctx(deadline=ma.time.monotonic() + 120, gmail_source=None, gmail_accounts=[], passes=None)
    assert await ma._recognised(ctx, "receipt.png", big, paged=False) is None
    assert ma._ocr_resting_until > ma.time.monotonic()


async def test_a_picture_met_while_recognition_rests_is_resting_not_skipped_and_nothing_is_stored(world, monkeypatch):
    world.app = True
    monkeypatch.setattr(ma, "_ocr_resting_until", ma.time.monotonic() + 200)
    big = PNG + b"0" * 30_000
    email, f = await one(world, "receipt.png", big)
    assert await ma.index_one(email, f, deadline=ma.time.monotonic() + 60) == "resting"
    assert attachment_store.get(attachment_store.digest(big)) is None
    assert world.kinds("recognise") == []
    # the rest is over: the same file is now read
    monkeypatch.setattr(ma, "_ocr_resting_until", 0.0)
    assert await ma.index_one(email, f, deadline=ma.time.monotonic() + 60) == "kept"


async def test_the_foreground_still_obeys_the_rest(world, monkeypatch):
    world.app = True
    monkeypatch.setattr(ma, "_ocr_resting_until", ma.time.monotonic() + 200)
    ctx = ma._Ctx(deadline=ma.time.monotonic() + 120, gmail_source=None, gmail_accounts=[], passes=None)
    assert await ma._recognised(ctx, "receipt.png", PNG + b"0" * 30_000, paged=False) is None
    assert ctx.resting is False and world.kinds("recognise") == []


# ---------------------------------------------------------------- repairs RD1: Messages contact words

def contacts(monkeypatch, **people):
    monkeypatch.setattr(imessage_tools, "_name_handles", {k.replace("_", " "): list(v) for k, v in people.items()})
    monkeypatch.setattr(imessage_tools, "_contacts", {})


async def test_contact_words_turned_into_senders_are_said(world, monkeypatch):
    contacts(monkeypatch, dana_lee=["+15551230000"])
    world.message_rows = [message_row("11", "lease.pdf")]
    out = await ma.run(query="dana lease", source="messages", list_only=True)
    assert "Looked for files sent by Dana Lee." in out
    assert world.message_index_args[0][:2] == (["lease"], ["+15551230000"])
    _found, notes = await ma.find(query="dana lease", source="messages")
    assert "Looked for files sent by Dana Lee." in notes


async def test_several_contact_names_are_summarised(world, monkeypatch):
    contacts(monkeypatch, dana_lee=["+1"], sam_roe=["+2"], pat_kim=["+3"], lou_may=["+4"])
    world.message_rows = [message_row("11", "lease.pdf")]
    out = await ma.run(query="dana sam pat lou", source="messages", list_only=True)
    assert "Looked for files sent by Dana Lee, Sam Roe and 2 others." in out
    out = await ma.run(query="dana sam", source="messages", list_only=True)
    assert "Looked for files sent by Dana Lee and Sam Roe." in out


async def test_nothing_from_the_contact_retries_once_with_the_word_as_a_term(world, monkeypatch):
    contacts(monkeypatch, dana_lee=["+15551230000"])
    calls = []

    async def index(terms, handles, since, until, limit=200, timeout=10.0):
        calls.append((list(terms), list(handles)))
        if handles:
            return [], False
        return [message_row("11", "dana-lease.pdf", chat="Dana")], False
    monkeypatch.setattr(bridge, "read_message_attachment_index", index)
    out = await ma.run(query="dana", source="messages", list_only=True)
    assert calls == [([], ["+15551230000"]), (["dana"], [])]
    assert "Nothing from Dana Lee, so looked for the word instead." in out
    assert "dana-lease.pdf" in out and "Looked for files sent by" not in out


async def test_no_retry_when_the_first_search_found_something_or_failed(world, monkeypatch):
    contacts(monkeypatch, dana_lee=["+15551230000"])
    calls = []

    async def index(terms, handles, since, until, limit=200, timeout=10.0):
        calls.append(list(handles))
        return [message_row("11")], False
    monkeypatch.setattr(bridge, "read_message_attachment_index", index)
    await ma.run(query="dana", source="messages", list_only=True)
    assert calls == [["+15551230000"]]
    calls.clear()

    async def broken(terms, handles, since, until, limit=200, timeout=10.0):
        calls.append(list(handles))
        raise bridge.BridgeError("app_not_connected")
    monkeypatch.setattr(bridge, "read_message_attachment_index", broken)
    out = await ma.run(query="dana", source="messages", list_only=True)
    assert len(calls) == 1 and "Nothing from" not in out


def test_only_a_full_name_or_a_first_name_of_three_letters_that_is_not_an_everyday_word_is_a_sender(monkeypatch):
    contacts(monkeypatch, al_jones=["+1"], dana_lee=["+2"], sent_smith=["+3"], mom=["+4"])
    assert ma._contact_split(["al", "plan"]) == (["al", "plan"], [])               # first name under 3 letters
    assert ma._contact_split(["lee", "plan"]) == (["lee", "plan"], [])             # a family name alone
    assert ma._contact_split(["dana", "lee"]) == ([], ["+2"])                       # first name then family name
    assert ma._contact_split(["sent", "plan"]) == (["sent", "plan"], [])           # an everyday word
    assert ma._contact_split(["mom", "plan"]) == (["plan"], ["+4"])                 # a whole name
    assert ma._contact_split(["dan", "plan"]) == (["dan", "plan"], [])


# ---------------------------------------------------------------- repairs RD1: smaller

async def test_the_childs_error_sentence_goes_through_the_same_filter_as_a_file_problem(world):
    memo(world)
    world.listen_error = ValueError("secret quoted from the file: Dear Bob, the password is hunter2")
    out = await ma.run()
    assert "hunter2" not in out and "Not read: It couldn't be listened to." in out


# ---------------------------------------------------------------- repairs S5: a real .eml through the real child

def real_eml_world(world, files, name="Fwd.eml", body="Please see the lease terms.", **extra):
    """An attached email built with the stdlib `email` package and parsed by the REAL child (`eml`,
    `eml_part`); `files` is [(name, mime, bytes)]. `extra` is merged into the child's `eml` answer."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = "Dana <dana@x.com>", "me@x.com", "Lease", "Mon, 9 Nov 2026"
    msg.set_content(body)
    for file_name, mime, data in files:
        main, _, sub = mime.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=file_name)
    raw = msg.as_bytes()
    world.apple_rows = [row(name=name, rfc="f@x.com")]
    world.local_files[(1, "1.2")] = raw
    world.eml[name] = {**attachment_child.eml(name, raw), **extra}
    for index in range(len(files)):
        world.parts[(name, index)] = attachment_child.eml_part(name, raw, index)
    return raw


async def test_an_inspect_only_inner_file_is_counted_as_inspected_not_read(world):
    real_eml_world(world, [("a.txt", "text/plain", b"alpha inner words"), ("setup.exe", "application/octet-stream", MZ)])
    out = await ma.run()
    assert "Read 1 of its 2 files; 1 only inspected." in out and "Read 2 of its 2" not in out
    assert "Inspected only. Wisp never opens, unpacks or runs programs" in out
    assert "alpha inner words" in out
    assert "Read the whole file." not in outer_coverage(out)


async def test_the_inner_files_header_does_not_claim_they_were_left_unkept(world):
    real_eml_world(world, [("a.txt", "text/plain", b"alpha inner words")])
    out = await ma.run()
    assert "  Files inside it that Wisp read (none was saved as a file or run):" in out
    assert "in memory only" not in out
    assert "Read 1 of its 1 file." in out and "only inspected" not in out


@pytest.mark.parametrize("extra", [{"walk_cut": True}, {"more_files": 2}, {"more_unknown": True}])
async def test_a_background_kept_email_is_stored_as_partial_when_the_child_dropped_anything(world, extra):
    import json
    real_eml_world(world, [("a.txt", "text/plain", b"alpha inner words")], **extra)
    emails, _ = await ma.find()
    assert await ma.index_one(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 60) == "kept"
    stored = attachment_store.get(attachment_store.digest(world.local_files[(1, "1.2")]))
    assert json.loads(stored.note)["partial"] is True


async def test_a_background_kept_email_with_nothing_dropped_is_not_partial_and_says_its_files_were_not_read(world):
    import json
    real_eml_world(world, [("a.txt", "text/plain", b"alpha inner words")])
    emails, _ = await ma.find()
    assert await ma.index_one(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 60) == "kept"
    stored = attachment_store.get(attachment_store.digest(world.local_files[(1, "1.2")]))
    meta = json.loads(stored.note)
    assert meta["partial"] is True              # a listed file was never read on this path
    assert "the files inside it were not read" in meta["cover"] and "read in memory" not in meta["cover"]


async def test_a_background_kept_email_with_no_files_inside_is_not_partial(world):
    import json
    real_eml_world(world, [])
    emails, _ = await ma.find()
    assert await ma.index_one(emails[0], emails[0].files[0], deadline=ma.time.monotonic() + 60) == "kept"
    stored = attachment_store.get(attachment_store.digest(world.local_files[(1, "1.2")]))
    assert json.loads(stored.note)["partial"] is False


async def test_an_email_whose_files_may_not_all_be_seen_never_prints_an_exact_total(world):
    real_eml_world(world, [("a.txt", "text/plain", b"alpha inner words")], more_unknown=True)
    out = await ma.run()
    assert "There may be more files inside that Wisp couldn't see." in out
    assert "Read 1 of at least 1 file." in out and "Read 1 of its 1" not in out
    assert "Files in this email (at least 1):" in out
    assert "Read the whole file." not in outer_coverage(out) and "Only part of it was read." in out


async def test_a_messages_retry_gets_the_search_budget_for_both_searches_together(world, monkeypatch):
    import asyncio
    contacts(monkeypatch, dana_lee=["+15551230000"])
    monkeypatch.setattr(ma, "_MESSAGES_SEARCH_S", 2.0, raising=False)
    calls = []

    async def index(terms, handles, since, until, limit=200, timeout=10.0):
        calls.append(list(handles))
        if handles:
            await asyncio.sleep(0.4)
            return [], False
        await asyncio.sleep(6)                  # the retry hangs
        return [message_row("11")], False
    monkeypatch.setattr(bridge, "read_message_attachment_index", index)
    began = ma.time.monotonic()
    out = await ma.run(query="dana", source="messages", list_only=True)
    spent = ma.time.monotonic() - began
    assert 1.8 < spent < 2.4                    # the second pass got exactly what remained, no more
    assert len(calls) == 2 and "took too long" in out


async def test_a_messages_retry_is_skipped_when_under_a_second_remains(world, monkeypatch):
    import asyncio
    contacts(monkeypatch, dana_lee=["+15551230000"])
    monkeypatch.setattr(ma, "_MESSAGES_SEARCH_S", 1.0, raising=False)
    calls = []

    async def index(terms, handles, since, until, limit=200, timeout=10.0):
        calls.append(list(handles))
        await asyncio.sleep(0.4)
        return [], False
    monkeypatch.setattr(bridge, "read_message_attachment_index", index)
    await ma.run(query="dana", source="messages", list_only=True)
    assert len(calls) == 1                      # 0.6 s left: no second pass


def test_the_search_budget_for_messages_is_twelve_seconds():
    assert ma._MESSAGES_SEARCH_S == 12.0
