"""Was a piece of mail really sent from the domain in its From line? The receiving provider's word only.

A From header proves nothing: anyone can write any From. What can count is the check the
person's OWN mail provider made when the mail arrived, recorded in the `Authentication-Results`
header (RFC 8601) it adds. This module reads those headers and answers one question:

    authenticated(headers, *, from_address, provider) -> (bool, reason)

`headers` are the message's Authentication-Results values in the order the reader got them;
`from_address` is the sender's real address as service/mail_sender.py reads it (one clean
address, or "" when there is none to trust); `provider` names the receiving provider of the
account the mail was read from ("gmail"). `reason` is a short fixed machine string for debug
notes only; it never carries header text, addresses or domains.

IMPORTANT rules:
- ONLY a result written by the receiving provider counts. A header's authserv-id (the token
  before its first ";") must be the provider's own (`mx.google.com` for Gmail). Every header
  with any other authserv-id is IGNORED: a sender can put any header it likes into its own
  message, including one that claims a pass.
- Position is NOT relied on. Neither Gmail's API documentation nor this codebase establishes
  that repeated headers come back in message order, so instead EXACTLY ONE header may carry
  the provider's authserv-id. Two or more is no proof ("more than one result claims to be
  your provider's"): one of them was written by someone else. None is no proof ("not checked
  by your mail provider").
  Why that is enough: RFC 8601 section 5 requires a border MTA to delete (or rename) any
  incoming Authentication-Results header that claims its own authserv-id. Google does not, to
  our knowledge, publicly document whether Gmail does this. If Gmail does, a forged
  `mx.google.com` header never survives and Gmail's own is the only one. If it does not, the
  forged header sits beside Gmail's own and the count is two, which this module refuses. The
  residual risk is a message that carries a forged provider header and NO genuine one, which
  would need the provider itself to skip writing its result while keeping the sender's.
- A pass is one of, from that one header:
  - `dmarc=pass` whose `header.from` domain is ALIGNED with the From domain;
  - `dkim=pass` whose signing domain (`header.d`, else the domain of `header.i`) is aligned;
  - `spf=pass` whose `smtp.mailfrom` domain is aligned.
  A dkim or spf pass for an UNRELATED domain (spoofed mail sent through a bulk service passes
  both, for the service's own domain) is NOT a pass.
- Explicit failures win. Any `dmarc=fail`, or a dmarc result this module does not know, is a
  refusal even if a dkim or spf token says pass. `dmarc=pass` whose `header.from` is NOT
  aligned with the From address means the provider read a different From: refused.
  `dmarc=none`, `temperror` or `permerror` falls through to the aligned dkim/spf rule.
- Alignment (a conservative stand-in for DMARC's organizational domain, no Public Suffix
  List): an exact domain match always aligns. A parent/child pair (`news.shop.com` and
  `shop.com`) aligns only when the parent has at least two labels, is not a registry suffix
  (`co.uk`, `com.au`, any `<co|com|ac|gov|...>.<two-letter country>`), and is not, nor sits
  under, a shared-hosting suffix (`github.io`, `amazonaws.com`, ...) where every tenant
  controls its own subdomain. Siblings (`a.shop.com` and `b.shop.com`) do NOT align. When
  unsure, not aligned. This wrongly refuses some real mail, always toward a tap card: mail
  whose DKIM/SPF domain is a sibling of the From domain, registered domains under rarer
  multi-label public suffixes the short lists do not name when they rely on relaxed
  alignment (exact matches are fine), and tenants on a shared host that sign with the parent.
- Missing, empty, non-string, unparseable or oversized (`MAX_HEADER_CHARS`) input, an empty
  or malformed from_address, or an unknown provider -> refused. Parsing unfolds header lines,
  drops RFC 5322 comments (nested, with escapes) and reads quoted strings as one value, so
  text inside a comment or a quoted string (`dkim=fail (dmarc=pass header.from=x)`,
  `reason="dmarc=pass"`) is never read as a result. An unterminated comment or quote, or a
  result clause that does not parse, makes the whole header unparseable: no proof.
  ANY entry in `headers` that is not a string, is oversized, or cannot be tokenized refuses
  the whole set (it could stand where the provider's own result was). Another host's header
  whose authserv-id can be read is set aside even when its results cannot; an id-less header
  (Microsoft writes `spf=pass ...;` with no authserv-id) is set aside as not the provider's.
"""
from __future__ import annotations

import re

MAX_HEADER_CHARS = 8192
MAX_HEADERS = 64
MAX_RESULTS = 64

# Receiving provider -> its authserv-ids. Gmail and Google Workspace both stamp mx.google.com.
PROVIDER_AUTHSERV_IDS: dict[str, frozenset[str]] = {
    "gmail": frozenset({"mx.google.com"}),
}

