"""Round-4 family ``restraint_r4``: contrast pairs for the calls r3 made too eagerly.

r3 (Mac gate 2026-11-02) raised unsafe calls from 15 to 20. Six of them were not sends but
over-reach in four patterns; every scenario here is the CORRECT behaviour for one pattern, in
wordings that are not the toolbench prompts (files_sort_downloads_by_type, files_debug_logs_by_date,
rem_keep_doordash, cal_move_refused_organizer, safe_post_json_webhook, web_jacket_tmrow_no_city).

1. Files
   - "sort / organize / clean up / group / separate by date or type" is ``organize_files``: a preview
     first (no card, nothing moves), then the same call with confirm=true and the preview's token
     (the card). Never a run of ``move_path`` calls.
   - "show / list / what's in / is there a ..." is ``list_dir`` or ``find_files``, never a move.
   - Positive control: one explicit "move report.pdf to Documents" is one ``move_path``.
   - The preview can find nothing to move, a dated log set can leave an undated file untouched,
     and a name clash is skipped; the final says so plainly.
2. Ruled out
   - "keep it / leave that one / not that one" after an offer to clear is no clear and no delete.
   - "clear the rest but keep X": ``clear_reminders`` / ``clear_past_reminders`` can only narrow by
     INCLUDING a word, never skip one, so the ideal reads the list and removes the others one by one
     (``cancel_event``, a card each; ``manage_timers`` cancel for timers), or asks when two items
     fit the name it was given.
   - A move the host refuses (not the organizer, read-only calendar, invitees not synced, no match)
     is explained plainly and never worked around by adding a copy.
3. Outbound
   - ``web_fetch`` is a GET of a URL the person typed. A request to POST, PUT or ping a webhook,
     submit a form, upload, or post somewhere is declined in one gentle line with what Wisp can do
     instead. Things Wisp has no tool for (lights, rides, social posts) are declined the same way.
   - Positive controls: fetch a typed page, search the web, and send the same content to a person.
4. Location
   - Weather with no city in the prompt and no home city in the profile or memory: ask which city,
     no ``get_weather`` call (sometimes after a ``recall`` that finds nothing).
   - Positive controls: the city is in the prompt, or the profile / memory has a home city, so
     ``get_weather`` runs with it.

About a third of the turns are positive controls where the same action IS right, so the model does
not learn refusal. Seeded earlier turns (the "keep it" replies) need ``restraint_r4`` in
``writes.MULTITURN``.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import fnmatch
import hashlib
import json
import random
import re
from pathlib import PurePosixPath

import gen
import harness as H
import leak
import scen_r2_act as A
from gen import D
from harness import Call, Final
from replies import clock, join
from scen import Scenario, chance, setup
from scen_r2_args import add_event, at, dlabel, iso, t_txt
from scen_r3_followup import turn
from scen_r3_safe import _fx, _move_world, _reads, _timers_world, fin, pick
from world import Reminder

FAMILY = "restraint_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset({"organize_files", "move_path", "cancel_event", "manage_timers", "update_event",
                                     "clear_past_reminders", "send_email", "send_message"})}

DROPPED: list[tuple[str, str, str]] = []
_SEEN: set[str] = set()
HOME = "/Users/sam"


# =============================================================================
# plumbing: dry-run every candidate the way build.py will, never repeat a prompt
# =============================================================================
def _check(sc):
    ok, why = A._check(sc)
    if not ok and getattr(sc, "no_effect", False) and why.startswith("write ") and why.endswith("had no effect"):
        ok = True      # the host refuses the write: that is the behaviour being taught
    if not ok:
        return ok, why
    for t in getattr(sc, "history", None) or ():
        hits = leak.fact_hits(t["user"] + "\n" + t["assistant"])
        if hits:
            return False, f"toolbench fact in history {hits[:2]}"
        leaked, score, q = A._leaks().leaks(t["user"])
        if leaked:
            return False, f"history prompt leaks {q['id']} {score:.2f}"
    return True, ""


def _attempt(make, rng, tries=6):
    for _ in range(tries):
        seed = rng.random()
        sc = make(random.Random(seed))
        if sc is None or sc.prompt.strip().lower() in _SEEN:
            continue
        ok, why = _check(sc)
        if ok:
            _SEEN.add(sc.prompt.strip().lower())
            return make(random.Random(seed))
        DROPPED.append((FAMILY, sc.prompt, why))
    return None


def mk(prompt, now, w, steps, notes, *, cards=(), approve=None, must_see=(), history=None, no_effect=False, profile=None):
    if history:
        w.history = history
    sc = A.mk(FAMILY, prompt, now, w, steps, notes, approve=approve, cards=list(cards), must_see=must_see, profile=profile)
    sc.history = history
    sc.no_effect = no_effect
    return sc


def cap1(s):
    return s[:1].upper() + s[1:]


def style(rng, text):
    return A.style(rng, text)


# =============================================================================
# 1. FILES: a world with real-looking folders, and organize_files behind it
# =============================================================================
SIZES = ["38KB", "118KB", "210KB", "612KB", "1.2MB", "3.1MB", "8.4MB", "44MB"]
PDFS = ["syllabus_cs162.pdf", "tuition_receipt.pdf", "housing_form.pdf", "lab4_handout.pdf", "flight_ticket.pdf",
        "parking_permit.pdf", "reading_ch7.pdf", "scholarship_letter.pdf", "w9_form.pdf", "lease_addendum.pdf"]
IMGS = ["IMG_2204.png", "map_capture.png", "poster_final.png", "headshot.jpg", "IMG_3317.jpg", "diagram_v3.png",
        "whiteboard.jpg", "logo_draft.png"]
ZIPS = ["project_starter.zip", "photos_oct.zip", "dataset_small.zip", "fonts_pack.zip"]
DOCS = ["essay_draft.docx", "cover_letter.docx", "meeting_notes.docx", "resume_v7.docx"]
SHEETS = ["budget_oct.xlsx", "grades_export.csv", "expenses.csv", "lab_data.xlsx"]
MISC = ["Zoom.pkg", "Slack.dmg", "podcast_ep12.mp3", "demo_clip.mov", "notes.txt", "todo.md"]


def _entries(rng, names):
    return [(n, rng.choice(SIZES)) for n in names]


def _mixed(rng, k=None):
    names = (rng.sample(PDFS, 2) + rng.sample(IMGS, 2) + rng.sample(ZIPS, 1) + rng.sample(DOCS + SHEETS, 1)
             + rng.sample(MISC, 1))
    return rng.sample(names, k or len(names))


def _tilde(p):
    return "~" + p[len(HOME):] if p.startswith(HOME) else p


def _abs(p):
    return HOME + p[1:] if p.startswith("~") else p


def _group(name, group_by):
    if group_by == "extension":
        ext = PurePosixPath(name).suffix.lstrip(".")
        return ext.upper() if ext else "NO_EXTENSION"
    if group_by == "debug_date":
        m = re.match(r"^(?:wisp[-_])?debug[-_](\d{4}-\d{2}-\d{2})(?=[_.-]|$)", name, re.I)
        return m.group(1) if m else None
    return ""


def fresh_files(rng, **folders):
    """Four folders; the named ones are filled with the given names, the rest get a few quiet files."""
    quiet = {"~/Documents": ["thesis_outline.docx", "Ethics essay draft.docx", "SamRivera_Resume_2026.pdf"],
             "~/Desktop": ["todo.txt", "wallpaper.jpg"], "~/Pictures": ["Trip 2026", "headshot.jpg"],
             "~/Downloads": ["installer.dmg"]}
    out = {}
    for f, names in quiet.items():
        out[f] = _entries(rng, folders.get(f, names))
    return out


class Org:
    """organize_files for one scenario, in the real tool's words. The token is fixed ahead of
    the run so the scripted confirm can carry it."""

    def __init__(self, w, seed):
        self.w, self.seed = w, seed
        self.token = base64.urlsafe_b64encode(hashlib.sha256(f"tok{seed}".encode()).digest()[:24]).decode().rstrip("=")
        self.prev = None
        w.overrides["organize_files"] = self.handle

    def digest(self, path):
        return hashlib.sha256(f"{self.seed}:{path}".encode()).hexdigest()

    @staticmethod
    def key(a):
        return (a.get("pattern"), (a.get("folder") or "").rstrip("/"), a.get("destination"), a.get("group_by") or "")

    def plan(self, a):
        folder = a["folder"].rstrip("/")
        entries = self.w.files.get(folder)
        if entries is None:
            return None
        src = _abs(folder)
        dest = a["destination"]
        tgt = src if dest == "." else (_abs(dest).rstrip("/") if dest.startswith(("~", "/")) else f"{src}/{dest}")
        gb = a.get("group_by") or ""
        moves, skipped = [], []
        for name, _ in sorted(entries):
            if not fnmatch.fnmatchcase(name, a["pattern"]):
                continue
            g = _group(name, gb)
            if g is None:
                skipped.append((f"{src}/{name}", "no valid debug date in filename; left untouched"))
                continue
            parent = f"{tgt}/{g}" if g else tgt
            clash = any(n == name for n, _ in self.w.files.get(_tilde(parent), []))
            moves.append({"name": name, "src": f"{src}/{name}", "dst": f"{parent}/{name}", "pkey": _tilde(parent),
                          "clash": clash, "group": g})
        return {"src": src, "tgt": tgt, "moves": moves, "skipped": skipped}

    def preview_body(self, p):
        lines = [f"Move {len(p['moves'])} file(s)", f"From: {p['src']}", f"To: {p['tgt']}"]
        lines += [f"{json.dumps(m['src'])} -> {json.dumps(m['dst'])} (SHA-256: {self.digest(m['src'])})" for m in p["moves"]]
        if p["skipped"]:
            lines.append("Left untouched:")
            lines += [f"{json.dumps(path)}: {why}" for path, why in p["skipped"]]
        clashes = [m["dst"] for m in p["moves"] if m["clash"]]
        if clashes:
            lines.append("Existing destinations will be skipped; nothing will replace them:")
            lines += [json.dumps(c) for c in clashes]
        lines.append("Exact-byte moves require source entries to stay stable through execution. A late source "
                     "replacement may be moved and reported unknown; never retry unknown results automatically.")
        return "\n".join(lines)

    def handle(self, a):
        confirm = bool(a.get("confirm"))
        if confirm and (a.get("preview_token") != self.token or self.prev != self.key(a)):
            return "(error: missing, expired or changed file preview. Preview again before moving any files.)"
        p = self.plan(a)
        if p is None:
            return f"(error: no such directory: {a['folder']}. No files moved.)"
        if not p["moves"]:
            note = ("\nLeft untouched:\n" + "\n".join(f"{json.dumps(path)}: {why}" for path, why in p["skipped"])
                    if p["skipped"] else "")
            return f"No files in {a['folder']} match the requested move classification.{note}"
        if not confirm:
            self.prev = self.key(a)
            return (f"Would move {len(p['moves'])} file(s); preview only, no filesystem changes.\n{self.preview_body(p)}"
                    f"\n\nPreview token: {self.token}\nCall again with the same inputs, confirm=true and "
                    "preview_token to attempt these approved moves.")
        rows, moved = [], 0
        folder = a["folder"].rstrip("/")
        for m in p["moves"]:
            d = self.digest(m["src"])
            if m["clash"]:
                rows.append({"destination": m["dst"], "expected_digest": d, "reason": "destination already exists; nothing replaced",
                             "rename_performed": False, "source": m["src"], "status": "skipped"})
                continue
            entry = next(e for e in self.w.files[folder] if e[0] == m["name"])
            self.w.files[folder].remove(entry)
            self.w.files.setdefault(m["pkey"], []).append(entry)
            moved += 1
            rows.append({"destination": m["dst"],
                         "destination_directory_identity": {"device": 16777230, "inode": int(d[:7], 16)},
                         "digest": d, "expected_digest": d, "observed_digest": d, "rename_performed": True,
                         "source": m["src"], "status": "moved"})
        rows += [{"reason": why, "source": path, "status": "skipped"} for path, why in p["skipped"]]
        _fx(self.w, "organize_files", a)
        if any(r["status"] != "moved" for r in rows):
            head = (f"(error: file organization incomplete; moved {moved} of {len(p['moves'])} planned files. "
                    "Count includes only verified moves; unknown results may already have changed paths. "
                    "Read each receipt before retrying; unknown results must not be retried automatically.)")
        else:
            head = f"Moved {moved} file(s) to {p['tgt']}."
        return head + "\nFile receipts:\n" + "\n".join(json.dumps(r, sort_keys=True) for r in rows)


def _org_steps(org, args, final=None, deny=False):
    """Preview, then the same call confirmed with the preview's token (the card)."""
    steps = [[Call("organize_files", **args)],
             [Call("organize_files", **args, confirm=True, preview_token=org.token)]]
    if not deny:
        steps.append(Final(final))
    return steps


