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


def _number(text: str, pattern: str) -> float:
    match = re.search(pattern, text)
    assert match, f"{pattern!r} not found; update this contract with the code it reads"
    return float(match.group(1).replace("_", ""))


def test_the_window_is_read_off_the_main_actor():
    """Every Accessibility call is a round trip to the other app, and the file
    read is disk work. Neither may run on the main actor, where it would hold
    the notch, chat and menu bar until the other app answers."""
    whole = SOURCE.read_text()
    before = whole[:whole.index("enum PageReader {")].rstrip().splitlines()[-1]
    assert "@MainActor" not in before, "PageReader as a whole is not on the main actor"
    reader = _reader()
    # Only these touch the app's own state; each says so itself.
    for signature in ("static var hasAccessibility: Bool", "static func requestAccessibility()",
                      "static func read() async -> PageText",
                      "static func reveal(_ page: PageText, offset: Int, length: Int) -> Bool"):
        lead = reader[:reader.index(signature)].rstrip().splitlines()[-2:]
        assert any(line.strip() == "@MainActor" for line in lead), signature
    assert len(re.findall(r"^\s*@MainActor\s*$", reader, re.M)) == 4
    read = _body(reader, "static func read() async -> PageText")
    assert re.search(r"await Task\.detached\(priority: \.userInitiated\) \{\s*"
                     r"PageReader\.readWindow\(pid: pid, appName: appName, trusted: trusted\)\s*\}\.value", read)
    for blocking in ("AXUIElement", "focusedWindow(", "stringValue(", "documentURL(", "readFile(", "harvest("):
        assert blocking not in read, f"read() itself must not call {blocking}"
    # The order of the three sources is unchanged: file, then tree, then screen.
    window = _body(reader, "static func readWindow(pid: pid_t, appName: String, trusted: Bool)")
    assert window.index("readFile(at: url)") < window.index("harvest(window, into: &collected, depth: 0)")
    assert read.index("readWindow(") < read.index("ocrFrontWindow(pid: pid)")


def test_no_accessibility_call_waits_for_the_system_default():
    """An app that isn't answering costs a short timeout per call, not several
    seconds. The timeout belongs to the element it is set on, so the app, the
    window and every visited element get it before they are asked."""
    reader = _reader()
    assert 0 < _number(reader, r"static let messagingTimeout: Float = ([\d._]+)") <= 1
    window = _body(reader, "static func readWindow(pid: pid_t, appName: String, trusted: Bool)")
    assert window.index("AXUIElementSetMessagingTimeout(axApp, messagingTimeout)") \
        < window.index("AXUIElementSetAttributeValue(axApp")
    assert window.index("AXUIElementSetMessagingTimeout(window, messagingTimeout)") \
        < window.index("stringValue(window")
    harvest = _body(reader, "static func harvest(_ el: AXUIElement, into out: inout Collected, depth: Int)")
    assert harvest.index("AXUIElementSetMessagingTimeout(el, messagingTimeout)") < harvest.index("stringValue(el")
    # Revealing a result is not a read: it goes back to the default first.
    reveal = _body(reader, "static func reveal(_ page: PageText, offset: Int, length: Int) -> Bool")
    assert reveal.index("AXUIElementSetMessagingTimeout(anchor.element, 0)") \
        < reveal.index("AXUIElementSetAttributeValue(")


def test_the_tree_walk_has_a_time_budget_and_counts_as_it_goes():
    """The walk stops at its deadline with what it has, and it never counts the
    collected text again: String.count walks the whole string, which made a
    long page quadratic."""
    reader = _reader()
    assert 0 < _number(reader, r"static let harvestBudgetNanoseconds: UInt64 = ([\d_]+)") <= 3_000_000_000
    window = _body(reader, "static func readWindow(pid: pid_t, appName: String, trusted: Bool)")
    assert re.search(r"Collected\(\s*deadline: DispatchTime\.now\(\)\.uptimeNanoseconds \+ harvestBudgetNanoseconds\)",
                     window)
    harvest = _body(reader, "static func harvest(_ el: AXUIElement, into out: inout Collected, depth: Int)")
    guard = harvest[:harvest.index("out.visited += 1")]
    assert "out.scalars < maxChars" in guard and "DispatchTime.now().uptimeNanoseconds < out.deadline" in guard
    append = _body(reader, "static func append(_ s: String, _ el: AXUIElement, to out: inout Collected)")
    assert "text.count" not in harvest and "text.count" not in append
    assert "collected.text.count" not in window and "collected.scalars > 40" in window
    # Anchors come from the running total.
    assert "let start = out.scalars" in append and "start..<(start + length)" in append
    assert "out.scalars += length + SearchOffset.length(gap)" in append