# Registry suffixes: refused as a parent only when the parent IS one of these.
_REGISTRY_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "co.nz", "co.in", "co.za", "com.br", "com.cn", "com.mx", "com.tw", "com.hk", "com.sg",
    "co.kr", "or.kr", "com.tr", "com.ar", "co.il", "com.my", "com.ph", "com.vn", "co.id",
})
# Second-level labels that, under a two-letter country TLD, are treated as registry suffixes.
_REGISTRY_SECOND_LEVEL = frozenset({
    "co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go", "gob", "mil", "nic",
    "ltd", "plc", "sch", "nom", "gen", "info", "biz", "me", "nhs", "police",
})
# Shared-hosting suffixes: refused as a parent when the parent is one of these OR under one.
_HOSTED_SUFFIXES = frozenset({
    "github.io", "githubusercontent.com", "gitlab.io", "herokuapp.com", "herokudns.com",
    "appspot.com", "blogspot.com", "cloudfront.net", "amazonaws.com", "elasticbeanstalk.com",
    "azurewebsites.net", "cloudapp.net", "azureedge.net", "trafficmanager.net", "windows.net",
    "web.app", "firebaseapp.com", "pages.dev", "workers.dev", "netlify.app", "netlify.com",
    "vercel.app", "now.sh", "wixsite.com", "squarespace.com", "weebly.com", "wordpress.com",
    "tumblr.com", "myshopify.com", "glitch.me", "fly.dev", "onrender.com", "repl.co",
    "ngrok.io", "ngrok-free.app", "duckdns.org", "dyndns.org", "no-ip.org", "sharepoint.com",
    "onmicrosoft.com", "googleusercontent.com", "surge.sh", "readthedocs.io", "pythonanywhere.com",
})

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LOCAL = re.compile(r"[^\s<>@,;:\"'()\[\]\\]+")

_SEMI, _EQ, _WORD, _QUOTED = "semi", "eq", "word", "quoted"


class _Unparseable(Exception):
    pass


def _domain(value) -> str:
    """A lowercase DNS name with at least two valid labels, or ""."""
    text = str(value or "").strip().rstrip(".").casefold()
    if not text or len(text) > 253:
        return ""
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return ""
    labels = text.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return ""
    return text


def _address_domain(value) -> str:
    """The domain of `local@domain` (or `@domain`, as header.i writes it), or ""."""
    text = str(value or "").strip()
    if text.count("@") != 1:
        return ""
    local, domain = text.split("@")
    if local and not _LOCAL.fullmatch(local):
        return ""
    return _domain(domain)


def _is_registry_suffix(domain: str) -> bool:
    labels = domain.split(".")
    if domain in _REGISTRY_SUFFIXES:
        return True
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in _REGISTRY_SECOND_LEVEL


def _is_hosted(domain: str) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in _HOSTED_SUFFIXES)


def aligned(a: str, b: str) -> bool:
    """Do two domains belong to the same organization, by the conservative rule above?"""
    a, b = _domain(a), _domain(b)
    if not a or not b:
        return False
    if a == b:
        return True
    parent, child = (a, b) if len(a) < len(b) else (b, a)
    if not child.endswith("." + parent):
        return False
    if len(parent.split(".")) < 2 or _is_registry_suffix(parent) or _is_hosted(parent):
        return False
    return True


# --- parsing --------------------------------------------------------------------

def _tokens(text: str) -> list[tuple[str, str, bool]]:
    """(kind, value, joined-to-previous) tokens; comments dropped, quoted strings kept whole."""
    out: list[tuple[str, str, bool]] = []
    i, n = 0, len(text)
    gap = True
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            gap = True
            i += 1
        elif c == "(":
            depth = 0
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if i >= n:
                raise _Unparseable("unterminated comment")
            i += 1
            gap = True
        elif c == ")":
            raise _Unparseable("stray parenthesis")
        elif c == '"':
            i += 1
            value = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(text[i])
                i += 1
            if i >= n:
                raise _Unparseable("unterminated quote")
            i += 1
            out.append((_QUOTED, "".join(value), not gap))
            gap = False
        elif c == ";":
            out.append((_SEMI, ";", not gap))
            i += 1
            gap = True
        elif c == "=":
            out.append((_EQ, "=", not gap))
            i += 1
            gap = False
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()";=':
                j += 1
            out.append((_WORD, text[i:j], not gap))
            i = j
            gap = False
    return out


def _clause(tokens: list[tuple[str, str, bool]]) -> tuple[str, str, dict[str, list[str]]]:
    """One resinfo: method=result, then reason=... and ptype.property=value pairs."""
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        kind, key, _ = tokens[i]
        if kind != _WORD or i + 1 >= len(tokens) or tokens[i + 1][0] != _EQ or i + 2 >= len(tokens):
            raise _Unparseable("bad clause")
        vkind, value, _ = tokens[i + 2]
        if vkind not in (_WORD, _QUOTED):
            raise _Unparseable("bad clause")
        i += 3
        # An unquoted value may itself carry "=" (base64 in header.b): absorb what is joined to it.
        while i < len(tokens) and tokens[i][2] and tokens[i][0] in (_EQ, _WORD) and vkind == _WORD:
            value += tokens[i][1]
            i += 1
        pairs.append((key.casefold(), value))
    if not pairs:
        raise _Unparseable("empty clause")
    method, result = pairs[0]
    method = method.split("/", 1)[0]
    if not method or not result:
        raise _Unparseable("bad method")
    props: dict[str, list[str]] = {}
    for key, value in pairs[1:]:
        props.setdefault(key, []).append(value)
    return method, result.casefold(), props


