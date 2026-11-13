"""Who a piece of mail is really from: one strict reading of a From value, for every caller.

A sender writes their own display name, and it can be made to look like someone else's
address: `"Dean <dean@school.edu>" <x@evil.com>`, `Dean <dean@school.edu> <"x>"@evil.com>`,
full-width brackets, an encoded word that decodes to an address. Code that picks "the first
address in brackets", or any address it can find, reads the posed one. Everything that needs
a mail sender's address, or decides whether a name can be shown, goes through here.

IMPORTANT rules:
- The real address is the parsed field a reader supplies (`sender_address`) when that is one
  clean address. Otherwise it is the ONE bracketed address at the very END of the From
  value, and only if what is inside the brackets is one clean address. Otherwise the whole
  value, if it is nothing but one clean address. Anything else has NO address ("") — never
  a guess, and never an address found elsewhere in the text.
- A sender is ODD when the display name carries "@", "<" or ">" (after Unicode folding, so
  full-width and look-alike brackets count, and after decoding encoded words), when there is
  address-like text but no address this module accepts, or when the parsed field is present
  but not one clean address. One exception: a name that is exactly the real address
  (`"maya@x.com" <maya@x.com>`) is not odd.
- An odd sender is shown by its real address alone, never by its name, and nothing is done
  on its strength without the person's tap. A row is "the user's own" only when its real
  address is theirs AND it is not odd.
- This reads a header. It does not prove who sent the mail: a From line can be forged
  outright where the sender's domain does not enforce DMARC. Callers that would ACT on a
  sender's identity need more than this.
"""
from __future__ import annotations

import re
import unicodedata

# One addr-spec, strictly: no spaces, quotes, brackets, commas or a second "@".
_CLEAN = re.compile(r"[^\s<>@,;:\"'()\[\]\\]+@[^\s<>@,;:\"'()\[\]\\]+\.[^\s<>@,;:\"'()\[\]\\]+|"
                    r"[^\s<>@,;:\"'()\[\]\\]+@[^\s<>@,;:\"'()\[\]\\.]+")
_FINAL_ANGLE = re.compile(r"<([^<>]*)>\s*$")
_LOOKALIKE = str.maketrans({"‹": "<", "›": ">", "〈": "<", "〉": ">", "⟨": "<", "⟩": ">", "«": "<", "»": ">",
                            "﹤": "<", "﹥": ">", "﹫": "@"})


def _fold(value) -> str:
    """NFKC (full-width ＜ ＞ ＠ become < > @), look-alike brackets mapped, encoded words decoded."""
    text = str(value or "")
    if "=?" in text:
        try:
            from email.header import decode_header, make_header
            text = str(make_header(decode_header(text)))
        except Exception:  # noqa: BLE001 — undecodable: read it as written
            pass
    return unicodedata.normalize("NFKC", text).translate(_LOOKALIKE)


def _clean(value) -> str:
    text = str(value or "").strip()
    return text.casefold() if _CLEAN.fullmatch(text) else ""


def _split(sender, sender_address="") -> tuple[str, str, bool]:
    """(display name, real address or "", the value is malformed)."""
    text = _fold(sender).strip()
    given_raw = str(sender_address or "").strip()
    given = _clean(_fold(given_raw))
    if given_raw and not given:
        return text, "", True
    final = _FINAL_ANGLE.search(text)
    inside = _clean(final.group(1)) if final else ""
    if given:
        # The reader parsed the address itself. The From value is then name (+ maybe that address).
        name = text[:final.start()] if final and inside == given else ("" if _clean(text) == given else text)
        return name, given, False
    if final and inside:
        return text[:final.start()], inside, False
    if _clean(text):
        return "", _clean(text), False
    # No address this module accepts. Address-like text with no usable address is malformed.
    return text, "", any(mark in text for mark in "@<>")


def real_address(sender, sender_address="") -> str:
    """The address the mail was really sent from, casefolded, or "" when there is none to trust."""
    return _split(sender, sender_address)[1]


def is_odd(sender, sender_address="") -> bool:
    """The display name is dressed up as an address, or the From value cannot be read cleanly."""
    name, address, malformed = _split(sender, sender_address)
    if malformed:
        return True
    bare = name.strip().strip("\"'").strip()
    if address and bare.casefold() == address:
        return False
    return any(mark in name for mark in "@<>")


def shown(sender, sender_address="", *, fallback: str = "Unknown sender") -> str:
    """How to show the sender: the plain name, or for an odd sender the real address alone."""
    name, address, _ = _split(sender, sender_address)
    if is_odd(sender, sender_address):
        return address or fallback
    plain = re.sub(r"\s+", " ", name).strip().strip("\"'").strip()
    return plain or address or fallback


def is_own(sender, sender_address, mine) -> bool:
    """The row is the user's own: its real address is one of theirs AND it is not odd."""
    address = real_address(sender, sender_address)
    return bool(address) and address in {str(m).casefold() for m in mine or ()} and not is_odd(sender, sender_address)
