# iMessage priority digest handoff

- Outcome: default and Daily Summary digests include only recent unread non-noise messages and narrowly defined critical read messages. Explicit conversation summaries remain unfiltered.
- Base SHA: 4c519c5334bbd661ed3f855dba672a428f78e59b.
- Sole writer: Wisp Hub in the imessage-priority-digest worktree.
- Owned paths: service/tools/imessage_tools.py, service/assistant/brief.py, tests/test_message_digest.py, tests/test_brief_fallback.py, tests/test_daily_summary_delivery.py, and this handoff.
- Validation plan: synthetic selection, read-state, spam, three-day window, Daily Summary, and explicit-chat tests; then repository CI and independent review for the exact candidate SHA. No live Messages database is needed.
- Dependencies: keep the native V2 U/R contract and per-chat identity semantics. Do not alter MessagesReader.swift or the unrelated web-retrieval worktree.

Critical read signals in this slice are direct call/FaceTime/meetup or pickup requests, an immediate safety emergency or injury, and a concrete change to a meeting, appointment, pickup, or call. Ordinary questions, routine plans, financial mentions, school/work topics, and messages sent by the user do not qualify merely by containing a broad keyword. Unknown read-state records are withheld from the automatic recent digest until a current V2 sync supplies read state.
