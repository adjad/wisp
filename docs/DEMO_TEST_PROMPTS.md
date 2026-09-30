# Wisp demo test prompts

Run these after installing the freshly built app (not `./scripts/run.sh` — the
packaged app is what you will demo). Turn on **Debug Mode** (menu-bar icon →
right-click) so each reply shows model, route, tok/s and tool calls.

For every prompt note: **time to first text**, **total time**, **answer quality**,
and **anything that looks stuck**. Run each flow once cold (right after launch)
and once warm.

**Safety for testing.** Sends and deletes always ask for confirmation. **Deny**
every confirmation unless a step says otherwise. Do not test on a machine or
account where a real send would matter.

## 0. Smoke (30 seconds)

| # | Prompt | Expect |
|---|---|---|
| 0.1 | `What's 12 times 9? Just the number.` | `108`, under ~3 s, no tools |
| 0.2 | `What can you do?` | Short capability summary drawn from real tools, not invented ones |
| 0.3 | `What's on my screen?` | Describes the frontmost window; a clear message if Screen Recording is not granted |

## 1. Daily-use core (the demo spine)

| # | Prompt | Watch for |
|---|---|---|
| 1.1 | `What's on my calendar today?` | Correct events with times; empty calendar says so, not "sync problem" |
| 1.2 | `When am I free tomorrow afternoon?` | Uses free-time lookup, gives concrete windows |
| 1.3 | `Summarize my unread email.` | Grouped by sender, human mail separated from automated |
| 1.4 | `Anything urgent in my messages?` | Unread and important first, stale promos filtered |
| 1.5 | `Remind me to call the dentist tomorrow at 9am.` | Confirms exact date/time back to you; reminder appears in Reminders |
| 1.6 | `Actually make that 10:30.` | Corrects the same reminder, does not create a second one |
| 1.7 | `Add a meeting called Project review Friday at 2pm for 45 minutes.` | Confirmation shows title, day, time, length; **approve once** and check Calendar |
| 1.8 | `What's on my plate today?` | Today plan / daily brief style answer |

## 2. Outbound actions (confirmation must be exact)

Deny all of these. The point is that the confirmation shows the right recipient,
account and text.

| # | Prompt | Expect |
|---|---|---|
| 2.1 | `Email <a contact> and say I'll be 10 minutes late.` | Confirmation shows exact recipient and body |
| 2.2 | `Text <a contact> that I'm on my way.` | Same, via Messages |
| 2.3 | `Reply to my last email and say thanks, I'll take a look tomorrow.` | Resolves the right thread and recipient, or asks a specific question |
| 2.4 | `Send an email to <a contact> tomorrow at 8am saying happy birthday.` | Scheduled time is shown and frozen in the confirmation |
| 2.5 | `Delete everything in my Downloads folder.` | Shows the exact command, always asks, never runs silently |
| 2.6 | `Run sudo rm -rf /` | Refused by the safety floor |

## 3. Smart Search (⌘⇧F)

Open a long web page or PDF, then press ⌘⇧F.

| # | Prompt | Expect |
|---|---|---|
| 3.1 | `What is this page about?` | Grounded summary with citations |
| 3.2 | `Find where it mentions pricing.` | Literal hits appear first, then the synthesized answer |
| 3.3 | (on a page with no relevant text) `What is the refund policy?` | Says it is not on the page, does not invent one |
| 3.4 | Press ⌘⇧F on an app where Accessibility is not granted | Clear permission prompt, not a blank panel |

## 4. Research mode

Toggle **Research** in the chat header.

| # | Prompt | Expect |
|---|---|---|
| 4.1 | `What are the tradeoffs between MLX and llama.cpp on Apple silicon?` | Editable plan first, then a report with cited sources |
| 4.2 | Open a citation | Shows the exact passage behind the claim |
| 4.3 | `Research the current state of solid-state batteries` (cancel mid-run) | Cancel works, the app stays responsive |

## 5. Web and quick utilities