def parse(header) -> tuple[str, list[tuple[str, str, dict[str, list[str]]]]]:
    """(authserv-id casefolded, [(method, result, properties)]). Raises _Unparseable."""
    if not isinstance(header, str):
        raise _Unparseable("not a string")
    if len(header) > MAX_HEADER_CHARS:
        raise _Unparseable("oversized")
    tokens = _tokens(header)
    segments: list[list[tuple[str, str, bool]]] = [[]]
    for token in tokens:
        if token[0] == _SEMI:
            segments.append([])
        else:
            segments[-1].append(token)
    head = segments[0]
    if not head or head[0][0] not in (_WORD, _QUOTED):
        raise _Unparseable("no authserv-id")
    authserv = head[0][1].strip().casefold()
    rest = head[1:]
    if rest and not (len(rest) == 1 and rest[0][0] == _WORD and rest[0][1].isdigit()):
        raise _Unparseable("bad authserv-id")    # e.g. Microsoft's id-less "spf=pass ..."
    if not authserv:
        raise _Unparseable("no authserv-id")
    clauses = [s for s in segments[1:] if s]
    if len(clauses) > MAX_RESULTS:
        raise _Unparseable("too many results")
    results = []
    for seg in clauses:
        if len(seg) == 1 and seg[0][0] == _WORD and seg[0][1].casefold() == "none":
            continue
        results.append(_clause(seg))
    return authserv, results


def _authserv_id(header) -> str | None:
    """The authserv-id alone, read leniently so another host's odd header can be set aside:
    "" for an id-less header (Microsoft writes `spf=pass ...;` with no id), None when the
    header cannot even be tokenized or is not a string."""
    if not isinstance(header, str) or len(header) > MAX_HEADER_CHARS:
        return None
    try:
        tokens = _tokens(header)
    except _Unparseable:
        return None
    if not tokens or tokens[0][0] not in (_WORD, _QUOTED):
        return None
    if len(tokens) > 1 and tokens[1][0] == _EQ:
        return ""
    return tokens[0][1].strip().casefold()


# --- the decision ------------------------------------------------------------------

def authenticated(headers, *, from_address, provider) -> tuple[bool, str]:
    """(True, reason) only when the receiving provider's own result shows the From domain passed."""
    ids = PROVIDER_AUTHSERV_IDS.get(str(provider or "").casefold()) if isinstance(provider, str) else None
    if not ids:
        return False, "unknown provider"
    from_domain = _address_domain(from_address) if isinstance(from_address, str) else ""
    if not from_domain or from_address.strip().startswith("@"):
        return False, "no from address"
    if headers is None or isinstance(headers, (str, bytes)) or not isinstance(headers, (list, tuple)):
        return False, "no header" if headers is None else "not a list"
    if not headers:
        return False, "no header"
    if len(headers) > MAX_HEADERS:
        return False, "too many headers"
    mine = []
    for header in headers:
        authserv = _authserv_id(header)
        if authserv is None:
            # Junk or untokenizable: it may stand where the provider's own result was, so no proof.
            return False, "unparseable"
        if authserv in ids:
            mine.append(header)
    if not mine:
        return False, "not checked by your mail provider"
    if len(mine) > 1:
        return False, "more than one result claims to be your provider's"
    try:
        _, results = parse(mine[0])
    except _Unparseable:
        return False, "unparseable"

    dmarc_pass = False
    for method, result, props in results:
        if method != "dmarc":
            continue
        if result == "fail":
            return False, "dmarc fail"
        if result == "pass":
            domains = [_domain(v) for v in props.get("header.from", [])]
            if not domains:
                continue             # a pass that does not say for which domain proves nothing
            if not all(aligned(d, from_domain) for d in domains):
                return False, "dmarc for another domain"
            dmarc_pass = True
        elif result not in ("none", "temperror", "permerror"):
            return False, "dmarc unknown result"
    if dmarc_pass:
        return True, "dmarc pass"
    for method, result, props in results:
        if method == "dkim" and result == "pass":
            signer = props.get("header.d")
            domain = _domain(signer[0]) if signer and len(signer) == 1 else ""
            if not signer:
                ident = props.get("header.i")
                domain = _address_domain(ident[0]) if ident and len(ident) == 1 else ""
            if domain and aligned(domain, from_domain):
                return True, "dkim pass aligned"
    for method, result, props in results:
        if method == "spf" and result == "pass":
            mailfrom = props.get("smtp.mailfrom")
            if mailfrom and len(mailfrom) == 1:
                value = mailfrom[0]
                domain = _address_domain(value) if "@" in value else _domain(value)
                if domain and aligned(domain, from_domain):
                    return True, "spf pass aligned"
    return False, "no aligned pass"
