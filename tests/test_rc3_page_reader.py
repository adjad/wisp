"""Source contracts for Smart Search's page reader (PageReader.swift).

Pure text checks on the Swift source: what they pin needs a live window, the
Accessibility API or a failing Vision request to exercise, which no check here
may use. No compiler, no app, no screen.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "Sources" / "WispApp" / "PageReader.swift"


def _reader() -> str:
    text = SOURCE.read_text()
    # PageReader only: ScreenSnapshot shares the file and is not read here.
    return text[text.index("enum PageReader {"):text.index("enum ScreenSnapshot {")]


def _code(text: str) -> str:
    """`text` without its // comments, so a comment can't satisfy a check."""
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`, comments removed."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return _code(text[open_at:i + 1])
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_the_screen_fallback_resumes_its_continuation_once():
    """A failed Vision request reports to a completion handler and also throws
    from perform. Resuming from both crashes the app, so there is one resume
    and the request has no handler."""
    reader = _code(_reader())
    assert len(re.findall(r"\.resume\(", reader)) == 1
    assert len(re.findall(r"\.resume\(", _body(_reader(), "static func ocrFrontWindow(pid: pid_t) async"))) == 1
    assert "VNRecognizeTextRequest()" in reader
    assert not re.search(r"VNRecognizeTextRequest\s*(\{|\(\s*completionHandler)", reader)
    # What the recognition keeps: accurate level, language correction, reading order.
    recognize = _body(_reader(), "static func recognizeText(in image: CGImage)")
    assert "request.recognitionLevel = .accurate" in recognize
    assert "request.usesLanguageCorrection = true" in recognize
    assert "boundingBox.midY > b.boundingBox.midY" in recognize