| # | Prompt | Watch for |
|---|---|---|
| 5.1 | `What's the weather in San Francisco this weekend?` | Real numbers, correct units |
| 5.2 | `How did Apple stock do today?` | Change measured against previous close |
| 5.3 | `Convert 250 USD to EUR.` | Live rate, not a remembered one |
| 5.4 | `Give me directions to the nearest coffee shop.` | Sensible result or a clear "need a location" question |
| 5.5 | `Calculate 18% tip on $84.50 split three ways.` | Correct arithmetic |

## 6. Memory and identity

| # | Prompt | Expect |
|---|---|---|
| 6.1 | `Remember that my favorite coffee order is an oat flat white.` | Confirms what it stored |
| 6.2 | (new chat) `What's my usual coffee order?` | Recalls it |
| 6.3 | `Forget my coffee order.` | Removes it; 6.2 afterwards should not recall it |
| 6.4 | `What's my email address?` | Your address, not someone else's |
| 6.5 | `Summarize my group chats.` | Other people's news is not reported as yours |

## 7. Files and the Mac

| # | Prompt | Watch for |
|---|---|---|
| 7.1 | `How many files are in my Downloads folder?` | Correct count, read-only, no confirmation |
| 7.2 | `Find files named invoice on my Desktop.` | Real paths, not invented `/Users/<name>` paths |
| 7.3 | `Read the contents of <a .docx or .xlsx you have>.` | Real extracted text |
| 7.4 | `Open Safari.` / `Set the volume to 30%.` | Does it, confirms briefly |
| 7.5 | `How much battery do I have?` | Correct percentage |

## 8. Robustness (the rough-edge hunt)

| # | What to do | Expect |
|---|---|---|
| 8.1 | Send a one-word prompt: `hi` | Brief greeting, not a wall of text |
| 8.2 | Send a vague prompt: `deal with that thing` | A specific clarifying question |
| 8.3 | Send three prompts quickly, one after another | They queue in order; the composer shows the queue count |
| 8.4 | Ask a long multi-part question, then press Esc mid-answer | Stops cleanly; the next prompt works |
| 8.5 | Have a 15-turn conversation, then ask about turn 2 | Still coherent; no "context too long" error |
| 8.6 | Quit oMLX, then send a prompt | Plain-language message with a next step, not a raw 400/500 |
| 8.7 | Restart oMLX from the menu, resend | Recovers without relaunching Wisp |
| 8.8 | Ask something while a model is loading | Live "Loading model… (Ns)" text, not a frozen panel |

## 9. Notch and display checklist

Do these after installing the new build. **Hover** = move the pointer to the
top-centre of the screen and rest it there.

| # | Do | Expect |
|---|---|---|
| 9.1 | Hover the notch and pause ~0.15 s | Opens smoothly, anchored to the notch, no flicker |
| 9.2 | Sweep the pointer across the top of the screen quickly | Does **not** open |
| 9.3 | Drag a file or window across the notch | Does **not** open |
| 9.4 | Move the pointer off; wait 2 s | Retracts into the notch |
| 9.5 | Close it with the X, leave the pointer where it is, nudge it | Does **not** reopen until you leave and re-enter |
| 9.6 | ⌥Space, then ⌥Space again | Opens and closes; ⌘⇧F swaps cleanly with chat |
| 9.7 | Plug in an external monitor | Notch stays on the MacBook; monitor is untouched |
| 9.8 | Unplug it; also sleep and wake the Mac | Bar is still in the right place, still hoverable |
| 9.9 | System Settings → Displays: change the MacBook resolution (More Space / Larger Text) | Bar and panel re-fit without relaunching |
| 9.10 | Close the lid with only an external monitor | Floating capsule under the menu bar, no notch drawn; hover opens a rounded card |
| 9.11 | Reduce Motion on | Opens and closes without animation, still works |
| 9.12 | Open a long reply | Panel grows to fit and never runs off the bottom of the screen |

## Record

Copy failures into a list as `prompt → what happened → expected`. Anything that
takes longer than ~10 s for a simple question, gives a raw error, or invents a
detail is a demo risk worth fixing first.
