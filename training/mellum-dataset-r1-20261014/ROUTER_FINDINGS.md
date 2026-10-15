# Router findings from the R1 gold replay

Generated from `gold_log.jsonl` after `build.py gold`. Each row is a training item the real router
made impossible to answer correctly: the gold tool was not offered, or the loop forced a tool on a
no-tool answer. These items are dropped (no rows), so they are router fixes, not training targets.

| family | dropped | why | router reason | example prompts |
|---|---|---|---|---|
| mail_reply_landlord | 24/35 | gold call ['reply_to_email'] not offered | messages+email read+write -> scoped tools (6) | reply to the landlord and say is the laundry room affected too?; reply to the landlord and say thanks for the heads up; reply to the landlord and say that works for me |
| rem_weekday | 20/60 | gold call ['add_reminder'] not offered | unresolved reminder task clauses -> clarify before effects | on friday, remind me to book a haircut; on friday, remind me to renew my passport; on friday, remind me to take my vitamins |
| fu_reply_yes | 20/60 | gold call ['send_message'] not offered | ambiguous -> retrieved tools (19), model decides | say that works |
| mail_dont_reply | 20/53 | gold call ['view_emails'] not offered | messages read+write -> scoped tools (5) | show me the Amex message — no reply needed; show me the Capital One message — no reply needed; show me the Chase message — no reply needed |
| mail_balance | 16/58 | gold call ['view_emails'] not offered | ambiguous -> retrieved tools (19), model decides | check my Wells Fargo balance for me; how much is my Capital One payment?; how much is my Citi payment? |
| mail_balance | 14/58 | gold call ['view_emails'] not offered | ambiguous -> retrieved tools (21), model decides | check my Amex balance for me; check my Chase balance for me; check my Citi balance for me |
| mail_balance | 12/58 | gold call ['view_emails'] not offered | current public information -> web_search on the agent model (router-direct) | what's the Amex bill this month?; what's the Capital One bill this month?; what's the Chase bill this month? |
| safe_send_nothing | 12/16 | loop asked for a step the plan does not have | messages+email read+write -> scoped tools (6) | go ahead and send; ok send that; send it now |
| mail_reply_landlord | 7/35 | gold call ['reply_to_email'] not offered | email lookup -> scoped tools (4) | answer the landlord's email: thanks for the heads up; answer the landlord's email: that works for me |
| cal_add_offset | 5/48 | gold call ['add_calendar_event'] not offered | ambiguous -> retrieved tools (24), model decides | set up a call with the electrician the day after tomorrow at 4:30pm; set up a call with the electrician the day after tomorrow at noon |
| chat_writing | 4/16 | loop asked for a step the plan does not have | messages+email+notes read+write -> scoped tools (7) · forced search_notes first (unresolved topic) | compose a haiku about the ocean |
| chat_writing | 4/16 | loop asked for a step the plan does not have | messages lookup -> scoped tools (2) | write a two-line birthday message for my coworker |
| safe_vague_folder | 4/12 | loop asked for a step the plan does not have | action verb implies operating the machine -> retrieved tools (11) | remove the old backups folder |
| mail_reply_landlord | 4/35 | gold call ['view_emails'] not offered | public follow-up is ambiguous -> clarify without tools | answer the landlord's email: is the laundry room affected too? |
| files_found_stop | 4/16 | gold call ['find_files'] not offered | messages+email read+write -> scoped tools (5) | where's my resume draft? |
| safe_vague_folder | 4/12 | loop asked for a step the plan does not have | action verb implies operating the machine -> retrieved tools (10) | delete the old cache directory |
| mail_balance | 4/58 | gold call ['view_emails'] not offered | ambiguous -> retrieved tools (18), model decides | how much is my Amex payment?; how much is my Chase payment? |
| mail_dont_reply | 3/53 | gold call ['view_emails'] not offered | email intent -> agent (full toolset) -> retrieved tools (21) | open the Wells Fargo email, don't respond to it |
| cal_add_offset | 3/48 | gold call ['add_calendar_event'] not offered | ambiguous -> retrieved tools (23), model decides | set up a call with the electrician the day after tomorrow at 2pm; set up a call with the electrician the day after tomorrow at 3pm |
| cal_add_weekday | 2/60 | gold call ['add_calendar_event'] not offered | ambiguous -> retrieved tools (16), model decides | block off wednesday at 4:30pm for a haircut |
| cal_add_weekday | 1/60 | gold call ['add_calendar_event'] not offered | ambiguous -> retrieved tools (21), model decides | block off monday at noon for a call with the bank |
| cal_add_weekday | 1/60 | gold call ['add_calendar_event'] not offered | ambiguous -> retrieved tools (17), model decides | block off sunday at 4:30pm for lunch with Priya |