def test_anchors_and_results_count_the_same_units():
    """A search result's offset is a Python string index: it counts Unicode
    scalars. Swift's `count` counts whole characters, so a flag, a family emoji
    or a combining accent earlier in the page moved every later result onto a
    neighbouring element. The reader counts scalars, and converts to UTF-16
    units only for the selection it asks the other app to make."""
    reader = _reader()
    append = _body(reader, "static func append(_ s: String, _ el: AXUIElement, to out: inout Collected)")
    assert "let length = SearchOffset.length(trimmed)" in append
    assert "trimmed.count" not in append and "gap.count" not in append
    # Each anchor keeps its element's text, which the conversion needs.
    assert "out.anchors.append((start..<(start + length), trimmed, el))" in append
    whole = SOURCE.read_text()
    assert whole.count("[(range: Range<Int>, text: String, element: AXUIElement)]") == 2
    reveal = _body(reader, "static func reveal(_ page: PageText, offset: Int, length: Int) -> Bool")
    assert "page.anchors.first(where: { $0.range.contains(offset) })" in reveal
    assert re.search(r"SearchOffset\.utf16Range\(in: anchor\.text, offset: offset - anchor\.range\.lowerBound,\s*"
                     r"length: length\)", reveal)
    assert "CFRange(location: span.location, length: span.length)" in reveal
    assert "CFRange(location: local" not in reveal

    # The two units, in the Swift that defines them.
    model = (ROOT / "app" / "Sources" / "WispApp" / "SearchModel.swift").read_text()
    offsets = model[model.index("enum SearchOffset {"):]
    assert "static func length(_ text: String) -> Int { text.unicodeScalars.count }" in offsets
    convert = _body(offsets, "static func utf16Range(in text: String, offset: Int, length: Int)")
    assert "for scalar in text.unicodeScalars" in convert and "scalar.value > 0xFFFF ? 2 : 1" in convert
    # tests/SearchModelChecks.swift runs them against these numbers, which are
    # Python's own for an element with a family emoji, a flag and "e" + U+0301
    # that follows a plain one.
    first = "Guest list"
    second = "\U0001F468\u200D\U0001F469\u200D\U0001F467 from \U0001F1E8\U0001F1E6 at the cafe\u0301 by noon"
    text = first + "\n" + second + "\n"
    assert (len(first), len(second), len(text), text.index("noon")) == (10, 34, 46, 41)
    units = len(second[:second.index("noon")].encode("utf-16-le")) // 2
    assert units == 35
    checks = (ROOT / "tests" / "SearchModelChecks.swift").read_text()
    assert "ranges == [0..<10, 11..<45] && SearchOffset.length(text) == 46" in checks
    assert "noon.location == 35 && noon.length == 4" in checks
    assert "        offsetsCountWhatTheBackendCounts()\n" in checks


def test_a_file_behind_the_window_is_bounded_before_it_is_read():
    """A plain file is sized before it is loaded, and a PDF is read page by
    page up to a page and a size limit, never as one string of any length."""
    reader = _reader()
    assert _number(reader, r"static let maxFileBytes = (\d+) \* 1024 \* 1024") <= 16
    assert _number(reader, r"static let maxPDFPages = ([\d_]+)") <= 5_000
    read_file = _body(reader, "static func readFile(at url: URL) -> String?")
    assert "doc.string" not in read_file
    assert "0..<min(doc.pageCount, maxPDFPages)" in read_file
    assert "text.utf8.count < maxFileBytes" in read_file
    assert read_file.index("size <= maxFileBytes") < read_file.index("String(contentsOf: url")
    assert "resourceValues(forKeys: [.fileSizeKey])" in read_file
