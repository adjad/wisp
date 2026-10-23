"""The evaluation app's OS profile, rendered for both modes and compared.

Runs the build script's own app_profile_text function (shell builtins only)
for a SYNTHETIC home, owned root and bundle, then checks the snapshot profile
against its reviewed text and that live mode differs by exactly the approved
deltas. Nothing here applies a profile or starts the app.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_notch_evaluation.sh"
BUNDLE_ID = "com.wisp.notch-evaluation.dev"
PLACES = {"owned": "/private/tmp/wisp-notch-run-SYNTH", "image": "/SYNTH/Eval.app/Contents/MacOS/WispNotchEvaluation",
          "bundle": "/SYNTH/Eval.app", "home": "/Users/synthetic", "cache": "/private/var/folders/sy/nth/C/"}

SNAPSHOT = """(version 1)
(allow default)
(deny appleevent-send)

(deny process-exec (require-not (literal "{image}")))
(deny process-fork)
(deny mach-lookup (global-name "com.apple.coreservices.appleevents")
  (global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.lsd.mapdb")
  (global-name "com.apple.usernoted.client") (global-name "com.apple.pasteboard.1")
  (global-name "com.apple.coreservices.quarantine-resolver"))
(deny network*)
(allow network-outbound (remote ip "localhost:18891"))
(deny file-read-data (require-all (subpath "{home}")
  (require-not (require-any (subpath "{owned}") (subpath "{bundle}") ))))
(deny file-write* (require-not (require-any (subpath "{owned}") (literal "/dev/null")
  (subpath "{home}/Library/Caches/com.wisp.notch-evaluation.dev")
  (subpath "{home}/Library/Saved Application State/com.wisp.notch-evaluation.dev.savedState")
  (subpath "{cache}com.wisp.notch-evaluation.dev") )))
"""


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, name
    return match.group(0)


def render(live: bool) -> str:
    program = (f'LISTEN_PORT=18891\nBUNDLE_ID="{BUNDLE_ID}"\n{_function("app_profile_text")}\n'
               'app_profile_text "$1" "$2" "$3" "$4" "$5" "$6"\n')
    args = [PLACES["owned"], PLACES["image"], PLACES["bundle"], "1" if live else "", PLACES["home"], PLACES["cache"]]
    return subprocess.run(["/bin/bash", "-c", program, "profile", *args], check=True,
                          capture_output=True, text=True).stdout


def test_snapshot_profile_is_the_reviewed_text():
    assert render(live=False) == SNAPSHOT.format(**PLACES)


def test_live_profile_differs_by_exactly_the_approved_deltas():
    snapshot, live = render(live=False), render(live=True)
    home = PLACES["home"]
    events = re.findall(r'appleevent-destination "([^"]+)"', live)
    assert events == ["com.apple.mail", "com.apple.Notes"] and "appleevent-destination" not in snapshot
    assert live.count("(allow appleevent-send") == 1 and "(deny appleevent-send)" in live
    prefs = re.findall(r'\(literal "([^"]*Preferences[^"]*)"\)', live)
    assert prefs == [f"{home}/Library/Preferences/{BUNDLE_ID}.plist"]
    reads = [f"{home}/Library/Mail", f"{home}/Library/Messages", f"{home}/Library/Group Containers/group.com.apple.notes"]
    read_rule = live.split("(deny file-read-data", 1)[1].split("(deny file-write*", 1)[0]
    write_rule = live.split("(deny file-write*", 1)[1]
    for path in reads:
        assert f'(subpath "{path}")' in read_rule and path not in write_rule      # readable, never writable
    # Removing exactly those additions (and the three lookups) gives back the snapshot.
    expected = live.replace(
        '(allow appleevent-send (appleevent-destination "com.apple.mail") (appleevent-destination "com.apple.Notes"))', "")
    for path in reads:
        expected = expected.replace(f'(subpath "{path}")', "", 1)
    expected = re.sub(r"\(subpath \"/SYNTH/Eval.app\"\)  \n    ", '(subpath "/SYNTH/Eval.app") ', expected)
    expected = expected.replace(f' (literal "{prefs[0]}")', " ")
    expected = expected.replace(
        "(deny mach-lookup (global-name",
        '(deny mach-lookup (global-name "com.apple.coreservices.appleevents")\n'
        '  (global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.lsd.mapdb")\n'
        '  (global-name', 1)
    assert expected == snapshot
    for lookup in ("com.apple.usernoted.client", "com.apple.pasteboard.1", "com.apple.coreservices.quarantine-resolver"):
        assert lookup in live
