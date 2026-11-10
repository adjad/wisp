"""Source contracts for the research elapsed-time chip (ResearchModel.swift).

Pure text checks on the Swift source. tests/ResearchLibraryChecks.swift runs
the clock itself, which needs the Swift compiler; these pin how the model uses
it where no compiler runs. No app, no server.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "app" / "Sources" / "WispApp" / "ResearchModel.swift"


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


def test_the_chip_does_not_count_from_when_the_plan_was_drafted():
    """The clock used to start at the job's creation time, so a plan read for
    eight minutes opened its research at "8m 0s"."""
    model = MODEL.read_text()
    code = _code(model)
    assert "startedAt" not in code and "createdAt" not in code
    apply = _body(model, "private func apply(_ snapshot: WispClient.ResearchSnapshot)")
    assert "clock" not in apply and "Date(" not in apply


def test_only_running_time_is_counted():
    """Starting or resuming opens a stretch, pausing or finishing closes it and
    keeps its time, and choosing another job starts from nothing."""
    model = MODEL.read_text()
    clock = _body(model, "struct RunClock")
    start = _body(clock, "mutating func start(at now: Date)")
    assert "if runningSince == nil { runningSince = now }" in start
    stop = _body(clock, "mutating func stop(at now: Date)")
    assert "accumulated += max(0, now.timeIntervalSince(since))" in stop and "runningSince = nil" in stop
    assert "accumulated + current" in _body(clock, "func seconds(at now: Date) -> Int")

    begin = _body(model, "private func startElapsedTimer()")
    assert "clock.start(at: Date())" in begin and "clock.stop" not in begin
    # A tick that lands after a pause or a new selection writes nothing.
    assert "guard let self, self.clock.isRunning else { return }" in begin
    assert "RunClock.text(self.clock.seconds(at: Date()))" in begin
    end = _body(model, "private func stopElapsedTimer()")
    assert "clock.stop(at: Date())" in end and "elapsedTimer?.invalidate()" in end
    select = _body(model, "private func prepareSelection() -> UUID")
    assert select.index("stopElapsedTimer()") < select.index("clock = RunClock()")
    assert 'elapsedText = ""' in select

    checks = (ROOT / "tests" / "ResearchLibraryChecks.swift").read_text()
    assert "        runClockCountsOnlyRunningTime()\n" in checks
    assert "        await elapsedChipIgnoresPlanAge()\n" in checks
