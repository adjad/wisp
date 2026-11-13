"""delete_note (exact title, confirmed every time, verified by read-back) and
real list items for "- " / "1. " lines in create_note and append_note.

Notes.app is never touched: notes_write._osa is replaced by a fake that
answers each fixed script from a small in-memory list of notes."""
from __future__ import annotations

import subprocess

import pytest

import service.main  # noqa: F401 — registers every tool
from service.safety import grants, policy
from service.tools import notes_write
from service.tools.registry import REGISTRY, classify_tool_outcome


class FakeNotes:
    """In-memory notes: dicts with id, folder id, folder name and title."""

    def __init__(self, notes, *, delete_works=True, verify_fails=False):
        self.notes = [dict(n) for n in notes]
        self.delete_works = delete_works
        self.verify_fails = verify_fails
        self.scripts = []

    @staticmethod
    def _quoted(script, after):
        start = script.index(after) + len(after)
        return script[start:script.index('"', start)]

    def __call__(self, script):
        self.scripts.append(script)
        ok = lambda out: subprocess.CompletedProcess(["osascript"], 0, out, "")  # noqa: E731
        if "delete note id" in script:
            nid = self._quoted(script, 'delete note id "')
            if self.delete_works:
                for n in self.notes:
                    if n["id"] == nid:
                        n["fid"], n["folder"] = "f-trash", "Recently Deleted"
            return ok("OK\n")
        if '"LEFT:"' in script:
            if self.verify_fails:
                return subprocess.CompletedProcess(["osascript"], 1, "", "synthetic Notes failure")
            nid = self._quoted(script, 'exists note id "')
            fid = self._quoted(script, 'folder id "')
            title = self._quoted(script, 'whose name is "')
            if any(n["id"] == nid and n["fid"] == fid for n in self.notes):
                return ok("STILL\n")
            left = [n["title"] for n in self.notes
                    if n["fid"] == fid and n["title"].lower() == title.lower()]
            return ok("LEFT:" + "".join(t + "\n" for t in left))
        if '"HITS:"' in script:
            title = self._quoted(script, 'whose name is "')
            if 'exists folder "' in script:
                folder = self._quoted(script, 'exists folder "')
                if not any(n["folder"] == folder for n in self.notes):
                    return ok("NOFOLDER\n")
                pool = [n for n in self.notes if n["folder"] == folder]
            else:
                pool = [n for n in self.notes if n["folder"] != "Recently Deleted"]
            # Notes' `is` compares without case, like AppleScript.
            rows = [n for n in pool if n["title"].lower() == title.lower()]
            return ok("HITS:" + "".join(f"{n['id']}\t{n['fid']}\t{n['folder']}\t{n['title']}\n"
                                        for n in rows))
        if "make new note" in script or "set body of target" in script:
            return ok("OK\n")
        raise AssertionError(f"unexpected script: {script}")


NOTES = [
    {"id": "n1", "fid": "f1", "folder": "Notes", "title": "Packing list"},
    {"id": "n2", "fid": "f1", "folder": "Notes", "title": "Packing list for Rome"},
    {"id": "n3", "fid": "f2", "folder": "Work", "title": "Ideas"},
    {"id": "n4", "fid": "f1", "folder": "Notes", "title": "Ideas"},
]


@pytest.fixture
def fake(monkeypatch):
    def install(notes=NOTES, **kw):
        f = FakeNotes(notes, **kw)
        monkeypatch.setattr(notes_write, "_osa", f)
        return f
    return install


# --------------------------------------------------------------- delete_note

def test_exact_title_is_deleted_and_verified(fake):
    f = fake()
    out = notes_write.delete_note("packing list")
    assert out == ("Deleted the note “Packing list” from Notes. "
                   "Notes keeps it in Recently Deleted for 30 days.")
    # Only the exact title went; the longer "Packing list for Rome" stayed.
    gone = {n["id"] for n in f.notes if n["folder"] == "Recently Deleted"}
    assert gone == {"n1"}
    assert classify_tool_outcome("delete_note", out).status == "succeeded"
    assert any("delete note id \"n1\"" in s for s in f.scripts)


def test_a_partial_title_deletes_nothing(fake):
    f = fake()
    out = notes_write.delete_note("Packing")
    assert "No note is titled exactly 'Packing'" in out and "Nothing was deleted" in out
    assert type(out).__name__ == "_NotDoneResult"
    assert not any("delete note id" in s for s in f.scripts)


def test_zero_matches_is_refused(fake):
    f = fake()
    out = notes_write.delete_note("Groceries")
    assert type(out).__name__ == "_NotDoneResult" and "Nothing was deleted" in out
    assert classify_tool_outcome("delete_note", out).status == "failed"
    assert not any("delete note id" in s for s in f.scripts)


def test_two_matches_are_refused_and_listed(fake):
    f = fake()
    out = notes_write.delete_note("Ideas")
    assert type(out).__name__ == "_NotDoneResult"
    assert "nothing was deleted" in out
    assert "  - Ideas (folder: Work)" in out and "  - Ideas (folder: Notes)" in out
    assert not any("delete note id" in s for s in f.scripts)
    assert all(n["folder"] != "Recently Deleted" for n in f.notes)


def test_a_folder_narrows_two_matches_to_one(fake):
    f = fake()
    out = notes_write.delete_note("Ideas", folder="Work")
    assert out.startswith("Deleted the note “Ideas” from Work.")
    assert [n["id"] for n in f.notes if n["folder"] == "Recently Deleted"] == ["n3"]


def test_a_missing_folder_is_refused(fake):
    f = fake()
    out = notes_write.delete_note("Ideas", folder="Nope")
    assert type(out).__name__ == "_NotDoneResult" and "no Notes folder called 'Nope'" in out
    assert not any("delete note id" in s for s in f.scripts)


