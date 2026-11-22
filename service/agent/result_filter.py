"""Choose what to keep of a big tool result before the length cap cuts it.

`loop._fit_tool_result` used to keep the first N characters of anything over the
Ask cap (about 6,000). The first N rows are rarely the ones the question is
about: a mailbox read keeps the oldest mail, a file hunt the alphabetical head.
This module runs first, only when a result is already over the cap and only on
the Ask path, and keeps what the question needs:

1. Drop what the model never uses (an internal account id, a long recipient or
   attachment list, a file's byte size, a time of day, tracking parameters in a
   URL, quoted reply chains). Every id a follow-up tool call takes stays:
   `Message-ID` (reply_to_email, forward_email, email_triage), `Account:` (the
   `account` argument of reply_to_email), each URL (web_fetch), each path
   (read_file, list_dir, organize_files). Only `Account-ID:` is dropped: the
   reply envelope is built by Wisp, and no tool schema takes it from the model.
2. If it is still over the cap, rank the rows against the user's question and
   keep the best that fit. Local lexical scoring only, no model call, no
   randomness: sender/name hits count 3, subject/path hits 2, body hits 1 each
   up to 3, and the tie-break is recency (newer first), then the original
   position. Kept rows stay in their original order.
3. Say what is missing in one line: `412 more emails not shown, 3 from Ada
   Lovelace. This is NOT all of it ...`, placed outside every fence.

What does not change: the announced-cut note (`_fit_tool_result` still adds it
whenever the shaped text is still over the cap), the untrusted-content fences
(markers are copied verbatim and only text inside them is shortened, never the
markers; a shaped result whose markers are not exactly a subset of the original
is thrown away), and every safety or confirm check, which run on the tool's raw
result elsewhere. Any failure returns the original text untouched.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from service.tools.untrusted import (EMAIL_BODY, WEB_PAGE, begin_marker,
                                     end_marker)

__all__ = ["shape_result", "intent_for", "SHAPED_TOOLS"]

_SEP = "\n\n---\n\n"
_RESERVE = 420          # room kept for the remainder line
_MAX_QUESTION = 600
_MESSAGE_ROW_CHARS = 600
_SNIPPET_CHARS = 300
_CHUNK_CHARS = 1500
_ATTACHMENTS_SHOWN = 3
_TO_SHOWN = 2

_STOP = frozenset("""
the and for are was were with that this from have has had you your not but can could would should will
what whats when where who whom which why how did does doing done any all out about into than then them
they their there here him her his she its our ours mine tell show give find get got see look check read
please latest recent last new old email emails mail mails message messages text texts file files
folder folders page pages web site search query com www http https org net ago today yesterday
me my i it is be to of in on at as by an or if so up do no yes
""".split())


# --------------------------------------------------------------------------- intent

def intent_for(messages: list | None, args: dict | None = None) -> str:
    """The user's current question plus the tool call's own search words."""
    question = ""
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user" \
                and isinstance(message.get("content"), str):
            question = message["content"][:_MAX_QUESTION]
            break
    extra = []
    if isinstance(args, dict):
        for key in ("query", "conversation", "question", "name", "content"):
            value = args.get(key)
            if isinstance(value, str):
                extra.append(value[:200])
    return " ".join([question, *extra]).strip()


def _terms(intent: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", intent or "").casefold()
    seen: list[str] = []
    for word in re.findall(r"\w+", text):
        if word in _STOP or word in seen:
            continue
        if len(word) >= 3 or (len(word) == 2 and any(c.isdigit() for c in word)):
            seen.append(word)
    return tuple(seen)


def _score(terms: tuple[str, ...], who: str = "", what: str = "", body: str = "") -> int:
    who, what, body = who.casefold(), what.casefold(), body.casefold()
    total = 0
    for term in terms:
        if term in who:
            total += 3
        if term in what:
            total += 2
        if body:
            total += min(body.count(term), 3)
    return total


# --------------------------------------------------------------------------- rows

@dataclass
class _Row:
    text: str
    index: int
    score: int = 0
    recency: float = 0.0
    group: str = ""          # the sender, folder, extension or site, for the remainder line


def _choose(rows: list[_Row], budget: int, sep_len: int, *, trim=None) -> list[_Row]:
    """The best rows that fit `budget`, in original order. The top row is always
    kept (shortened by `trim(row, room)` when it alone is too long)."""
    ranked = sorted(rows, key=lambda r: (-r.score, -r.recency, r.index))
    kept: list[_Row] = []
    used = 0
    for row in ranked:
        cost = len(row.text) + sep_len
        if used + cost <= budget:
            kept.append(row)
            used += cost
        elif not kept:
            shortened = trim(row, max(200, budget - sep_len)) if trim else row
            kept.append(shortened)
            used += len(shortened.text) + sep_len
    kept.sort(key=lambda r: r.index)
    return kept


def _remainder(rows: list[_Row], kept: list[_Row], noun: str, how: str, group_phrase) -> str:
    left = [r for r in rows if r.index not in {k.index for k in kept}]
    if not left:
        return ""
    best = sorted(kept, key=lambda r: (-r.score, -r.recency, r.index))[0] if kept else None
    same = ""
    if best is not None and best.group:
        n = sum(1 for r in left if r.group.casefold() == best.group.casefold())
        if n:
            same = f", {n:,} {group_phrase(best.group)}"
    return (f"[{len(left):,} more {noun} not shown{same}. This is NOT all of it: do not describe "
            f"what you have as complete. To see the rest, {how}.]")


def _split_rows(text: str, is_row, *, joiner: str = "\n") -> tuple[str, list[str], str]:
    """(preamble, row chunks, trailer) of `text`. A line that is not a row start
    continues the row above it."""
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if is_row(ln)), None)
    if first is None:
        return text, [], ""
    pre = "\n".join(lines[:first])
    rows: list[list[str]] = []
    trailer_at = len(lines)
    for i in range(first, len(lines)):
        ln = lines[i]
        if is_row(ln):
            rows.append([ln])
        elif ln == "" and i + 1 < len(lines) and not is_row(lines[i + 1]) \
                and any(x.strip() for x in lines[i + 1:]):
            # a blank line followed by non-row text: the rest is Wisp's trailer
            trailer_at = i
            break
        elif ln.startswith(("…[", "(\"at least\"", "(+")) and rows:
            trailer_at = i
            break
        else:
            rows[-1].append(ln)
    trailer = "\n".join(lines[trailer_at:])
    return (pre + "\n" if pre else ""), ["\n".join(r) for r in rows], trailer


def _assemble_rows(pre: str, remainder: str, kept: list[_Row], trailer: str, sep: str = "\n") -> str:
    out = pre
    if remainder:
        out += remainder + "\n"
    out += sep.join(r.text for r in kept)
    if trailer:
        out += "\n" + trailer
    return out


# --------------------------------------------------------------------------- text chunks

def _chunks(body: str) -> list[str]:
    out: list[str] = []
    for part in body.split("\n\n"):
        if len(part) <= _CHUNK_CHARS:
            out.append(part)
            continue
        group = ""
        for line in part.split("\n"):
            while len(line) > _CHUNK_CHARS:
                if group:
                    out.append(group)
                    group = ""
                out.append(line[:_CHUNK_CHARS])
                line = line[_CHUNK_CHARS:]
            if group and len(group) + len(line) + 1 > _CHUNK_CHARS:
                out.append(group)
                group = line
            else:
                group = f"{group}\n{line}" if group else line
        if group:
            out.append(group)
    return out


_GAP = "[… {n} sections not shown]"


def _select_text(body: str, terms: tuple[str, ...], budget: int) -> tuple[str, int, int]:
    """(text, sections shown, sections total). The first section always stays."""
    parts = _chunks(body)
    rows = [_Row(p, i, _score(terms, body=p) if i else 10**6) for i, p in enumerate(parts)]
    kept = _choose(rows, budget, 2 + len(_GAP.format(n=999)) + 2)
    shown = {r.index for r in kept}
    out: list[str] = []
    gap = 0
    for i, part in enumerate(parts):
        if i in shown:
            if gap:
                out.append(_GAP.format(n=gap))
                gap = 0
            out.append(kept[[k.index for k in kept].index(i)].text)
        else:
            gap += 1
    if gap:
        out.append(_GAP.format(n=gap))
    return "\n\n".join(out), len(shown), len(parts)


def _shorten(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    return text[:max(0, room - 40)].rstrip() + f" … [+{len(text) - room + 40:,} chars]"


# --------------------------------------------------------------------------- mail

_MAIL_HEADER = re.compile(r"^(?:Account: [^\n]*\n)?(?:Account-ID: [^\n]*\n)?From: [^\n]*\nTo: ", re.M)
_MAIL_DATE = "%a %b %d, %Y %I:%M %p"
_ATTACHED = re.compile(r"^\[Attached: (.*)\]$")
_QUOTED = re.compile(r"^\s*>")


def _fence_depth_positions(text: str, begin: str, end: str):
    events = sorted([(m.start(), 1) for m in re.finditer(r"^" + re.escape(begin) + r"$", text, re.M)]
                    + [(m.start(), -1) for m in re.finditer(r"^" + re.escape(end) + r"$", text, re.M)])
    depths, depth = [], 0
    for _, delta in events:
        depth += delta
        depths.append(depth)
    return [p for p, _ in events], depths


def _split_outside_fence(text: str, sep: str, begin: str, end: str) -> list[str]:
    """Split on `sep` only where no fence is open, so a "---" in an email body
    never splits it."""
    positions, depths = _fence_depth_positions(text, begin, end)
    parts, start, at = [], 0, 0
    while True:
        i = text.find(sep, at)
        if i < 0:
            break
        k = bisect.bisect_right(positions, i) - 1
        if (depths[k] if k >= 0 else 0) == 0:
            parts.append(text[start:i])
            start = at = i + len(sep)
        else:
            at = i + 1
    parts.append(text[start:])
    return parts


def _tidy_attachments(line: str) -> str:
    match = _ATTACHED.match(line.strip())
    if not match:
        return line
    inner = match.group(1)
    more = 0
    tail = re.search(r", \+(\d+) more$", inner)
    if tail:
        more = int(tail.group(1))
        inner = inner[:tail.start()]
    items = re.findall(r"(.+?\([^()]*\))(?:, |$)", inner)
    if not items or len(items) + more <= _ATTACHMENTS_SHOWN:
        return line
    hidden = len(items) - _ATTACHMENTS_SHOWN + more
    return f"[Attached: {', '.join(items[:_ATTACHMENTS_SHOWN])}, +{hidden} more (read_email_attachments list_only lists them)]"


def _tidy_mail_body(body: str) -> str:
    out: list[str] = []
    quoted = 0
    blank = 0

    def flush_quoted() -> None:
        nonlocal quoted
        if quoted >= 3:
            out.append(f"[… {quoted} quoted lines omitted]")
        elif quoted:
            out.extend(["> …"] * quoted)
        quoted = 0

    for line in body.split("\n"):
        if _QUOTED.match(line):
            quoted += 1
            blank = 0
            continue
        flush_quoted()
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(_tidy_attachments(line) if line.startswith("[Attached:") else line)
    flush_quoted()
    return "\n".join(out)


def _clip_recipients(value: str) -> str:
    people = [p.strip() for p in value.split(",") if p.strip()]
    if len(people) <= _TO_SHOWN:
        return value
    return ", ".join(people[:_TO_SHOWN]) + f" (+{len(people) - _TO_SHOWN} more)"


def _mail_header(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if line.startswith("Account-ID: "):
            continue
        if line.startswith("To: "):
            line = "To: " + _clip_recipients(line[4:])
        out.append(line)
    return out


def _shape_mail(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    begin, end = begin_marker(EMAIL_BODY), end_marker(EMAIL_BODY)
    if begin not in result:
        return _shape_mail_lines(result, limit, terms)
    parts = _split_outside_fence(result, _SEP, begin, end)
    first = _MAIL_HEADER.search(parts[0])
    if first is None:
        return None
    preamble = parts[0][:first.start()]
    parts[0] = parts[0][first.start():]
    trailer = ""
    last_end = parts[-1].rfind("\n" + end)
    if last_end < 0:
        return None
    cut = last_end + 1 + len(end)
    trailer, parts[-1] = parts[-1][cut:], parts[-1][:cut]

    rows: list[_Row] = []
    bodies: dict[int, tuple[list[str], str]] = {}
    for i, block in enumerate(parts):
        b = block.find("\n" + begin + "\n")
        e = block.rfind("\n" + end)
        if b < 0 or e < b or block[e + 1 + len(end):].strip():
            return None
        header = _mail_header(block[:b].split("\n"))
        body = _tidy_mail_body(block[b + 2 + len(begin):e])
        head_text = "\n".join(header)
        sender = next((ln[6:] for ln in header if ln.startswith("From: ")), "")
        subject = next((ln[9:] for ln in header if ln.startswith("Subject: ")), "")
        when = next((ln[6:] for ln in header if ln.startswith("Date: ")), "")
        try:
            stamp = datetime.strptime(when.strip(), _MAIL_DATE).timestamp()
        except ValueError:
            stamp = 0.0
        text = f"{head_text}\n{begin}\n{body}\n{end}"
        bodies[i] = (header, body)
        rows.append(_Row(text, i, _score(terms, sender, subject, body), stamp, sender))

    def render(row: _Row, body_room: int | None = None) -> _Row:
        header, body = bodies[row.index]
        if body_room is not None:
            body, _shown, _all = _select_text(body, terms, body_room)
            body = _shorten(body, body_room)
        return _Row(f"{chr(10).join(header)}\n{begin}\n{body}\n{end}", row.index, row.score,
                    row.recency, row.group)

    budget = limit - len(preamble) - len(trailer) - _RESERVE
    total = sum(len(r.text) for r in rows) + len(_SEP) * (len(rows) - 1)
    if total <= limit - len(preamble) - len(trailer):
        kept, remainder = rows, ""
    else:
        def trim(row: _Row, room: int) -> _Row:
            overhead = len(render(row, 0).text)
            return render(row, max(200, room - overhead))
        kept = _choose(rows, budget, len(_SEP), trim=trim)
        remainder = _remainder(rows, kept, "emails",
                               "ask for them by `query`, `day` or a smaller `count`",
                               lambda g: f"from {g}")
    out = preamble + (remainder + "\n\n" if remainder else "") + _SEP.join(r.text for r in kept) + trailer
    return out


_MAIL_LINE = re.compile(r"^- \w{3} \w{3} \d{1,2}, \d{4} ")


def _shape_mail_lines(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    pre, chunks, trailer = _split_rows(result, lambda ln: bool(_MAIL_LINE.match(ln)))
    if len(chunks) < 2:
        return None
    rows = []
    for i, chunk in enumerate(chunks):
        fields = chunk.split(" | ")
        sender = fields[1] if len(fields) > 1 else ""
        subject = fields[2] if len(fields) > 2 else ""
        try:
            stamp = datetime.strptime(fields[0][2:].strip(), _MAIL_DATE).timestamp()
        except ValueError:
            stamp = 0.0
        rows.append(_Row(chunk, i, _score(terms, sender, subject), stamp, sender.split(" <")[0]))
    kept = _choose(rows, limit - len(pre) - len(trailer) - _RESERVE, 1)
    remainder = _remainder(rows, kept, "emails", "ask for them by `query`, `day` or a smaller `count`",
                           lambda g: f"from {g}")
    return _assemble_rows(pre, remainder, kept, trailer)


# --------------------------------------------------------------------------- messages

_MESSAGE_ROW = re.compile(r"^\[[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M\] ")


def _shape_messages(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    pre, chunks, trailer = _split_rows(result, lambda ln: bool(_MESSAGE_ROW.match(ln)))
    if len(chunks) < 2:
        return None
    rows = []
    for i, chunk in enumerate(chunks):
        head, dash, text = chunk.partition(" — ")
        ctx = head.split("] ", 1)[1] if "] " in head else ""
        if dash and len(text) > _MESSAGE_ROW_CHARS:
            text = _shorten(text, _MESSAGE_ROW_CHARS)
            chunk = f"{head}{dash}{text}"
        # The rows are in time order, so a later row is a newer one.
        rows.append(_Row(chunk, i, _score(terms, ctx, "", text), float(i), ctx))
    if sum(len(r.text) + 1 for r in rows) + len(pre) + len(trailer) <= limit:
        kept, remainder = rows, ""
    else:
        kept = _choose(rows, limit - len(pre) - len(trailer) - _RESERVE, 1)
        remainder = _remainder(rows, kept, "messages",
                               "ask for them by `query`, `conversation`, `period` or a smaller `count`",
                               lambda g: f"from {g}")
    return _assemble_rows(pre, remainder, kept, trailer)


# --------------------------------------------------------------------------- files

_FIND_ROW = re.compile(r"^(?:~|/)\S.*  \([^()]*\)$")
_SIZE = re.compile(r"\(\d+(?:B|KB|MB|GB), modified ")
_MODIFIED = re.compile(r"modified (\w{3} \d{1,2}, \d{4})")


def _date_stamp(text: str) -> float:
    match = _MODIFIED.search(text)
    if not match:
        return 0.0
    try:
        return datetime.strptime(match.group(1), "%b %d, %Y").timestamp()
    except ValueError:
        return 0.0


def _shape_find_files(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    pre, chunks, trailer = _split_rows(result, lambda ln: bool(_FIND_ROW.match(ln)))
    if len(chunks) < 2:
        return None
    rows = []
    for i, chunk in enumerate(chunks):
        chunk = _SIZE.sub("(modified ", chunk)
        path = chunk.split("  (", 1)[0]
        name = path.rsplit("/", 1)[-1]
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        # Spotlight already ordered these best first: a lower index wins ties.
        rows.append(_Row(chunk, i, _score(terms, name, folder), _date_stamp(chunk) - i * 1e-3, folder))
    if sum(len(r.text) + 1 for r in rows) + len(pre) + len(trailer) <= limit:
        kept, remainder = rows, ""
    else:
        kept = _choose(rows, limit - len(pre) - len(trailer) - _RESERVE, 1)
        remainder = _remainder(rows, kept, "files",
                               "narrow with `folder`, `kind` or a more specific query",
                               lambda g: "in the same folder")
    return _assemble_rows(pre, remainder, kept, trailer)


_DIR_TIME = re.compile(r"(modified \w{3} \d{1,2}, \d{4}) \d{1,2}:\d{2} [AP]M")


def _shape_list_dir(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    lines = result.split("\n")
    end = len(lines)
    while end and (lines[end - 1].startswith(("…[", "(\"at least\"")) or not lines[end - 1].strip()):
        end -= 1
    rows_text = [_DIR_TIME.sub(r"\1", ln) for ln in lines[:end]]
    trailer = "\n".join(lines[end:]).strip("\n")
    if len(rows_text) < 2:
        return None
    rows = []
    for i, text in enumerate(rows_text):
        name = re.split(r"  +", text, maxsplit=1)[0].rstrip("/")
        leaf = name.rsplit("/", 1)[-1]
        ext = ("." + leaf.rsplit(".", 1)[1].casefold()) if "." in leaf.strip(".") and not text.split("  ")[0].endswith("/") else ""
        rows.append(_Row(text, i, _score(terms, leaf, name), _date_stamp(text) - i * 1e-3, ext))
    if sum(len(r.text) + 1 for r in rows) + len(trailer) <= limit:
        kept, remainder = rows, ""
    else:
        kept = _choose(rows, limit - len(trailer) - _RESERVE, 1)
        remainder = _remainder(rows, kept, "entries", "list a subfolder, or use find_files",
                               lambda g: f"that are {g} files")
    return _assemble_rows("", remainder, kept, trailer)


def _shape_text_body(result: str, limit: int, terms: tuple[str, ...], *, fenced: bool) -> str | None:
    """read_file (bare text) and web_fetch (header, fenced page, tail)."""
    if not fenced:
        room = limit - _RESERVE
        text, shown, total = _select_text(result, terms, room)
        if shown >= total:
            return None
        return (f"[Showing {shown:,} of {total:,} sections of this file: the first one and the ones "
                f"closest to the question, in their original order. This is NOT all of it: do not "
                f"describe what you have as complete.]\n\n{text}")
    begin, end = begin_marker(WEB_PAGE), end_marker(WEB_PAGE)
    b = result.find("\n" + begin + "\n")
    e = result.rfind("\n" + end)
    if b < 0 or e < b:
        return None
    head = result[:b + 1]
    body = result[b + 2 + len(begin):e]
    tail = result[e + 1 + len(end):]
    room = limit - len(head) - len(tail) - len(begin) - len(end) - _RESERVE - 4
    text, shown, total = _select_text(body, terms, room)
    if shown >= total:
        return None
    note = (f"[Showing {shown:,} of {total:,} sections of this page: the first one and the ones "
            f"closest to the question, in their original order. This is NOT all of it: do not "
            f"describe what you have as complete.]\n")
    return f"{head}{note}\n{begin}\n{text}\n{end}{tail}"


# --------------------------------------------------------------------------- web search

_SEARCH_ROW = re.compile(r"^(?:\[\d+\]|\d+\.) ")
_TRACKING = re.compile(r"^(?:utm_.*|fbclid|gclid|mc_eid|mc_cid|igshid|ref_src|ocid)$", re.I)


def _clean_url(line: str) -> str:
    if not line.startswith("URL: "):
        return line
    url = line[5:].strip()
    try:
        parts = urlsplit(url)
        if not parts.query:
            return line
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _TRACKING.match(k)]
        if len(kept) == len(parse_qsl(parts.query, keep_blank_values=True)):
            return line
        return "URL: " + urlunsplit(parts._replace(query=urlencode(kept)))
    except ValueError:
        return line


def _shape_web_search(result: str, limit: int, terms: tuple[str, ...]) -> str | None:
    blocks = result.split("\n\n")
    first = next((i for i, b in enumerate(blocks) if _SEARCH_ROW.match(b)), None)
    if first is None or len(blocks) - first < 2:
        return None
    pre = "\n\n".join(blocks[:first])
    rows = []
    for i, block in enumerate(blocks[first:]):
        lines = [_clean_url(ln) for ln in block.split("\n")]
        head = lines[0]
        url = next((ln[5:] for ln in lines if ln.startswith("URL: ")), "")
        snippet = "\n".join(ln for ln in lines[1:] if not ln.startswith("URL: "))
        snippet = _shorten(snippet, _SNIPPET_CHARS) if len(snippet) > _SNIPPET_CHARS else snippet
        url_lines = [ln for ln in lines[1:] if ln.startswith("URL: ")]
        text = "\n".join([head, *url_lines] + ([snippet] if snippet else []))
        host = (urlsplit(url).hostname or "") if url else ""
        rows.append(_Row(text, i, _score(terms, head, host, snippet), -float(i), host))
    kept_all = sum(len(r.text) for r in rows) + 2 * len(rows) + len(pre)
    if kept_all <= limit:
        kept, remainder = rows, ""
    else:
        kept = _choose(rows, limit - len(pre) - _RESERVE, 2)
        remainder = _remainder(rows, kept, "results", "search again with a more specific query",
                               lambda g: "from the same site")
    out = pre + ("\n\n" if pre else "")
    if remainder:
        out += remainder + "\n\n"
    return out + "\n\n".join(r.text for r in kept)


# --------------------------------------------------------------------------- entry point

_SHAPERS = {
    "view_emails": _shape_mail,
    "view_messages": _shape_messages,
    "find_files": _shape_find_files,
    "list_dir": _shape_list_dir,
    "read_file": lambda r, n, t: _shape_text_body(r, n, t, fenced=False),
    "web_fetch": lambda r, n, t: _shape_text_body(r, n, t, fenced=True),
    "web_search": _shape_web_search,
}
SHAPED_TOOLS = frozenset(_SHAPERS)

_MARKERS = re.compile(r"^<<<(?:end )?untrusted [^\n]*>>>$", re.M)


def shape_result(result: str, name: str, limit: int, intent: str = "") -> str:
    """`result` shortened to fit `limit` by dropping unneeded fields and ranking
    rows against `intent`; the original when `name` has no shaper, the result
    already fits, anything goes wrong, or the shaped text is not smaller."""
    shaper = _SHAPERS.get(name)
    text = str(result)
    if shaper is None or len(text) <= limit:
        return result
    try:
        out = shaper(text, limit, _terms(intent))
    except Exception:  # noqa: BLE001 — shaping is an optimisation, never a failure
        return result
    if not out or len(out) >= len(text):
        return result
    # Fences: every marker line in the output must come from the original, and
    # the fences must still balance.
    original = _MARKERS.findall(text)
    shaped = _MARKERS.findall(out)
    pool = list(original)
    for marker in shaped:
        if marker not in pool:
            return result
        pool.remove(marker)
    begins = sum(1 for m in shaped if not m.startswith("<<<end "))
    if begins != len(shaped) - begins:
        return result
    return out
