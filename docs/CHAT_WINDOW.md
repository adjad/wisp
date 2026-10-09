# Wisp Chat window

A real window for conversations that outgrow the notch panel. The notch stays
the quick ask; both surfaces talk to the same service and the same saved chats.

## What it is

- Sidebar: New chat (⌘N), search over chat titles (⌘K), shortcuts to Today,
  Research, Memory and Search everything (these open the existing windows), and
  every saved chat grouped Today / Yesterday / Previous 7 days / Earlier.
- Thread: your messages, then Wisp's. Tool calls are listed above the answer
  (collapsed once finished). Answers render through `MarkdownView`.
- Approvals: the same confirmation card as the notch (Deny / Always allow /
  Allow once), and the same editable message draft (Discard / Edit / Send).
  Nothing sends without the Send button.
- Details (⌘I): route, model, routed-by, speed, time, why that route, tools used.
- Composer: Return sends, Option-Return adds a line, paperclip attaches an image,
  the pill shows and toggles "Ask before acting" / "Full access" (the same
  service setting as Settings; sending and bulk deletes always ask).
- Hand-off: the notch header's "Open in Wisp Chat" opens this window on the
  notch's current conversation (by service session id).
- Appearance: follows the Mac. Dark is the aurora; light is a pale daylight blue.

## How it fits together

| Piece | File |
| --- | --- |
| Pure types, event reducer, history mapping, titles, grouping | `app/Sources/WispApp/Chat/ChatTypes.swift` |
| Store: chats, selection, sending, streaming, approvals, drafts | `app/Sources/WispApp/Chat/ChatStore.swift` |
| Service adapter (`/chats`, `/sessions/{id}`, `/agent`, approvals) | `app/Sources/WispApp/Chat/LiveChatBackend.swift` |
| Views | `app/Sources/WispApp/Chat/ChatViews.swift` |
| Colors, orb, aurora | `app/Sources/WispApp/Chat/ChatTheme.swift` |
| Window | `app/Sources/WispApp/Chat/ChatWindow.swift` |
| Visual QA harness (`WISP_CHAT_QA` only) | `app/Sources/WispApp/Chat/ChatQA.swift` |
| Chat list endpoint | `service/memory/store.py` `list_chats`, `service/main.py` `GET /chats` |

Chats are not stored in the app. The service already persists every
conversation in `~/.moe/sessions.db`; the window lists them with `GET /chats`,
loads one with `GET /sessions/{id}`, deletes with `DELETE /sessions/{id}`, and
continues one by passing its `session_id` to `/agent`. Closing the window loses
nothing.

`MarkdownView` now reads its colors from `\.markdownPalette`; the default is the
notch's monochrome look, so the notch is unchanged.

## Known limits

- A reloaded chat shows which tools an answer used but not their results or
  timing; the service keeps only the tool names. Live answers show everything.
- There is no per-message model picker: the router chooses the model.
- The "Remembered" facts and context-size gauge from the concept art are not
  shown because the service does not report them per answer.
- Today, Research, Memory and Settings are launchers into the existing
  windows, not embedded pages.
- The app stays menu-bar only (no Dock icon) while the window is open.

## Testing

```bash
scripts/test_chat_contract.sh            # reducer, history, titles, grouping, store (scripted backend)
.venv/bin/python tests/test_chat_list.py # GET /chats storage
```

Render the real window against a scripted fixture, in light and dark, without
starting Wisp or touching its port:

```bash
cd app
swift build -Xswiftc -DWISP_CHAT_QA
WISP_CHAT_QA_OUT=/some/dir .build/debug/WispApp
```

On a machine whose `swift` is the Command Line Tools toolchain, SwiftUI's `@State`
macro plugin is missing; add
`-Xswiftc -plugin-path -Xswiftc /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/usr/lib/swift/host/plugins`
to the build command.