def test_read_back_still_finding_the_note_is_not_success(fake):
    fake(delete_works=False)
    out = notes_write.delete_note("Packing list")
    assert out.startswith("(couldn't confirm the delete")
    # Not a marked refusal: the delete script ran, so the outcome is unknown.
    assert type(out) is str
    assert classify_tool_outcome("delete_note", out).status != "succeeded"


def test_read_back_that_does_not_answer_is_not_success(fake):
    fake(verify_fails=True)
    out = notes_write.delete_note("Packing list")
    assert out.startswith("(couldn't confirm the delete") and type(out) is str
    assert classify_tool_outcome("delete_note", out).status != "succeeded"


def test_a_note_titled_like_the_old_one_still_in_the_folder_is_not_success(fake):
    # The deleted note left, but another note with the same title is still there.
    f = fake([NOTES[0]])

    def add_twin(script, _orig=f.__call__):
        result = _orig(script)
        if "delete note id" in script:
            f.notes.append({"id": "n9", "fid": "f1", "folder": "Notes", "title": "Packing list"})
        return result
    notes_write._osa = add_twin
    out = notes_write.delete_note("Packing list")
    assert out.startswith("(couldn't confirm the delete")


def test_the_card_names_the_note_and_its_folder(fake):
    fake()
    bound, reason = notes_write.delete_note_card({"title": "packing list"})
    assert bound == {"title": "packing list", "folder": "Notes"}
    assert reason == ("deletes the note “Packing list” from the “Notes” folder. Notes keeps it "
                      "in Recently Deleted for 30 days — always confirmed")
    assert notes_write.delete_note_card({"title": "Ideas"}) is None
    assert notes_write.delete_note_card({"title": "Groceries"}) is None


# ------------------------------------------------------- confirm and grants

@pytest.fixture
def isolated_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})


ARGS = {"title": "Packing list", "folder": "Notes"}


def _mode(monkeypatch, mode):
    monkeypatch.setattr(policy, "_FULL_ACCESS", mode == "full")
    monkeypatch.setattr(policy, "_READ_ONLY", mode == "view")


@pytest.mark.parametrize("mode", ["default", "full"])
def test_delete_note_is_always_confirmed(monkeypatch, isolated_grants, mode):
    _mode(monkeypatch, mode)
    decision = policy.decide(REGISTRY["delete_note"].category, ARGS, tool="delete_note")
    assert decision.tier is policy.Tier.CONFIRM
    assert decision.reason == ("deletes the note “Packing list” from the “Notes” folder. Notes "
                               "keeps it in Recently Deleted for 30 days — always confirmed")


def test_delete_note_is_refused_in_view_only(monkeypatch, isolated_grants):
    _mode(monkeypatch, "view")
    decision = policy.decide(REGISTRY["delete_note"].category, ARGS, tool="delete_note")
    assert decision.tier is policy.Tier.DENY


def test_delete_note_is_never_grantable(monkeypatch, isolated_grants):
    _mode(monkeypatch, "full")
    assert not grants.is_grantable("delete_note", ARGS)
    assert grants.grant("delete_note", ARGS, decision="allow").get("ok") is False
    assert "delete_note" in grants.never_grantable()
    # A stale allow written by hand is not honoured either.
    grants._CACHE["delete_note"] = {"allow": [{"scope": ""}], "deny": []}
    decision = policy.decide(REGISTRY["delete_note"].category, ARGS, tool="delete_note")
    assert decision.tier is policy.Tier.CONFIRM


def test_a_deny_grant_still_blocks_delete_note(monkeypatch, isolated_grants):
    _mode(monkeypatch, "full")
    assert grants.grant("delete_note", ARGS, decision="deny", scoped=False).get("ok") is not False
    assert policy.decide(REGISTRY["delete_note"].category, ARGS, tool="delete_note").tier is policy.Tier.DENY


# ------------------------------------------------------------- bullet HTML

def test_body_bullets_become_list_items():
    html = notes_write._body_html("Intro\n- Demo point 1\n* Demo point 2\n• Demo <3>\n1. First\n2) Second\nBye")
    assert html == ("Intro<ul><li>Demo point 1</li><li>Demo point 2</li><li>Demo &lt;3&gt;</li></ul>"
                    "<ol><li>First</li><li>Second</li></ol>Bye")


def test_ordinary_lines_are_unchanged():
    assert notes_write._body_html("a\n\nb & c\n-5 degrees") == "a<br><br>b &amp; c<br>-5 degrees"


def test_create_note_sends_list_items(fake):
    f = fake()
    out = notes_write.create_note("Demo", "- Demo point 1\n- Demo point 2")
    assert out.startswith("Created the note")
    script = f.scripts[-1]
    assert "<ul><li>Demo point 1</li><li>Demo point 2</li></ul>" in script
    assert "- Demo" not in script


def test_checklist_lines_lose_their_marker(fake):
    f = fake()
    notes_write.create_note("Shop", "- milk\n* eggs", checklist=True)
    assert "<ul><li>milk</li><li>eggs</li></ul>" in f.scripts[-1]


def test_append_note_sends_list_items(fake):
    f = fake()

    def answer(script):
        f.scripts.append(script)
        return subprocess.CompletedProcess(["osascript"], 0, "OK:Packing list\n", "")
    notes_write._osa = answer
    assert notes_write.append_note("Packing", "- socks\n- hat") == "Added it to \"Packing list\"."
    assert "<div><ul><li>socks</li><li>hat</li></ul></div>" in f.scripts[-1]
    assert "- socks" not in f.scripts[-1]
