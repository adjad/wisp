# Product
<!-- impeccable:product-schema 1 -->

## Platform
macos  <!-- impeccable only recognizes web/ios/android/adaptive, so no platform guide loads automatically; use Apple's macOS HIG -->

## Users
Mac users on Apple silicon who download Wisp from GitHub Releases and set it up
themselves (onboarding, macOS permission prompts, model download). They use it
many times a day in short bursts from the notch, in the middle of other work.

## Product Purpose
An assistant that lives in the Mac's notch. It answers questions, reads Mail,
Messages, Notes, Calendar and Reminders, briefs the day, drafts messages (and
sends them only with approval), runs cited web research, and works the Mac
through tools. Success: small questions and chores get done without opening
an app, and the user trusts that nothing happens without their consent.

## Positioning
Local first. Inference, memory, private stores and embeddings run on the Mac
through the bundled oMLX engine by default. Cloud providers are an explicit
opt-in for some generation tasks: never the default, never silent. Web
search and fetch are the one routine outbound path.

## Operating Context
Notch panel (⌥Space or hover) with the tabs Ask · Brief · Day, a hidden
Research mode and quick settings. When collapsed it is a black bar around the
camera housing (a virtual notch on built-in screens without one, a capsule on
external monitors). The Wisp window holds chats, the Today planner, the
Research library, Memory and Settings. ⌘⇧F opens Smart Search; ⌥⇧Space asks
by voice. The notch panel never takes focus from other apps, so it can't
show sheets or alerts.

## Capabilities and Constraints
- macOS 14+ on Apple silicon; the bundled engine needs macOS 26.2+.
- Full access is on by default, so most actions run without a card. Sending mail or messages, calendar changes, AppleScript, installing a tool, permanent deletes and shell commands that can send data off the Mac always ask. With Full access off, risky actions ask too: Deny / Always allow (limited to a folder or site) / Allow once. The choice does not yet survive a restart.
- Pictures, video clips up to 30 s and documents are read by the local model in one call with no tools.
- Notch panel is 640 pt wide; the notch itself is 185 × 32 pt.
- Undecided: whether the iPhone app joins this design context (out of scope for now).

## Brand Commitments
- Name: Wisp. Mark: the orb.
- The notch stays black and monochrome so it reads as part of the hardware.
- The notch comes first: most use happens there. The window is for long reading, one-time setup, and settings that widen access.
- The default voice is gentle, not upbeat.

## Evidence on Hand
docs/design/apple-ui-refresh (audit, prototype, 14 exports) and the README
feature list. There are no testimonials, user counts or marketing benchmarks;
don't invent any.

## Product Principles
1. Local by default, cloud only by explicit choice, and the UI always says which one is running.
2. Nothing acts without consent whose limits the user can see.
3. The notch is for a glance; the window is for reading.
4. Calm over clever: gentle voice, quiet motion, no fake progress.

## Accessibility & Inclusion
VoiceOver, Reduce Motion, Increase Contrast and Text size (Default/Large/Extra
large). Text meets WCAG AA contrast in both light and dark.