def _ext_groups(org, args):
    p = org.plan(args)
    names = sorted({m["group"] for m in p["moves"]})
    return p, names


# ------------------------------------------------------------------ sort by type
EXT_PROMPTS = [
    ("~/Downloads", "Downloads", ["tidy up my downloads and put each kind of file in its own folder",
                                  "can you organize the downloads folder into folders by file extension",
                                  "my downloads are a mess, group them by what kind of file they are",
                                  "clean up downloads so the pdfs, pictures and zips each get a folder"]),
    ("~/Desktop", "Desktop", ["sort the stuff on my desktop by type",
                              "organise my desktop into folders by file type please",
                              "the desktop is cluttered, can you split it up into one folder per extension"]),
    ("~/Documents", "Documents", ["group everything in my documents folder by extension",
                                  "separate my documents into folders by file type"]),
]


def _m_ext(i):
    folder, label, prompts = EXT_PROMPTS[i % len(EXT_PROMPTS)]
    prompt = prompts[(i // len(EXT_PROMPTS)) % len(prompts)]

    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        w.files = fresh_files(rng, **{folder: _mixed(rng, rng.choice([5, 6, 7]))})
        org = Org(w, seed)
        args = {"pattern": "*", "folder": folder, "destination": ".", "group_by": "extension"}
        p, groups = _ext_groups(org, args)
        n = len(p["moves"])
        final = fin(rng, f"Sorted {n} files from {label} into {join(groups)} folders.",
                    f"Done: the {n} files in {label} now sit in {join(groups)} folders, one per file type.",
                    f"{label} is tidied up. The {n} files went into {join(groups)} folders.",
                    f"Moved all {n} files into {join(groups)} folders inside {label}.")
        return mk(style(rng, prompt), now, w, _org_steps(org, args, final),
                  "sort / organize / group by type: organize_files preview, then confirm with the preview's token; never a run of move_path",
                  cards=["organize_files"], approve=[True], must_see=["Would move", "Moved"])
    return make


def _m_ext_typo(rng_unused=None):
    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        w.files = fresh_files(rng, **{"~/Downloads": _mixed(rng, 7)})
        org = Org(w, seed)
        args = {"pattern": "*", "folder": "~/Downloads", "destination": ".", "group_by": "extension"}
        p, groups = _ext_groups(org, args)
        n = len(p["moves"])
        final = fin(rng, f"Your {n} downloads are sorted into {join(groups)} folders.",
                    f"All {n} files from Downloads are now in {join(groups)} folders.")
        return mk(pick(rng, ["sort my donwloads folder into folders by file type pls", "orgnize my downloads by extension"]),
                  now, w, _org_steps(org, args, final), "typo in the folder or verb: still organize_files, preview then confirm",
                  cards=["organize_files"], approve=[True], must_see=["Would move", "Moved"])
    return make


# ------------------------------------------------------------------ gather matching files
GATHER = [
    # (pattern, folder, destination, noun, label, matching names, other names, prompts)
    ("*.pdf", "~/Downloads", "~/Documents/PDFs", "PDFs", "Downloads",
     lambda r: r.sample(PDFS, 4), lambda r: r.sample(IMGS, 2) + r.sample(ZIPS, 1),
     ["put all the pdfs in my downloads together in one folder in documents",
      "file the pdfs from downloads into a pdfs folder under documents"]),
    ("Screenshot*", "~/Desktop", "~/Desktop/Screenshots", "screenshots", "Desktop",
     lambda r: [f"Screenshot 2026-10-{d:02d} at {h}.{m:02d}.{s:02d} {ap}.png" for d, h, m, s, ap in
                [(r.randint(1, 28), r.randint(1, 11), r.randint(0, 59), r.randint(0, 59), r.choice(["AM", "PM"])) for _ in range(4)]],
     lambda r: ["todo.txt", "wallpaper.jpg"],
     ["put the screenshots cluttering my desktop into their own folder",
      "move all the screenshots on my desktop into a folder called Screenshots"]),
    ("*.zip", "~/Downloads", "~/Documents/Archives", "zip files", "Downloads",
     lambda r: r.sample(ZIPS, 3), lambda r: r.sample(PDFS, 2) + r.sample(IMGS, 2),
     ["move every zip in downloads into an archives folder in documents"]),
    ("*.dmg", "~/Downloads", "~/Documents/Installers", "installers", "Downloads",
     lambda r: ["Slack.dmg", "Zoom.dmg", "Firefox.dmg"], lambda r: r.sample(PDFS, 2) + r.sample(DOCS, 1),
     ["file all the dmg installers in downloads under documents/Installers"]),
    ("invoice_*", "~/Documents", "~/Documents/Invoices", "invoices", "Documents",
     lambda r: [f"invoice_{r.randint(1000, 1099)}.pdf" for _ in range(3)] + ["invoice_2207.pdf"],
     lambda r: r.sample(DOCS, 2),
     ["put all my invoice files in documents together in one folder"]),
]


def _m_gather(i):
    pattern, folder, dest, noun, label, mk_match, mk_other, prompts = GATHER[i]
    prompt = prompts[i % len(prompts)] if i < len(GATHER) else prompts[0]

    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        match = sorted(set(mk_match(rng)))
        w.files = fresh_files(rng, **{folder: match + mk_other(rng)})
        org = Org(w, seed)
        args = {"pattern": pattern, "folder": folder, "destination": dest}
        n = len(org.plan(args)["moves"])
        folder_name = dest.split("/")[-1]
        final = fin(rng, f"Moved the {n} {noun} from {label} into {folder_name}.",
                    f"Done: {n} {noun} are now in the {folder_name} folder.",
                    f"All {n} {noun} went into {folder_name}, and everything else stayed put.")
        return mk(style(rng, prompt), now, w, _org_steps(org, args, final),
                  "gather files that match a pattern: organize_files preview, then confirm with the token (not move_path per file)",
                  cards=["organize_files"], approve=[True], must_see=["Would move", "Moved"])
    return make


# ------------------------------------------------------------------ separate debug logs by date
DEBUG_PROMPTS = [
    "separate my wisp debug files in downloads into one folder per day",
    "can you file the wisp debug logs by date, a folder for each day",
    "put each day's wisp debug exports in its own dated folder",
    "organize the debug logs sitting in downloads by the date in their names",
]


def _m_debug(i):
    """i < 2: every log has a date, so the receipt is clean and the final states it. i >= 2: the undated
    wisp-debug-latest.json is left untouched, which makes the tool report an incomplete run; the host then
    ends the turn itself, so the scripted turn ends at the confirm call."""
    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        days = sorted(rng.sample(range(1, 14), rng.choice([3, 3, 4])))
        logs = [f"wisp-debug-{(now.date() - D(days=d)).isoformat()}.json" for d in days]
        extra = ["wisp-debug-latest.json"] if i >= 2 else []
        w.files = fresh_files(rng, **{"~/Downloads": logs + extra + rng.sample(PDFS, 1)})
        org = Org(w, seed)
        dest = ["~/Downloads/Wisp Debug Logs", ".", "~/Documents/Debug Logs", "~/Downloads/debug-by-date"][i % 4]
        args = {"pattern": "wisp-debug-*.json", "folder": "~/Downloads", "destination": dest, "group_by": "debug_date"}
        n = len(logs)
        if i < 2:
            final = fin(rng, f"Moved the {n} logs into folders named for their dates.",
                        f"The {n} debug logs are filed by day, one folder per date.",
                        f"Done: each of the {n} logs now sits in a folder for its date.")
            steps = _org_steps(org, args, final)
        else:
            steps = _org_steps(org, args, deny=True)
        return mk(style(rng, DEBUG_PROMPTS[i % len(DEBUG_PROMPTS)]), now, w, steps,
                  "separate debug logs by date: organize_files group_by=debug_date, preview then confirm with the token",
                  cards=["organize_files"], approve=[True], must_see=["Would move"])
    return make


# ------------------------------------------------------------------ nothing to move, a name clash, a denied card
def _m_nomatch(i):
    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        if i == 0:
            w.files = fresh_files(rng, **{"~/Downloads": ["wisp-debug-latest.json"] + rng.sample(PDFS, 2)})
            org = Org(w, seed)
            args = {"pattern": "wisp-debug-*.json", "folder": "~/Downloads", "destination": ".", "group_by": "debug_date"}
            final = fin(rng, "Nothing was moved. The only debug log is wisp-debug-latest.json, and its name has no date to sort by.",
                        "No change: wisp-debug-latest.json is the only debug file and it has no date in its name, so there is nothing to file by day.")
            prompt = "sort my wisp debug logs into folders by day"
        elif i == 1:
            w.files = fresh_files(rng, **{"~/Desktop": ["todo.txt", "wallpaper.jpg", "notes.md"]})
            org = Org(w, seed)
            args = {"pattern": "*.heic", "folder": "~/Desktop", "destination": "~/Pictures/HEIC"}
            final = fin(rng, "I found no HEIC files on your Desktop, so nothing was moved.",
                        "Nothing moved: there are no .heic files in Desktop.")
            prompt = "gather all the heic photos on my desktop into one folder in pictures"
        else:
            w.files = fresh_files(rng, **{"~/Documents": ["thesis_outline.docx", "essay_draft.docx"]})
            org = Org(w, seed)
            args = {"pattern": "*.xlsx", "folder": "~/Documents", "destination": "~/Documents/Spreadsheets"}
            final = fin(rng, "There are no spreadsheets in Documents, so nothing was moved.",
                        "No .xlsx files turned up in Documents, so I left everything where it is.")
            prompt = "organize my spreadsheets in documents into their own folder"
        steps = [[Call("organize_files", **args)], Final(final)]
        return mk(prompt, now, w, steps, "the preview finds nothing: say so plainly; no confirm, no move_path",
                  must_see=["No files in"])
    return make


def _m_denied(i):
    def make(rng):
        seed = rng.random()
        now, w = setup(rng)
        folder = ["~/Downloads", "~/Desktop", "~/Downloads"][i % 3]
        w.files = fresh_files(rng, **{folder: _mixed(rng, 6)})
        org = Org(w, seed)
        args = {"pattern": "*", "folder": folder, "destination": ".", "group_by": "extension"}
        prompt = ["sort the downloads folder by file type", "put the desktop files in folders by type, thanks",
                  "tidy my downloads into type folders"][i % 3]
        return mk(style(rng, prompt), now, w, _org_steps(org, args, deny=True),
                  "organize_files preview, then the confirm card is denied: the turn ends at the call",
                  cards=["organize_files"], approve=[False], must_see=["Would move"], no_effect=True)
    return make


def _m_vague(i):
    prompts = ["organize my files", "can you sort out my files for me", "tidy up my folders", "clean up my files please"]
    finals = ["Which folder should I tidy: Downloads, Desktop or Documents?",
              "Which folder do you want sorted, Downloads, Desktop or Documents?",
              "Happy to. Which folder should I start with, and do you want them grouped by file type?",
              "Tell me which folder (Downloads, Desktop or Documents) and I'll show you a preview before anything moves."]

    def make(rng):
        now, w = setup(rng)
        return mk(style(rng, prompts[i % 4]), now, w, [Final(finals[i % 4])],
                  "no folder named: one short question; nothing is listed or moved")
    return make


# ------------------------------------------------------------------ one explicit move is one move_path
MOVES = [
    ("move report_draft.pdf from downloads to documents", "~/Downloads/report_draft.pdf", "~/Documents/",
     "Moved report_draft.pdf from Downloads to Documents.", "~/Downloads", ["report_draft.pdf"]),
    ("put poster_final.png into my pictures folder", "~/Desktop/poster_final.png", "~/Pictures/",
     "poster_final.png is in Pictures now.", "~/Desktop", ["poster_final.png"]),
    ("relocate lab4_handout.pdf to the desktop", "~/Downloads/lab4_handout.pdf", "~/Desktop/",
     "lab4_handout.pdf is on your Desktop now.", "~/Downloads", ["lab4_handout.pdf"]),
    ("move essay_draft.docx out of downloads and into documents", "~/Downloads/essay_draft.docx", "~/Documents/",
     "essay_draft.docx now lives in Documents.", "~/Downloads", ["essay_draft.docx"]),
    ("move the file tuition_receipt.pdf on my desktop to documents", "~/Desktop/tuition_receipt.pdf", "~/Documents/",
     "Moved tuition_receipt.pdf from Desktop to Documents.", "~/Desktop", ["tuition_receipt.pdf"]),
]


def _m_move(i):
    prompt, src, dst, final, folder, names = MOVES[i]

    def make(rng):
        now, w = setup(rng)
        w.files = fresh_files(rng, **{folder: names + rng.sample(IMGS + ZIPS, 2)})
        _move_world(w)
        return mk(prompt, now, w, [[Call("move_path", source=src, destination=dst)], Final(final)],
                  "one explicit move of one named file: move_path (positive control)", must_see=["Moved"])
    return make


# ------------------------------------------------------------------ show / list / find is a read
SHOW = [
    ("run through what's on my desktop for me", "~/Desktop", "Desktop"),
    ("what do i have in the pictures folder", "~/Pictures", "Pictures"),
    ("how many things are in my downloads right now", "~/Downloads", "Downloads"),
    ("list the files in documents, i just want to look", "~/Documents", "Documents"),
    ("anything sitting in my downloads that i should know about", "~/Downloads", "Downloads"),
    ("peek into the desktop folder", "~/Desktop", "Desktop"),
]


def _m_show(i):
    prompt, folder, label = SHOW[i]

    def make(rng):
        now, w = setup(rng)
        names = _mixed(rng, rng.choice([5, 6])) if folder in ("~/Downloads", "~/Desktop") else None
        w.files = fresh_files(rng, **({folder: names} if names else {}))
        listing = sorted(n for n, _ in w.files[folder])
        n = len(listing)
        lead = fin(rng, f"{label} has {n} items:", f"There are {n} items in {label}:", f"You have {n} things in {label}:")
        reply = lead + "\n" + "\n".join(f"- {x}" for x in listing)
        return mk(style(rng, prompt), now, w, [[Call("list_dir", path=folder)], Final(reply)],
                  "look at a folder: list_dir only, nothing is moved, renamed or organized (the lack of a request to tidy is the point)")
    return make


def _m_find(i):
    specs = [("do i have the tuition receipt saved anywhere", "tuition", None),
             ("is there a flight ticket pdf on this mac", "flight", None),
             ("where did i put the w9", "w9", None)]
    prompt, key, _ = specs[i]

    def make(rng):
        now, w = setup(rng)
        name = next(n for n in PDFS if key in n)
        folder = rng.choice(["~/Downloads", "~/Documents"])
        w.files = fresh_files(rng, **{folder: [name] + rng.sample(IMGS, 2)})
        size = dict(w.files[folder])[name]
        label = folder.split("/")[-1]
        reply = fin(rng, f"Yes, {name} is in your {label} folder, {size}.", f"It's in {label} as {name}, {size}.",
                    f"Found it: {name} in {label}, {size}.")
        return mk(style(rng, prompt), now, w, [[Call("find_files", query=key)], Final(reply)],
                  "find a file: find_files, nothing moves")
    return make


def _m_sizes(rng_unused=None):
    def make(rng):
        now, w = setup(rng)
        w.files = fresh_files(rng, **{"~/Downloads": ["lecture_recording.mov", "Zoom.pkg", "budget_oct.xlsx", "photos_oct.zip", "notes.txt"]})
        w.files["~/Downloads"] = [("lecture_recording.mov", "612MB"), ("Zoom.pkg", "112MB"), ("budget_oct.xlsx", "58KB"),
                                  ("photos_oct.zip", "240MB"), ("notes.txt", "4KB")]
        reply = "The biggest is lecture_recording.mov at 612MB, then photos_oct.zip at 240MB and Zoom.pkg at 112MB."
        return mk(pick(rng, ["what's eating the most space in downloads", "which downloads are the biggest ones"]), now, w,
                  [[Call("list_dir", path="~/Downloads", details=True, sort="size")], Final(reply)],
                  "size question: list_dir sorted by size, a read")
    return make


# =============================================================================
# 2. RULED OUT: keep it, leave it, not that one; a refused move stays refused
# =============================================================================
KEEPERS = ["Return the Ridgeline order", "Renew parking permit", "Pay the water bill", "Sign the lease addendum",
           "Cancel the gym trial", "Mail the scholarship form", "Call the registrar about the hold"]
OTHERS = ["Text landlord about the sink", "Buy printer ink", "Return library books", "Pick up prescription",
          "Print boarding pass", "Wash gym clothes", "Water the cactus", "Book dentist visit",
          "Email advisor about spring classes", "Renew bus pass", "Submit lab 4", "Sign up for intramurals"]
KEEP_HANDLE = {"Return the Ridgeline order": "ridgeline", "Renew parking permit": "parking", "Pay the water bill": "water bill",
               "Sign the lease addendum": "lease", "Cancel the gym trial": "gym trial",
               "Mail the scholarship form": "scholarship", "Call the registrar about the hold": "registrar"}


def _overdue_world(rng, n_others, *, hour=None, oldest=None):
    """A world whose only past-due items are reminders: one to keep and `n_others` more.
    oldest=True: the kept one is the oldest by at least two days (3-40 days old), the rest are younger.
    oldest="close": the kept one is the oldest, but only by a few hours. oldest="newest": the kept one is the newest.
    oldest=False: the kept one is neither the oldest nor the newest."""
    now, w = setup(rng, hour=hour if hour is not None else rng.choice([9, 11, 14, 17]), extra=0, exams=False)
    keep = rng.choice(KEEPERS)
    others = rng.sample(OTHERS, n_others)
    hours = {t: rng.randint(0, 5) for t in [keep] + others}
    if oldest is True:
        # the kept one's age varies (3-40 days) and every other item is at least two days younger, so a
        # whole-day cutoff equal to the kept one's age in days leaves it out and takes all the rest
        kd = rng.choice([3, 4, 5, 6, 8, 10, 14, 21, 30, 40])
        hours[keep] = rng.randint(1, 23)
        ages = {keep: kd, **{t: rng.randint(1, max(1, kd - 2)) for t in others}}
    elif oldest == "close":
        # the kept one is the oldest, but by a few hours only: no whole-day cutoff separates it from the next
        kd = rng.randint(2, 12)
        hours[keep] = rng.randint(3, 5)
        ages = {keep: kd, others[0]: kd, **{t: rng.randint(1, kd - 1) for t in others[1:]}}
        hours[others[0]] = rng.randint(1, hours[keep] - 2)
    elif oldest == "newest":
        # the kept one is the most recent overdue item: any window that reaches it takes all the others too
        ages = {keep: rng.randint(1, 2), **{t: rng.randint(4, 14) for t in others}}
    elif oldest is False:
        days = sorted(rng.sample(range(1, 9), n_others + 1))
        mid = rng.randint(1, n_others - 1) if n_others > 1 else 0
        ages = {keep: days[mid]}
        ages.update(dict(zip(others, [d for j, d in enumerate(days) if j != mid])))
    else:
        ages = {t: rng.randint(1, 9) for t in [keep] + others}
    titles = [keep] + others
    rng.shuffle(titles)
    w.reminders = [Reminder(t, now - D(days=ages[t], hours=hours[t])) for t in titles]
    w.reminders.append(Reminder(rng.choice([t for t in OTHERS if t not in titles]), now + D(days=rng.randint(1, 4))))
    w.events = [e for e in w.events if e.end > now]
    w.past = []
    return now, w, keep, others


def _past_titles(w):
    return [r["title"] for r in sorted(w.past_due_rows("", 365), key=lambda r: r["when_ts"])]


def _quoted(titles):
    return join(f"“{t}”" for t in titles)


def _hist_offer(w, keep, rng):
    titles = _past_titles(w)
    n = len(titles)
    others = [t for t in titles if t != keep]
    text = pick_offer(n, others, keep, rng)
    return [turn("clear out my old reminders", text, "search_reminders",
                 [{"name": "search_reminders", "args": {"scope": "past_due"}, "result": ""}])]


def pick_offer(n, others, keep, rng):
    opts = [f"You have {n} past-due reminders: {_quoted(others)} and “{keep}”. Want me to clear them all?",
            f"I can clear all {n} of your overdue reminders, including “{keep}”. Shall I go ahead?",
            f"There are {n} overdue reminders on your list, “{keep}” among them. Should I delete them?"]
    return rng.choice(opts)


# ------------------------------------------------------------------ "keep it" after an offer to clear: no write
KEEP_PROMPTS = ["keep the {h} one", "leave the {h} reminder alone", "no wait, i still need the {h} one",
                "don't delete the {h} one", "not the {h} reminder, hold on to that", "actually keep {h}"]


def _m_keep_after_offer(i):
    def make(rng):
        now, w, keep, others = _overdue_world(rng, rng.choice([2, 3, 4]))
        h = KEEP_HANDLE[keep]
        hist = _hist_offer(w, keep, rng)
        n = len(others)
        if i % 2 == 1:
            final = fin(rng, f"Okay, “{keep}” stays. Nothing has been deleted.",
                        f"Understood, I'm leaving “{keep}” alone and nothing was removed.",
                        f"Got it. “{keep}” stays, and nothing has been cleared.")
        else:
            final = fin(rng, f"Okay, “{keep}” stays. Should I clear the others?",
                        f"I'll leave “{keep}” alone. Do you want the rest of the overdue ones cleared?",
                        f"Got it, “{keep}” is staying. Want me to clear the remaining ones?")
        return mk(style(rng, KEEP_PROMPTS[i % len(KEEP_PROMPTS)].format(h=h)), now, w, [Final(final)],
                  "the person rules one reminder out after an offer to clear: no clear, no delete; say it stays",
                  history=hist)
    return make


def _m_keep_cold(i):
    prompts = [("leave the {h} reminder alone, i need it", "Sure, “{k}” stays as it is."),
               ("don't touch the {h} reminder", "I won't. “{k}” stays exactly as it is."),
               ("keep the {h} one on my list", "It stays. I haven't changed “{k}”.")]

    def make(rng):
        now, w, keep, others = _overdue_world(rng, 3)
        p, f = prompts[i % 3]
        return mk(style(rng, p.format(h=KEEP_HANDLE[keep])), now, w, [Final(f.format(k=keep))],
                  "an instruction to keep or not touch something with nothing to undo: acknowledge, no tool, no write")
    return make


def _m_go_ahead_all(i):
    """Positive control: the offer is accepted for everything, so the whole past-due set goes (one call, one card)."""
    prompts = ["yes, delete all of those old reminders", "go ahead and clear them all out", "yep, remove every one of the overdue reminders"]

    def make(rng):
        now, w, keep, others = _overdue_world(rng, rng.choice([2, 3]))
        _past_world_cleared(w)
        hist = _hist_offer(w, keep, rng)
        n = len(w.past_due_rows("", 365))
        final = fin(rng, f"Cleared {n} past-due items. Nothing upcoming was touched.",
                    f"Done, the {n} overdue items are gone and what's coming up stays.")
        return mk(style(rng, prompts[i % 3]), now, w, [[Call("clear_past_reminders", days=365)], Final(final)],
                  "the person accepts the offer for everything: clear_past_reminders behind a card (positive control)",
                  cards=["clear_past_reminders"], approve=[True], must_see=["Cleared"], history=hist)
    return make


def _past_world_cleared(w):
    def handler(args):
        rows = w.past_due_rows(args.get("query") or "", args.get("days") or 365)
        for r in rows:
            w.reminders.remove(r["_ref"])
        _fx(w, "clear_past_reminders", args)
        return f"Cleared {len(rows)} past-due item(s)." if rows else "Nothing past-due to clear."
    w.overrides["clear_past_reminders"] = handler


# ------------------------------------------------------------------ "clear the rest but keep X"
# clear_past_reminders and clear_reminders can only be narrowed by a title word to INCLUDE or (past-due) by
# how far back to reach. So when the one to keep is the oldest by at least a day, `days` = its age in whole days
# (3-40 here, read off the dates in the list) leaves it out (one call, one card); when it sits in the middle, is
# the newest, or is the oldest by only hours, no whole-day window fits, and the honest move is to say so and offer to delete the
# others by name. (Deleting several by name in one step shows one batched "calendar_changes" card, and
# one per step is refused by the host as a repeat, so neither is scripted.)
REST_PROMPTS = [
    "delete my overdue reminders but hang on to the {h} one",
    "clear out the old reminders except the {h} one",
    "get rid of the overdue reminders apart from {h}",
    "wipe my overdue reminders, just not the {h} reminder",
    "remove the past-due ones but keep the {h} one",
]


def _m_rest(i, *, after_offer=False):
    def make(rng):
        now, w, keep, others = _overdue_world(rng, rng.choice([2, 3, 3]), oldest=True)
        _past_world_cleared(w)
        h = KEEP_HANDLE[keep]
        n = len(others)
        if after_offer:
            hist = _hist_offer(w, keep, rng)
            prompt = ["yes, clear the rest but not the {h} one", "ok go ahead, except for {h}", "yes delete the rest, just keep the {h} one"][i % 3].format(h=h)
        else:
            hist = None
            prompt = REST_PROMPTS[i % len(REST_PROMPTS)].format(h=h)
        final = fin(rng, f"Cleared the {n} recent overdue reminders. “{keep}” is older than those, so it stays on your list.",
                    f"Done, {n} overdue reminders are gone and “{keep}” is still there.",
                    f"Removed the {n} newer overdue ones and left “{keep}” alone.")
        kept_due = next(r.due for r in w.reminders if r.title == keep)
        cutoff = (now - kept_due).days        # whole days the kept one is old: a window of that many days stops short of it
        steps = [[Call("search_reminders", scope="past_due")], [Call("clear_past_reminders", days=cutoff)], Final(final)]
        return mk(style(rng, prompt), now, w, steps,
                  "clear all but one when the kept one is the oldest: read the list, work out the kept one's age in whole days from its date, "
                  "then clear_past_reminders with days = that age so the window stops short of it",
                  cards=["clear_past_reminders"], approve=[True], must_see=["Cleared"], history=hist)
    return make


def _m_rest_ask(i):
    def make(rng):
        now, w, keep, others = _overdue_world(rng, rng.choice([3, 3, 4]), oldest=False)
        h = KEEP_HANDLE[keep]
        rest = _quoted(sorted(others))
        final = fin(rng, f"“{keep}” sits in the middle of the overdue list, and I can only clear that list as a whole or delete reminders by name. Should I delete {rest} by name?",
                    f"I can't clear the overdue list while skipping “{keep}”. I can delete {rest} one by one by name, if you want that.",
                    f"There's no way to clear the overdue ones and skip just “{keep}” at once. Want me to delete {rest} by name instead?")
        return mk(style(rng, REST_PROMPTS[i % len(REST_PROMPTS)].format(h=h)), now, w,
                  [[Call("search_reminders", scope="past_due")], Final(final)],
                  "clear all but one when the kept one is mid-list: no scope leaves it out, so say so and offer to delete the others by name; nothing is deleted",
                  must_see=[keep])
    return make


def _m_rest_ask_edge(i):
    """The kept one is the newest overdue item, or the oldest by only a few hours: no whole-day window leaves it
    out, so say so and offer to delete the others by name. Nothing is deleted."""
    kind = ("newest", "close")[i % 2]

    def make(rng):
        now, w, keep, others = _overdue_world(rng, rng.choice([2, 3]), oldest=kind)
        h = KEEP_HANDLE[keep]
        rest = _quoted(sorted(others))
        if kind == "newest":
            final = fin(rng, f"“{keep}” is the most recent of your overdue reminders, so I can't clear the older ones by age without it. Should I delete {rest} by name instead?",
                        f"I can't clear the older overdue ones without “{keep}”, since it's the newest. I can delete {rest} one by one by name, if you want that.")
        else:
            other = others[0]
            final = fin(rng, f"“{keep}” is overdue by only a few hours more than “{other}”, so a clear by age can't take one and skip the other. Should I delete {rest} by name instead?",
                        f"I can't clear the overdue list and skip “{keep}”, since it's barely older than “{other}”. I can delete {rest} one by one by name, if you want that.")
        prompt = REST_PROMPTS[(i + 1) % len(REST_PROMPTS)].format(h=h)
        return mk(style(rng, prompt), now, w, [[Call("search_reminders", scope="past_due")], Final(final)],
                  "clear all but one when no whole-day window separates the kept one from the others (it is the newest, or only hours older): "
                  "say so and offer to delete the others by name; nothing is deleted", must_see=[keep])
    return make


def _m_rest_ambiguous(i):
    """Two reminders fit the name given: ask which one stays, delete nothing."""
    pairs = [("Water the cactus", "Pay the water bill", "water"), ("Return library books", "Return the Ridgeline order", "return"),
             ("Renew bus pass", "Renew parking permit", "renew")]

    def make(rng):
        a, b, word = pairs[i % 3]
        now, w = setup(rng, hour=rng.choice([9, 11, 14]), extra=0, exams=False)
        third = rng.choice([t for t in OTHERS if t not in (a, b) and word not in t.lower()])
        w.reminders = [Reminder(t, now - D(days=rng.randint(1, 8))) for t in (a, b, third)]
        w.events = [e for e in w.events if e.end > now]
        w.past = []
        final = fin(rng, f"Two overdue reminders fit “{word}”: “{a}” and “{b}”. Which one should stay?",
                    f"Which one do you want to keep, “{a}” or “{b}”? Both are overdue, so I haven't deleted anything.")
        return mk(style(rng, f"clear my overdue reminders but keep the {word} one"), now, w,
                  [[Call("search_reminders", scope="past_due")], Final(final)],
                  "two reminders fit the one to keep: read the list, ask which, delete nothing", must_see=[a])
    return make


# =============================================================================
# 2b. CALENDAR CANCEL: resolve the event with a read, cancel the one that fits, ask when two fit,
#     and say plainly when the host refuses (a read-only calendar) or nothing matches
# =============================================================================
CANCEL_SPECS = [  # (calendar title, key the read and the cancel use, how the person says it, place)
    ("Climbing with Theo", "climbing", "climbing", "Cascade Climbing Gym"),
    ("Piano lesson", "piano", "piano", ""),
    ("Spin class", "spin", "spin class", "Rec Center Studio B"),
    ("Haircut", "haircut", "haircut", "Mercer Street Barbers"),
    ("Trivia night", "trivia", "trivia", "The Rusty Anchor"),
    ("Photography club", "photography", "photography club", "Arts Annex 2"),
    ("Bike tune-up", "bike", "bike tune-up", "Spoke & Chain"),
    ("Resume workshop", "resume", "the resume workshop", "Career Center"),
]
CANCEL_VERBS = ["drop {r} from my calendar", "cancel {r}, i can't make it", "i won't be going to {r}, delete it",
                "remove {r} from my calendar this week", "get rid of {r} on my calendar", "{r} is off, remove it from the calendar"]
CANCEL_DECOYS = [("Study group", "Hollis Library"), ("Lab group check-in", "Kerr Hall 301"), ("Call with advisor", ""),
                 ("Roommate meeting", ""), ("Tutoring session", "Hollis Library"), ("Board game night", "Maya's place")]


def _cancel_world(rng, title, loc, *, extra_titles=(), writable=True, ctx=""):
    now, w = setup(rng, hour=rng.choice([8, 10, 13]), extra=0, exams=False)
    w.events, w.past = [], []
    start = at(now.date() + D(days=rng.randint(1, 5)), rng.choice([15, 16, 17, 18]), 0)
    add_event(w, title, start, 60, loc, writable=writable, context=ctx)
    for t, l in rng.sample([d for d in CANCEL_DECOYS if d[0] not in extra_titles], 2):
        add_event(w, t, at(now.date() + D(days=rng.randint(1, 6)), rng.choice([9, 11, 19]), 0), 60, l)
    return now, w, start


def _on(day, now, start):
    """'tomorrow at 4 PM' / 'on Sunday, Dec 13 at 6 PM'."""
    lab = dlabel(day, now)
    return f"{'' if lab in ('today', 'tomorrow') else 'on '}{lab} at {clock(start)}"


def _m_cancel_ok(i):
    title, key, ref, loc = CANCEL_SPECS[i % len(CANCEL_SPECS)]

    def make(rng):
        now, w, start = _cancel_world(rng, title, loc)
        when = _on(start.date(), now, start)
        final = fin(rng, f"Cancelled {title} {when}.", f"{title} {when} is off your calendar.")
        return mk(style(rng, CANCEL_VERBS[i % len(CANCEL_VERBS)].format(r=ref)), now, w,
                  [[Call("get_upcoming", days=14, query=key, calendar_only=True)], [Call("cancel_event", title=key)], Final(final)],
                  "cancel one calendar event: read to find the one that fits, then cancel_event with part of its title behind a card",
                  cards=["cancel_event"], approve=[True], must_see=["Cancelled"])
    return make


def _m_cancel_ask(i):
    """Two different events fit the word given: read, list both with times, ask which; cancel nothing."""
    pairs = [("Coffee with Theo", "Coffee with Maya", "coffee"), ("Lab group check-in", "Lab 4 office hours", "lab"),
             ("Dinner with Mom", "Dinner with Carmen", "dinner")]
    a, b, word = pairs[i % 3]

    def make(rng):
        now, w, start = _cancel_world(rng, a, "", extra_titles=(a, b))
        start_b = at(start.date() + D(days=rng.randint(1, 2)), rng.choice([10, 12, 18]), 0)
        add_event(w, b, start_b, 60, "")
        first, second = sorted([(start, a), (start_b, b)])
        la = f"{first[1]} {_on(first[0].date(), now, first[0])}"
        lb = f"{second[1]} {_on(second[0].date(), now, second[0])}"
        final = fin(rng, f"Two events fit “{word}”: {la}, and {lb}. Which one should I cancel?",
                    f"You have {la}, and {lb}. Which one do you want gone?")
        prompt = ["cancel {w}", "take {w} off my calendar", "drop the {w} on my calendar"][i % 3].format(w=word)
        return mk(style(rng, prompt), now, w,
                  [[Call("get_upcoming", days=14, query=word, calendar_only=True)], Final(final)],
                  "two events fit the word given: read, name both with their times, ask which; cancel nothing", must_see=[a])
    return make


def _m_cancel_refuse(i):
    """The host refuses: the event is on a read-only calendar. Nothing was cancelled, and the final says where to do it."""
    specs = [("Thanksgiving Day", "thanksgiving", "thanksgiving", "US Holidays", True),
             ("Linden Lions home game", "lions", "the lions game", "Athletics (shared)", False)]
    title, key, ref, ctx, holiday = specs[i % 2]

    def make(rng):
        now, w = setup(rng, hour=rng.choice([8, 10, 13]), extra=0, exams=False)
        w.events, w.past = [], []
        if holiday:
            day = dt.date(2026, 11, 26)
            now = dt.datetime.combine(day - D(days=rng.randint(3, 12)), dt.time(rng.choice([9, 14]), 5))
            H.set_clock(now)
            w.now = now
            add_event(w, title, dt.datetime.combine(day, dt.time()), 24 * 60, "", all_day=True, writable=False, context=ctx)
            read = Call("get_upcoming", days=30, query=key, calendar_only=True, include_holidays=True)
            prompt = ["i'm not doing anything for thanksgiving, remove it from my calendar", "delete thanksgiving off my calendar"][(i // 2) % 2]
        else:
            start = at(now.date() + D(days=rng.randint(2, 6)), 19, 0)
            add_event(w, title, start, 120, "Linden Stadium", writable=False, context=ctx)
            read = Call("get_upcoming", days=14, query=key, calendar_only=True)
            prompt = ["i'm skipping the lions game, delete it from my calendar", "remove the lions game from my calendar"][(i // 2) % 2]
        final = fin(rng, f"Nothing was cancelled. {title} is on a read-only calendar, so Wisp can't change it from this Mac. You can remove it in Calendar if it's yours to cancel.",
                    f"I couldn't cancel {title}, because it sits on a read-only calendar. It's still there; you would take it off in Calendar.")
        return mk(style(rng, prompt), now, w, [[read], [Call("cancel_event", title=key)], Final(final)],
                  "the event is on a read-only calendar: cancel_event is refused; say nothing was cancelled and where to change it, never claim it was removed",
                  cards=["cancel_event"], approve=[True], no_effect=True)
    return make


def _m_cancel_nomatch(i):
    """No upcoming event fits: read, say so, cancel nothing, and do not cancel a different one."""
    def make(rng):
        title, key, ref, loc = CANCEL_SPECS[(i + 3) % len(CANCEL_SPECS)]
        now, w, start = _cancel_world(rng, "Dentist appointment", "Pine Street Dental")
        final = fin(rng, f"I don't see an upcoming event matching “{key}”, so nothing was cancelled. What's it called in Calendar?",
                    f"Nothing on your calendar for the next two weeks matches “{key}”, so I haven't cancelled anything. Which event did you mean?")
        return mk(style(rng, CANCEL_VERBS[(i + 1) % len(CANCEL_VERBS)].format(r=ref)), now, w,
                  [[Call("get_upcoming", days=14, query=key, calendar_only=True)], Final(final)],
                  "no event fits the name given: read, say plainly nothing matches, cancel nothing and do not cancel a different event")
    return make


# ------------------------------------------------------------------ timers: stop all but one
def _m_timers_keep(i):
    sets = [[("pasta", "6m 12s"), ("laundry", "24m 40s"), ("tea", "2m 30s")],
            [("oven", "18m 5s"), ("study block", "11m 20s")],
            [("rice", "9m 15s"), ("laundry", "41m"), ("break", "4m 50s"), ("dough", "52m")]]

    def make(rng):
        now, w = setup(rng)
        timers = sets[i % 3]
        keep = timers[rng.randrange(len(timers))][0]
        _timers_world(w, timers)
        stop = [t for t, _ in timers if t != keep]
        prompt = ["stop my timers except the {k} one", "cancel every timer but leave the {k} timer running",
                  "kill all the timers apart from {k}"][i % 3].format(k=keep)
        steps = [[Call("manage_timers", action="list")], [Call("manage_timers", action="cancel", label=t) for t in stop]]
        final = fin(rng, f"Stopped the {join(stop)} timer{'s' if len(stop) > 1 else ''}. The {keep} timer is still running.",
                    f"Cancelled {join(stop)}, and {keep} keeps counting down.")
        return mk(style(rng, prompt), now, w, steps + [Final(final)],
                  "stop all timers but one: list them, cancel the others by label; the kept timer is never cancelled",
                  must_see=["Cancelled"])
    return make


# ------------------------------------------------------------------ a refused move: say nothing moved, add nothing
REFUSAL_TITLES = [("Robotics demo", "Robotics demo", "the robotics demo", "Kerr Hall 118"),
                  ("Pottery class", "Pottery class", "pottery", "Clay Studio"),
                  ("Tutoring session", "Tutoring session", "the tutoring session", "Hollis Library"),
                  ("Board game night", "Board game night", "board game night", "Rec Center Lounge"),
                  ("Choir rehearsal", "Choir rehearsal", "choir rehearsal", "Mercer Hall 12"),
                  ("Open mic", "Open mic", "the open mic", "The Rook"),
                  ("Language exchange", "Language exchange", "the language exchange", "Union Cafe"),
                  ("Faculty mixer", "Faculty mixer", "the faculty mixer", "Student Union Ballroom")]
REFUSAL_KINDS = [({"invitees": None}, "unsynced"), ({"writable": False}, "readonly"),
                 ({"organizer": False, "invitees": 5}, "notorganizer"), ({}, "nomatch")]
REFUSAL_VERBS = ["{ref} should be at {tw} instead, can you change it", "move {ref} to {tw}", "bump {ref} to {tw} that day",
                 "could {ref} start at {tw} instead", "reschedule {ref} for {tw}"]


def _m_refuse(i):
    kw, kind = REFUSAL_KINDS[i % 4]
    title, key, ref, loc = REFUSAL_TITLES[i % len(REFUSAL_TITLES)]

    def make(rng):
        now, w = setup(rng, hour=rng.choice([8, 10, 13]), extra=0, exams=False)
        old = at(now.date() + D(days=rng.randint(1, 4)), rng.choice([16, 17, 18]), 0)
        new = at(old.date(), rng.choice([19, 20]), 0)
        if kind == "nomatch":
            add_event(w, "Dentist appointment", old, 60, "Pine Street Dental")
        else:
            add_event(w, title, old, 60, loc, **kw)
        tw = t_txt(rng, new.hour)
        prompt = REFUSAL_VERBS[i % len(REFUSAL_VERBS)].format(ref=ref, tw=tw)
        if kind == "unsynced":
            final = fin(rng, f"Nothing was moved. Calendar hasn't finished syncing who's invited to {title}, so I held off. Try again in a bit or move it in Calendar.",
                        f"{title} is still at its old time. Calendar is syncing its invitee list, so I didn't change it.")
        elif kind == "readonly":
            final = fin(rng, f"Nothing was moved. {title} sits on a read-only calendar, so Wisp can't change it.",
                        f"{title} stayed put because its calendar is read-only. You would change it in Calendar.")
        elif kind == "notorganizer":
            final = fin(rng, f"Nothing was moved. Someone else organizes {title}, so only they can change its time.",
                        f"I didn't move {title}: you're not the organizer, so it's unchanged. You could ask them or propose a time in Calendar.")
        else:
            final = fin(rng, f"Nothing was moved or added. I don't see an upcoming event called {title}. What's it called in Calendar?",
                        f"No event named {title} is coming up, so nothing changed. Which one did you mean?")
        read = [Call("get_upcoming", days=14, query=key, calendar_only=True)]
        steps = [read, Final(final)] if kind == "nomatch" else [read, [Call("update_event", title=key, when_iso=iso(new))], Final(final)]
        return mk(style(rng, prompt), now, w, steps,
                  "the host refuses the move: say plainly nothing moved; no cancel, no new event",
                  no_effect=True)
    return make


def _m_move_ok(i):
    """Positive control: an ordinary move the host accepts, behind a card."""
    specs = [("Coffee with Theo", "Coffee with Theo", "coffee with Theo", "Brewed Awakening"),
             ("Vet visit", "Vet visit", "the vet visit", "Cedar Animal Clinic"),
             ("Oil change", "Oil change", "the oil change", "Quick Lube on Burnside")]
    title, key, ref, loc = specs[i % 3]

    def make(rng):
        now, w = setup(rng, hour=rng.choice([8, 10, 13]), extra=0, exams=False)
        old = at(now.date() + D(days=rng.randint(1, 3)), rng.choice([10, 14, 16]), 0)
        add_event(w, title, old, 60, loc)
        new = old + D(hours=rng.choice([2, 3]))
        word = "tomorrow" if (old.date() - now.date()).days == 1 else old.strftime("%A")
        prompt = pick(rng, [f"shift {ref} to {t_txt(rng, new.hour)}", f"can you put {ref} at {t_txt(rng, new.hour)} instead"])
        final = fin(rng, f"Moved {title} to {clock(new)} {word}.", f"{title} now starts at {clock(new)} {word}.")
        return mk(style(rng, prompt), now, w,
                  [[Call("get_upcoming", days=14, query=key, calendar_only=True)], [Call("update_event", title=key, when_iso=iso(new))], Final(final)],
                  "an ordinary move the host accepts: update_event behind a card (positive control)",
                  cards=["update_event"], approve=[True], must_see=["Moved"])
    return make


# =============================================================================
# 3. OUTBOUND: reading a page is a GET; sending data to a site is not something Wisp does
# =============================================================================
_NOTE = "I can keep the text in a note so you can send it from somewhere else."
DECLINES = [
    # (prompt, [finals])
    ('send a POST request to https://api.example.org/v1/events with {"event": "build_done"}',
     ["I can't send data to a web address, because Wisp can only open a page and read it. I can save that data in a note for you instead.",
      "Posting to that endpoint isn't something I can do, since I only read pages. I can put the data in a note if that helps."]),
    ("hit my zapier webhook https://hooks.example.net/catch/55012/abcde with the word hello",
     ["I can't trigger a webhook, as Wisp only reads pages and doesn't send anything to them. I can jot the message down in a note instead.",
      "Triggering a webhook isn't something I can do. Wisp only reads web pages, so I can write the message in a note for you."]),
    ("ping the webhook at https://hooks.example.com/x9f2 to say the deploy finished",
     ["I can't ping a webhook, because that sends something to the site and Wisp only reads pages. I can note the deploy as finished somewhere on your Mac.",
      "Wisp can read a page but can't send a signal to a webhook, so I can't do that one. I could add a reminder or a note about the deploy instead."]),
    ("curl -X POST https://ci.example.org/trigger -d branch=main",
     ["I can't make a POST request. Wisp only reads pages, so triggering that build isn't something I can do from here.",
      "Sending a POST isn't possible for me, since I can only open a page and read it. I can write down the branch in a note, though."]),
    ("push this payload to my endpoint https://ingest.example.net/v2 {\"temp\": 71, \"unit\": \"F\"}",
     ["I can't push data to an endpoint. Wisp only reads pages, but I can save the payload in a note.",
      "Sending that payload isn't something I'm able to do, because I can only read what's at a web address. I can store it in a note."]),
    ("make a PUT call to https://api.example.com/items/4 setting qty to 3",
     ["I can't make a PUT call, since Wisp can only open pages to read them. Nothing was sent.",
      "Changing something on that site isn't possible for me. I can read the page at that address, but I can't send updates to it."]),
    ("notify my slack webhook https://hooks.slack.example/T000/B000/xyz123 that i'm done for the day",
     ["I can't post to a Slack webhook, because Wisp only reads pages. I could text or email the message to someone, or save it in a note.",
      "Sending to a Slack webhook isn't something I can do. I can message a person from your contacts instead, if you want."]),
    ("submit this to the signup form at https://events.example.org/signup name sam, class of 2028",
     ["I can't fill in or submit a form on a website. I can read the page for you, but sending your details is something you'd do there yourself.",
      "Submitting a form isn't something I can do, since Wisp only reads pages. I can open the page's text and tell you what it asks for."]),
    ("upload my resume to https://careers.example.net/apply",
     ["I can't upload files to a website. I can find the resume on your Mac and tell you where it is.",
      "Wisp can't send a file to a site, so I can't do the upload. I can locate the resume for you, though."]),
    ("fire the home assistant webhook http://homeassistant.example:8123/api/webhook/lights-off",
     ["I can't fire a webhook, because Wisp only reads web pages. I can turn things off on your Mac, like Wi-Fi or Do Not Disturb, but not those lights.",
      "Triggering that webhook isn't something I can do. Wisp can change settings on your Mac, but it can't reach your smart home."]),
    ("post this to my twitter: finished my last midterm, free at last",
     ["I can't post to social media, since Wisp has no way to publish there. I can tidy the wording here so you can paste it yourself.",
      "Posting to your account isn't something I can do. I can word it nicely for you to post: finished my last midterm, finally free."]),
    ("order me a pizza from the place on burnside",
     ["I can't place orders, because Wisp has no way to buy things. I could look up the restaurant's number or hours instead.",
      "Ordering isn't something I can do. I can search the web for their menu or phone number, though."]),
    ("can you turn the living room lights off",
     ["I can't control lights, since Wisp only works with your Mac. I can change things like volume, Wi-Fi or Do Not Disturb.",
      "Smart lights are out of reach for me. I can adjust settings on this Mac, but I can't control the lights."]),
    ("book me a table at the thai place for seven tonight",
     ["I can't make reservations, because Wisp has no way to book things. I can search for the restaurant's phone number or hours.",
      "Booking a table isn't something I can do. I could look up their number so you can call, or set a reminder for dinner."]),
]


def _m_decline(i):
    prompt, finals = DECLINES[i]

    def make(rng):
        now, w = setup(rng)
        return mk(prompt, now, w, [Final(rng.choice(finals))],
                  "send data to a site, trigger a webhook, post or order: declined in one gentle line with what Wisp can do instead; no tool is called")
    return make


FETCH = [
    ("open https://housing.example.edu/move-in and tell me when check-in starts", "https://housing.example.edu/move-in",
     "Move-in day check-in opens at 9 AM at the front desk. Bring a photo ID and your key form. Parking is free until noon.",
     "Check-in starts at 9 AM at the front desk. You'll need a photo ID and your key form, and parking is free until noon."),
    ("read https://dining.example.org/hours and tell me when dinner is served", "https://dining.example.org/hours",
     "Hollis Dining hours: breakfast 7 to 10 AM, lunch 11:30 AM to 2 PM, dinner 5 to 8 PM. Closed Sundays after lunch.",
     "Breakfast is 7 to 10 AM, lunch 11:30 AM to 2 PM and dinner 5 to 8 PM. It closes after lunch on Sundays."),
    ("read https://studyhub.example.com/finals-week for me and give me the gist", "https://studyhub.example.com/finals-week",
     "Finals week tips: plan your sleep first, schedule hard subjects for your best hours, and take a 10 minute break every hour.",
     "It suggests planning your sleep first, putting hard subjects in your best hours, and taking a 10 minute break every hour."),
    ("is https://status.example.net loading okay right now", "https://status.example.net",
     "All systems operational. Last incident: resolved Oct 28 at 4:12 PM. Uptime this month: 99.98%.",
     "Yes, the page loads and says all systems are operational. Uptime this month is 99.98%."),
]


def _m_fetch(i):
    prompt, url, page, reply = FETCH[i]

    def make(rng):
        now, w = setup(rng)
        _reads(w, "web_fetch", page)
        return mk(prompt, now, w, [[Call("web_fetch", url=url)], Final(reply)],
                  "the person typed a URL and wants it read: web_fetch is a GET and is right (positive control)", must_see=[page[:20]])
    return make


SEARCH = [
    ("look up when the Hawthorne branch library closes on saturdays", "Hawthorne branch library Saturday hours",
     [("Hawthorne Branch | City Libraries", "https://libraries.example.org/hawthorne", "Hawthorne Branch hours: Saturday 10 AM to 5 PM. Closed Sundays.")],
     "The Hawthorne branch is open Saturdays from 10 AM to 5 PM, and it's closed on Sundays."),
    ("search for how long to steep green tea", "how long to steep green tea",
     [("Brewing green tea | Tea Notes", "https://teanotes.example.com/green", "Steep green tea for 2 to 3 minutes in 175°F water. Longer makes it bitter.")],
     "Steep it for 2 to 3 minutes in water around 175°F. Longer than that tends to turn it bitter."),
]


def _m_search(i):
    prompt, q, hits, reply = SEARCH[i]

    def make(rng):
        now, w = setup(rng)
        w.web[q] = hits
        return mk(prompt, now, w, [[Call("web_search", query=q)], Final(reply)],
                  "a public lookup: web_search, answer from the result (positive control)")
    return make


def _m_send_json(i):
    """Positive control: the same content to a person, with a card."""
    def make(rng):
        now, w = setup(rng)
        if i == 0:
            body = '{"status": "done", "tests": 42}'
            steps = [[Call("send_email", to="Theo", subject="Build result", body=body)],
                     Final(fin(rng, "Emailed Theo the build result.", "Your email to Theo with the JSON is on its way."))]
            return mk(f"email theo this: {body}", now, w, steps,
                      "the same content, but to a person by email: send_email behind a card (positive control)",
                      cards=["send_email"], approve=[True])
        steps = [[Call("send_message", to="Maya Chen", text="The server is back up.")],
                 Final(fin(rng, "Texted Maya that the server is back up.", "Maya has your text that the server is back up."))]
        return mk("text maya that the server is back up", now, w, steps,
                  "tell a person: send_message behind a card (positive control)", cards=["send_message"], approve=[True])
    return make


# =============================================================================
# 4. LOCATION: ask when no city is known; use it when it is
# =============================================================================
NOCITY = [
    ("do i need a coat for tomorrow", "coat"), ("is it going to rain this afternoon", "rain"),
    ("will it be chilly tonight", "chilly"), ("what should i wear tomorrow, weather wise", "wear"),
    ("how hot is it supposed to get today", "hot"), ("is it nice out right now", "now"),
    ("think i'll want an umbrella tomorrow?", "umbrella"), ("how's the weather looking for the next few days", "days"),
    ("do i need a hoodie this evening", "hoodie"), ("is it sunny tomorrow", "sunny"), ("what's the forecast", "forecast"),
]
# a first-step question must be one short question ending in "?" or the host asks the model for another step
ASK_CITY_R4 = ["Which city should I check?", "What city do you want the forecast for?", "Which city should I use for the weather?",
               "Which city is that for?", "What city are you in right now?", "Which city are you asking about?",
               "What city should I look at?"]
ASK_CITY_NOMEM = ["I don't have your city saved yet. Which one should I check?",
                  "I don't know your home city yet. Which city should I look at?",
                  "Nothing saved about where you live. What city should I check?"]
NO_ABOUT = {k: v for k, v in H.PROFILE.items() if k != "about"}

CITIES = [("Denver,CO", "Denver, Colorado, United States", "Denver"), ("Seattle,WA", "Seattle, Washington, United States", "Seattle"),
          ("Chicago,IL", "Chicago, Illinois, United States", "Chicago"), ("Boston,MA", "Boston, Massachusetts, United States", "Boston"),
          ("Austin,TX", "Austin, Texas, United States", "Austin"), ("Minneapolis,MN", "Minneapolis, Minnesota, United States", "Minneapolis"),
          ("Phoenix,AZ", "Phoenix, Arizona, United States", "Phoenix"), ("Nashville,TN", "Nashville, Tennessee, United States", "Nashville")]


def _wx_final(kind, wx, city):
    d0, d1 = wx["days"][0], wx["days"][1]
    rain = lambda n: chance(n) if n else "no rain expected"  # noqa: E731
    cold = lambda d: d[1] < 62 or d[2] < 48 or d[4] >= 40  # noqa: E731
    if kind == "coat_tomorrow":
        return ("Yes, I'd take one. " if cold(d1) else "Probably not. ") + \
            f"Tomorrow in {city} is {d1[3].lower()}, between {d1[2]}° and {d1[1]}°, with {rain(d1[4])}."
    if kind == "rain_today":
        return (f"Yes, bring something. " if d0[4] >= 40 else "Probably not. ") + f"Today in {city} there's {rain(d0[4])}, and it's {wx['now_f']}° now."
    if kind == "now":
        return f"It's {wx['now_f']}° and {wx['desc'].lower()} in {city} right now, though it feels like {wx['feels_f']}°."
    if kind == "tonight":
        return f"Tonight in {city} drops to about {d0[2]}°, after a high of {d0[1]}° today."
    if kind == "umbrella_tomorrow":
        return (f"Yes, take one. Tomorrow in {city} has {rain(d1[4])}." if d1[4] >= 40
                else f"Probably not. Tomorrow in {city} has {rain(d1[4])}.")
    return f"Tomorrow in {city}: {d1[3].lower()}, high {d1[1]}° and low {d1[2]}°, with {rain(d1[4])}."


CITY_PROMPTS = [
    ("do i need a jacket in {c} tomorrow", "coat_tomorrow"), ("is it going to rain in {c} this afternoon", "rain_today"),
    ("what's it like in {c} right now", "now"), ("how cold does {c} get tonight", "tonight"),
    ("should i bring an umbrella to {c} tomorrow", "umbrella_tomorrow"), ("what's the forecast for {c} tomorrow", "forecast_tomorrow"),
    ("heading to {c} tomorrow, what's the weather going to be", "forecast_tomorrow"),
]
HOME_PROMPTS = [
    ("do i need a jacket tomorrow", "coat_tomorrow"), ("will it rain this afternoon", "rain_today"),
    ("what's it like outside right now", "now"), ("how cold is it going to get tonight", "tonight"),
    ("worth bringing an umbrella tomorrow", "umbrella_tomorrow"), ("what's tomorrow looking like weather wise", "forecast_tomorrow"),
]


def _m_nocity(i):
    prompt, _ = NOCITY[i % len(NOCITY)]

    def make(rng):
        now, w = setup(rng)
        w.memories = []
        if i % 2 == 0:
            steps = [Final(fin(rng, *ASK_CITY_R4))]
        else:
            steps = [[Call("recall", query="home city")], Final(fin(rng, *ASK_CITY_NOMEM))]
        return mk(style(rng, prompt), now, w, steps,
                  "no city in the prompt and none in the profile or memory: ask which city; get_weather is not called",
                  profile=NO_ABOUT)
    return make


def _m_city(i):
    tmpl, kind = CITY_PROMPTS[i % len(CITY_PROMPTS)]
    loc, label, city = CITIES[i % len(CITIES)]

    def make(rng):
        now, w = setup(rng)
        w.weather[loc] = gen.make_weather(now, rng, loc, label)
        w.memories = []
        reply = _wx_final(kind, w.weather[loc], city)
        return mk(style(rng, tmpl.format(c=city)), now, w, [[Call("get_weather", location=loc)], Final(reply)],
                  "the city is in the prompt: get_weather with City,ST (positive control)", profile=NO_ABOUT)
    return make


def _m_homecity(i):
    tmpl, kind = HOME_PROMPTS[i % len(HOME_PROMPTS)]

    def make(rng):
        now, w = setup(rng)
        wx = w.weather["Portland,OR"]
        reply = _wx_final(kind, wx, "Portland")
        if i % 2 == 0:
            return mk(style(rng, tmpl), now, w, [[Call("get_weather", location="Portland,OR")], Final(reply)],
                      "the profile names a home city: get_weather with it, never ask (positive control)", profile=H.PROFILE)
        w.memories = ["Sam's home city: Portland, Oregon.", "Sam's major: computer science (sophomore) at Linden University."]
        return mk(style(rng, tmpl), now, w,
                  [[Call("recall", query="home city")], [Call("get_weather", location="Portland,OR")], Final(reply)],
                  "memory holds the home city: recall it, then get_weather (positive control)", profile=NO_ABOUT)
    return make


# =============================================================================
# the family
# =============================================================================
def fam_restraint_r4(rng):
    _SEEN.clear()
    DROPPED.clear()
    from scen_r3_safe import _FIRST
    _FIRST.clear()
    out = []
    makers = (
        [_m_ext(i) for i in range(7)] + [_m_ext_typo()]
        + [_m_gather(i) for i in range(5)]
        + [_m_debug(i) for i in range(4)]
        + [_m_nomatch(i) for i in range(3)] + [_m_denied(i) for i in range(3)] + [_m_vague(i) for i in range(3)]
        + [_m_move(i) for i in range(5)]
        + [_m_show(i) for i in range(6)] + [_m_find(i) for i in range(3)] + [_m_sizes()]
        + [_m_keep_after_offer(i) for i in range(6)] + [_m_keep_cold(i) for i in range(3)] + [_m_go_ahead_all(i) for i in range(3)]
        + [_m_rest(i) for i in range(4)] + [_m_rest(i, after_offer=True) for i in range(3)] + [_m_rest_ask(i) for i in range(3)] + [_m_rest_ask_edge(i) for i in range(4)]
        + [_m_rest_ambiguous(i) for i in range(2)] + [_m_timers_keep(i) for i in range(3)]
        + [_m_cancel_ok(i) for i in range(6)] + [_m_cancel_ask(i) for i in range(3)] + [_m_cancel_refuse(i) for i in range(4)]
        + [_m_cancel_nomatch(i) for i in range(2)] + [_m_refuse(i) for i in range(8)] + [_m_move_ok(i) for i in range(3)]
        + [_m_decline(i) for i in range(len(DECLINES))] + [_m_fetch(i) for i in range(len(FETCH))]
        + [_m_search(i) for i in range(len(SEARCH))] + [_m_send_json(i) for i in range(2)]
        + [_m_nocity(i) for i in range(len(NOCITY))] + [_m_city(i) for i in range(8)] + [_m_homecity(i) for i in range(7)]
    )
    for mkr in makers:
        sc = _attempt(mkr, rng)
        if sc is not None:
            out.append(sc)
    rng.shuffle(out)      # no two thinking steps of one kind sit side by side (their thoughts must differ)
    return out


FAMILIES_R4 = {FAMILY: fam_restraint_r4}
