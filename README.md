# Wisp

**A private assistant that lives in your Mac's notch, keeps track of what needs you, and does the
busywork. Its AI runs on your Mac, so your mail, texts and calendar never go to a cloud.**

[Download Wisp](https://github.com/adjad/wisp/releases) · [What's new in 0.5.0-dev4](docs/releases/0.5.0-dev4.md) · Apple silicon, macOS 26.2+

## The problem

Your day is spread across Mail, Messages, Calendar, Reminders and Notes, and none of them talk to each other. Each app holds one piece of the picture, and you are the only thing connecting them. The plan someone made in a group chat at 9pm only becomes real if you remember to put it on your calendar. The form the school needs signed by Friday is in an email you opened once on your phone. You jotted the contractor’s quote in Notes, and the follow-up you promised sits in a thread you haven’t opened since. Your grocery list is in Reminders, but the dinner it was for moved to Thursday in a message you read on the train.

Nothing here is hard. The trouble is that it’s scattered. A reply you owe, a time that changed, a payment that’s due and a promise you made all look the same: one line in an app you may not open today. Each app only knows about itself, so nothing can tell you that the dentist appointment now collides with the pickup someone asked you about, or that the person waiting on a yes has been waiting for four days. Keeping all of that straight is unpaid work, and when it fails, things slip. You miss a reply, a return window or a renewal, and you feel like you weren’t paying attention. You were paying attention. There were just five places to look.

The assistants that could help either can’t see that information or want it uploaded. Siri can set a timer, but it can’t tell you who’s waiting on you. Cloud assistants can reason across your life, but only after your inbox and your texts have been sent to someone else’s servers.

## Why privacy is the whole point

Your mail, your messages, your calendar and your notes are among the most private things you have. They hold your family’s group chat, your health appointments, your finances, your work, and the things you told one person and no one else. Even when a cloud provider promises not to keep any of it, you are still trusting a company, a retention policy and a security team you will never see. An assistant that has to read everything to be useful is only worth using if that reading never leaves your hands.

That’s why Wisp runs entirely on your Mac. The models run locally, the indexes and caches stay on your disk, and nothing is uploaded, so no account, server or third party is involved. Because it’s local, it can safely be given what a cloud assistant never should be: the full text of your mail, your whole message history and every note.

It also works with the Apple apps you already use rather than asking you to move into a new one. It reads from Mail, Messages, Calendar, Reminders and Notes through the system’s own interfaces and respects the permissions macOS already gives you. When it acts, for example by adding an event, creating a reminder or drafting a reply, the result lands in the same apps, where it syncs through iCloud to your iPhone and iPad like anything else you made yourself. Nothing is stored in a silo that only Wisp can open.

## What Wisp does

Wisp reads those apps on your Mac, works out what needs you, and gets it ready, using an AI model
that runs on the Mac itself.

- **Brief: what needs you today.** Open the notch and see who's waiting on a reply, with the reply
  already drafted. It also shows deadlines and bills found in your mail and today's to-dos from
  every source. Send the draft, add the reminder or dismiss it, right from the card.
- **Day: the rest of today, hour by hour.** Meetings, reminders and the open time in between, in
  one timeline.
- **Ask: type or say anything.** Try "Any texts I need to answer?", "Move my 3pm to tomorrow",
  "Remind me at 5 to call Mom" or "What makes the sky blue?". Wisp looks through your mail,
  messages, calendar and notes, searches the web when it needs to, and can act on your Mac. It drafts
  and sends mail and texts, adds events and reminders, opens apps and changes settings like Do
  Not Disturb.
- **Found in your mail.** While you're away from the Mac, Wisp works through the last 90 days of your
  mail and finds dates you might forget: a renewal, a bill, an appointment, a promise you made in
  an email. Only the important ones reach your Brief.
- **Smart Search (⌘⇧F).** Ask a question about the document on your screen and get the answer,
  with the passage it came from.
- **Research.** For bigger questions, Wisp searches the web in several rounds and writes a report
  that cites the exact passage behind every claim.

It gets to know you, too. Tell it your name, your goals and how you like to be talked to (gentle
by default), and your Brief and answers fit you.

## Private by design

- **The AI runs on your Mac.** Wisp downloads its model once (Gemma 4 E4B, about 5.5 GB) and runs it
  with an engine built into the app. Your mail, calendar, messages and notes are read on the Mac
  and stored there, in `~/.moe`.
- **You decide how much it does on its own.** Wisp starts with **Full access** on, so everyday
  actions such as adding a reminder, writing a file or opening an app run without asking. Some things
  always wait for your approval, whatever the setting: sending mail or messages, calendar changes,
  AppleScript, installing a tool, permanent deletes, and shell commands that could send data off
  your Mac. Turn Full access off in **Settings › Privacy & Access**, the notch's quick settings or
  the chat window, and risky actions pause for your confirmation. For now, Full access turns back on
  when Wisp restarts. Every action Wisp takes is logged in `~/.moe/audit.jsonl`.
- **You can start over.** `/resetwisp` deletes everything Wisp has stored and revokes the access
  you gave it.

## Get started

### What you need

| | |
| --- | --- |
| Mac | Apple silicon (M1 or later) |
| macOS | 26.2 or newer for Wisp's built-in engine. On macOS 14–26.1 you need another engine, such as oMLX, Ollama or LM Studio ([details](docs/technical-overview.md#using-another-local-inference-app)). |
| Memory | 16 GB minimum, 24 GB recommended |
| Disk | About 8 GB free for the first download |

### Install

Wisp isn't notarized by Apple yet, so macOS blocks the first open. You only allow it once:

1. Download the ZIP from [Releases](https://github.com/adjad/wisp/releases), unzip it, and drag
   **Wisp.app** into **Applications**.
2. Open Wisp. When macOS says it can't verify the app, click **Done**.
3. Open **System Settings ▸ Privacy & Security**, scroll to the bottom, and click **Open Anyway**
   next to the message about Wisp. Confirm with your password or Touch ID.

On macOS 14 and earlier, Control-click **Wisp.app** and choose **Open** instead. If macOS still
refuses, run `xattr -dr com.apple.quarantine /Applications/Wisp.app` in Terminal. Updating from an
earlier build? Quit the old Wisp first.

### First run

A single setup window walks you through five steps, and you can skip any of them and come back from
the menu-bar icon (**Finish Setup...**):

1. **The model.** Wisp downloads its model while you carry on. That takes about 10 minutes on a
   fast connection. If you already run oMLX, Wisp uses it instead and spends up to 30 seconds
   looking for it.
2. **Permissions.** Each is optional, and macOS asks only when you click **Allow** on that row:

   | Permission | What it's for |
   | --- | --- |
   | Calendar, Reminders | Planning your day; adding or moving events and reminders when you ask |
   | Mail and Notes | Reading them while those apps are open (Wisp opens them briefly to ask, then closes them) |
   | Full Disk Access | Reading Messages, and Mail and Notes while those apps are closed |
   | Messages | Sending the texts you approve |
   | Contacts, Notifications | Names instead of numbers; reminder alerts |

3. **Google** (optional). Gmail and Google Calendar in your Brief and Day, plus Drive and Docs in Ask.
4. **About you** (optional). What to call you, your goals, and how you like replies.
5. **A short tour.** Your first Brief starts when you finish.

### Run Wisp

Open **Wisp** from Applications or Spotlight. It starts its own engine and model, and then lives
in the notch and the menu bar, so there's nothing else to launch. Closing its windows doesn't quit
it; to stop it, choose **Quit Wisp** from the menu-bar icon. After you quit, open it the same way
to start it again. The first start after a restart takes a little longer while the model loads.

## Everyday use

- **Open it.** Hover over the notch. On a Mac without a notch, click the menu-bar icon or push the
  pointer against the top-centre of the screen.
- **Shortcuts.** **⌥Space** opens Ask, **⌥⇧Space** asks by voice, **⌘⇧F** starts Smart Search and **⌘,**
  opens Settings.
- **Slash commands.** Type `/` in Ask to see them, including `/today`, `/about` (tell Wisp about
  you), `/report` (report a problem) and `/resetwisp`.
- **Voice and accessibility.** Answers to spoken questions are read aloud. Wisp follows Reduce
  Motion, Increase Contrast and VoiceOver, and **Settings ▸ Voice & accessibility** sets the text
  size.

### Connect Google (optional)

Open **Settings ▸ Google** and click **Connect Google account**. Access is read-only: Wisp can't
send, edit or delete anything in Google. For now:
- your account has to be on the developer's tester list;
- Google shows an "unverified app" warning (choose **Continue**);
- the sign-in expires every 7 days, and Wisp tells you when it does.

**Disconnect & erase** removes the account and everything Wisp read from it. Building without the
built-in sign-in? See [docs/google-cloud-setup.md](docs/google-cloud-setup.md).

### Quit, uninstall and reset

- **Quit.** Use the menu-bar icon and choose **Quit Wisp**. Quitting also frees the model's memory.
- **Start over.** Type `/resetwisp` in full in the notch and confirm. Wisp disconnects Google,
  deletes its memories, chats, caches and settings, resets its permissions, and reopens to setup.
  The downloaded model stays, and nothing in your own Mail, Calendar, Notes, Reminders or Messages
  is touched.
- **Uninstall.**
  1. Disconnect Google first (**Settings ▸ Google ▸ Disconnect & erase**).
  2. Quit Wisp, then drag **Wisp.app** to the Bin.
  3. To remove everything, also delete `~/.moe` (it holds the 5.5 GB model) and run
     `defaults delete com.wisp.assistant` and `tccutil reset All com.wisp.assistant`.

### Report a problem

Choose **Report a Problem...** from the menu-bar icon (or type `/report`). Wisp builds a report you
save yourself; nothing is uploaded. Attach it to an
[issue](https://github.com/adjad/wisp/issues). Logs are in `$TMPDIR/moe-backend.log` and
`~/.moe/engine/engine.log`.

## For developers

Wisp is a Swift menu-bar app, a bundled Python (FastAPI) agent service, and a private copy of the
oMLX model engine.

### Run Wisp from source

You need Apple silicon, macOS 14 or newer, Python 3.9+ and Apple's Command Line Tools
(`xcode-select --install`).

1. **Set up Python** (once):

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-dev.txt
   ```

2. **Start a model server.** Run [oMLX](https://github.com/jundot/omlx) with a model loaded; its
   menu-bar app serves on `127.0.0.1:8000`. Gemma 4 E4B (`gemma-4-E4B-it-oQ4e-mtp`) is the default.
3. **Run the backend** with auto-reload on `:8765`:

   ```bash
   ./scripts/run.sh
   ```

   Try it from another terminal:

   ```bash
   curl -sN localhost:8765/agent -H 'Content-Type: application/json' -d '{"prompt":"what is on my calendar today?"}'
   ```

4. **Build and open the app.** This writes a ZIP to `dist/<version>-<build>-<commit>-preview/`.
   Unzip it and open `Wisp.app`.

   ```bash
   ./scripts/wisp-build preview
   ```

   The app starts and manages its own backend on `:8765`, so stop `scripts/run.sh` first. Only one
   Wisp can own that port.

A release candidate is `./scripts/wisp-build all --strict-toolchain --output dist/candidate`. It
runs the full test gate and Simulation QA as well; see [docs/build-release.md](docs/build-release.md).

- [docs/technical-overview.md](docs/technical-overview.md): how it works (tools, assistant layer,
  memory, safety, models, layout, state on disk).
- [docs/WISP_OVERVIEW.md](docs/WISP_OVERVIEW.md): the long-form system reference.
- [docs/README.md](docs/README.md): every other document.
- [CHANGELOG.md](CHANGELOG.md): what shipped.
