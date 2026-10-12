# iPhone capability matrix

Verified against official Apple documentation on October 10, 2026. Implementation
is an initial source inspector, not a semantic replacement for the Mac service.

| Workflow families | Foundation behavior | Incomplete capability |
| --- | --- | --- |
| 01 overview, 02 daily | Synthetic/imported source excerpts with IDs and coverage limitations | Native inboxes and semantic summaries |
| 03 calendar ranges | Synthetic today interval in explicit timezone | Tomorrow/week/month natural-language planning |
| 04 California weather | Literal search over explicitly synthetic forecast | Live WeatherKit, attribution, typo/city resolution |
| 05 priorities | Reminder/calendar excerpts labeled unranked | Grounded priority selection and estimates |
| 06 contact said, 07 thread recap | Literal search over structured synthetic sender/thread | Identity disambiguation and recap synthesis |
| 08 groceries | Synthetic note/reminder excerpts | Item merging, completed/negated item resolution, native Notes access |
| 09 event time | Literal synthetic event search | Ambiguity handling and structured answer generation |
| 10 reminder/calendar create | Clarification plus explicit unavailable integration | Native creation, exact time and write approval |
| 11 SFO trip | Unsupported; no schedule or Maps action | Scoped location, live traffic, arrival planning and handoff |
| 12 grounded communication | No send/draft/queue effects | Cross-source drafting, composers and scheduled provider delivery |
| 13 unsubscribe | Unsupported with zero effects | Complete engagement history and authorized provider integration |
| 14 file organization | Unsupported with zero effects | Scoped directory access, preview plan, approved reversible moves |
| 15 personal knowledge | Synthetic memory/note excerpts | Persistent index, editable memory, follow-ups and receipts |
| 16 meeting | Clarification plus explicit unavailable integration | Contact resolution, after-event timing, event/invitation writes |
| 17 fuzzy test | Literal search labeled limited | Validated semantic aliases and ambiguity handling |
| 18 unread | Only explicit `unread == true`; unknown is disclosed | Native unread synchronization and importance ranking |
| 19 availability reply | Clarification plus explicit unavailable integration | Interval reasoning, grounded draft and exact approved send |
| 20 pending actions | Synthetic queue inspection (empty fixture) | Durable queue, inspect/cancel effects and receipts |

The shared contract-only adapter reports all twenty semantic families unsupported.
Its result cannot establish capability completion, even where the app can show
useful excerpts. Zero effects are intentional in the foundation, not a claim that
requested actions succeeded.

## Platform boundaries

- **Messages/Mail:** baseline uses selected paste/import. MessageUI offers
  user-operated composers, not a general inbox reader; composition does not prove
  delivery. No general native Apple Mail inbox API was established by the reviewed
  public docs. Specialized TelephonyMessagingKit carrier messaging requires the
  default-app role, entitlement and EU eligibility; it is not general iMessage
  archive access and is outside this port.
  [MessageUI](https://developer.apple.com/documentation/messageui),
  [TelephonyMessagingKit](https://developer.apple.com/documentation/TelephonyMessagingKit).
- **Calendar/Reminders:** EventKit reads require full access; there is no read-only
  permission. Write-only calendar authorization cannot read events. Included
  request methods are not connected to the app UI. Source methods never save
  events or reminders.
  [Event store access](https://developer.apple.com/documentation/eventkit/accessing-the-event-store).
- **Contacts:** permission state includes limited access on iOS 18+. Limited data
  cannot establish a complete directory. Contact notes are excluded; no native
  contact lookup is currently connected.
  [Contacts access](https://developer.apple.com/documentation/contacts/accessing-the-contact-store).
- **Scheduling:** notifications remind users; they do not send messages. Background
  refresh runs at system-chosen times and cannot promise exact-time delivery.
  Neither facility is configured in this app.
  [Background strategies](https://developer.apple.com/documentation/backgroundtasks/choosing-background-strategies-for-your-app),
  [Local notifications](https://developer.apple.com/documentation/usernotifications/scheduling-a-notification-locally-from-your-app).
- **Maps/Weather:** live directions and forecasts require separately scoped service
  calls, WeatherKit capability/attribution, and location permission when needed.
  No provider calls or location prompts are made here.
  [Directions](https://developer.apple.com/documentation/mapkit/mkdirections),
  [WeatherKit](https://developer.apple.com/weatherkit/).
- **Inference:** `SystemLanguageModel.default` is Apple's on-device model; the
  broader FoundationModels framework also has provider/cloud functionality, so
  framework import alone does not establish locality. Device/region/Apple
  Intelligence/model readiness affect availability. No automatic activation,
  model download, load, inference, latency or thermal measurement occurred.
  [SystemLanguageModel](https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel).
- **Import:** explicit paste and balanced security-scoped file access; a share
  extension would be a separate target. Text import is capped at 64 KiB and stays
  in memory. UI layout, paste prompt behavior and revocation still require actual
  simulator/device runtime QA.
  [Paste control](https://developer.apple.com/documentation/uikit/uipastecontrol),
  [File access](https://developer.apple.com/documentation/uikit/providing-access-to-directories).

## Readiness limits

Unsigned simulator compilation covers SwiftUI, iOS API availability and framework
linkage for arm64/x86_64. It does not prove a signed device build, installation,
permissions, UI interaction, inference quality, memory, thermals, speed or battery
behavior. No supported simulator device was available during inventory. Native
permission adapters need isolated specialist QA before integration. Independent
release review and any required QA must bind the exact frozen candidate SHA.
