#!/usr/bin/env bash
# Remove exactly what scripts/seed_real_apps.sh created — anything in
# Calendar/Reminders/Notes whose title starts with "[Wisp QA]". Safe to run
# even if seed_real_apps.sh was never run (just deletes nothing, reports 0).
set -euo pipefail

echo "→ Calendar…"
osascript <<'APPLESCRIPT' || echo "  (Calendar cleanup failed)"
tell application "Calendar"
    set n to 0
    repeat with c in calendars
        if writable of c then
            repeat with e in (every event of c whose summary starts with "[Wisp QA]")
                delete e
                set n to n + 1
            end repeat
        end if
    end repeat
    return n
end tell
APPLESCRIPT

echo "→ Reminders…"
osascript <<'APPLESCRIPT' || echo "  (Reminders cleanup failed)"
tell application "Reminders"
    set n to 0
    repeat with l in lists
        repeat with r in (every reminder of l whose name starts with "[Wisp QA]")
            delete r
            set n to n + 1
        end repeat
    end repeat
    return n
end tell
APPLESCRIPT

echo "→ Notes…"
osascript <<'APPLESCRIPT' || echo "  (Notes cleanup failed)"
tell application "Notes"
    set n to 0
    repeat with acct in accounts
        repeat with f in folders of acct
            repeat with nt in (every note of f whose name starts with "[Wisp QA]")
                delete nt
                set n to n + 1
            end repeat
        end repeat
    end repeat
    return n
end tell
APPLESCRIPT

echo ""
echo "✓ cleanup done."
