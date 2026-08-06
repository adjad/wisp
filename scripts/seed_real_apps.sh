#!/usr/bin/env bash
# OPTIONAL: create a few REAL, clearly-tagged items in Calendar.app,
# Reminders.app, and Notes.app so you can test the live Swift sync pipeline
# itself (CalendarReader/RemindersWriter/NotesReader -> /assistant/sync/*),
# not just the backend logic that scripts/wisp_testdata.py already seeds
# directly into assistant.db/facts.db.
#
# Everything created here is prefixed "[Wisp QA]" so it's unmistakable and so
# scripts/clear_real_apps.sh can remove exactly these items and nothing else.
# This DOES touch your real Calendar/Reminders/Notes (reversible — run the
# clear script when you're done). Requires Wisp/Terminal to have the relevant
# TCC grants (Calendar, Reminders, Automation for Notes) the first time.
set -euo pipefail

echo "→ Calendar…"
osascript <<'APPLESCRIPT' || echo "  (Calendar step failed — is Calendar.app usable and TCC granted?)"
tell application "Calendar"
    set targetCal to missing value
    repeat with c in calendars
        if writable of c then
            if name of c is "Home" then
                set targetCal to c
                exit repeat
            end if
            if targetCal is missing value then set targetCal to c
        end if
    end repeat
    if targetCal is missing value then error "no writable calendar found"
    tell targetCal
        set d1 to (current date) + 1 * days
        set hours of d1 to 15
        set minutes of d1 to 0
        set seconds of d1 to 0
        make new event with properties {summary:"[Wisp QA] Real-Calendar Test Event", start date:d1, end date:d1 + 1 * hours, location:"Test Location"}

        set d2 to (current date) + 3 * days
        set hours of d2 to 9
        set minutes of d2 to 0
        make new event with properties {summary:"[Wisp QA] Real-Calendar All-Day Test", start date:d2, end date:d2 + 1 * days, allday event:true}
    end tell
end tell
APPLESCRIPT
echo "  ✓ created 2 test events"

echo "→ Reminders…"
osascript <<'APPLESCRIPT' || echo "  (Reminders step failed — is Reminders.app usable and TCC granted?)"
tell application "Reminders"
    set d to (current date) + 2 * days
    tell default list
        make new reminder with properties {name:"[Wisp QA] Real-Reminders Test Item", remind me date:d}
    end tell
end tell
APPLESCRIPT
echo "  ✓ created 1 test reminder"

echo "→ Notes…"
osascript <<'APPLESCRIPT' || echo "  (Notes step failed — is Notes.app usable and Automation access granted?)"
tell application "Notes"
    set acct to default account
    set targetFolder to missing value
    repeat with f in folders of acct
        if name of f is "Notes" then
            set targetFolder to f
            exit repeat
        end if
    end repeat
    if targetFolder is missing value then set targetFolder to first folder of acct
    tell targetFolder
        make new note with properties {body:"[Wisp QA] Real-Notes Test Note<br><br>This note was created by scripts/seed_real_apps.sh for manual testing. Safe to delete, or run scripts/clear_real_apps.sh."}
    end tell
end tell
APPLESCRIPT
echo "  ✓ created 1 test note"

echo ""
echo "Done. Give Wisp a few minutes to sync (or restart it), then test."
echo "When you're done: bash scripts/clear_real_apps.sh"
