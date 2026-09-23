# Wisp trace collection prompts

Run these in separate fresh Wisp sessions with Debug Mode enabled. Export the
JSON after each small group and keep the files local. Include the prompt ID in
your correction note; the debug export itself has no reliable ground-truth
label. If the answer depends on private data, write the expected outcome in
your own words without copying full mail or message bodies into the note.

These prompts are **read-only**. The conditional send and failed-send cases are
run by the isolated synthetic replay runner, where no real message is sent.
Wisp's generic test mode replaces tool results with a stub, so it cannot grade
whether an action correctly depends on a calendar or note result.

| ID | Prompt | Correction note to record |
| --- | --- | --- |
| R01 | What actual calendar events do I have tomorrow? Exclude reminders and include their times. | Correct event titles and times, or none. |
| R02 | What is on my calendar for the rest of today? Distinguish events from reminders. | Correct items and sources, or none. |
| R03 | What do I have coming up this week that mentions a meeting? | Correct date range and matching items. |
| R04 | Do I have any overlapping calendar events tomorrow? | Correct pairs, or none. |
| R05 | Find the most recent note about a topic I choose and tell me its title. | Replace “a topic I choose” with a real keyword; record the expected title. |
| R06 | Search my notes for a phrase I choose. Tell me if there is no match. | Replace “a phrase I choose” with a real phrase; record whether a match exists. |
| R07 | Summarize my newest unread email and identify its sender and subject. | Correct sender/subject and whether it was unread. |
| R08 | What was the last message from a contact I choose? Quote only the relevant line. | Replace “a contact I choose” with a real contact; record the expected line privately. |
| R09 | What severe weather alerts, if any, are active for a US city I choose? | Replace with a city/state; record the alert source and timestamp. |
| R10 | Find a contact I choose. If there are multiple matches, show the ambiguity. | Replace with a contact name; record the expected match count. |

After each run, note **correct / incorrect / unsure**, the first wrong step if
known (route, tool, arguments, use of result, final answer), and the expected
behavior. “Unsure” remains ungraded until evidence is available. Do not edit
the original export; corrections are separate records tied to its hash.
