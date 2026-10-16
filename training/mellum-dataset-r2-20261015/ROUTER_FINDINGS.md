# Router findings from the R2 gold build

From `gold_log.jsonl` after `build.py gold --engine` on `main` at 40ffe997 (PRs #239 + #241). Each row is a gold item the router made
impossible to answer: training cannot fix these. Routes marked *retrieved* come from the packaged
`tool_retrieval.provider: lexical` (the user's config does not override it), so they are production menus. The
offline stub-embedder run gave the same yields in every family (11/1434 menus differed, none changed an outcome).

| family | items | why dropped | router reason | example prompts | read |
|---|---|---|---|---|---|
| mail_card_balance | 41/57 | `view_emails` not offered | ambiguous -> retrieved tools | "Amex card — what's the balance?"; "what do I owe on my Chase card?" | *retrieved* (lexical): "card"/"owe" phrasings miss the `email lookup` rule that "What's my Chase balance?" hits |
| inj_file_read, inj_file_do_it | 19/96 | `read_file` not offered | todo lookup -> scoped tools | "read ~/Documents/todo.txt for me" | **router fault**: a file path containing "todo" routes to reminders |
| inj_file_read, inj_file_do_it | 18/96 | `read_file` not offered | file/document task -> retrieved tools | "can you read ~/Documents/meeting_notes.md?" | *retrieved*: an explicit path plus "read" should pin `read_file` |
| msg_channel_photos | 11/50 | `view_messages` not offered | ambiguous -> retrieved tools | "any pics from Alex yet?" | *retrieved* |
| msg_channel_mom | 4/12 | `view_messages` not offered | current public information -> web_search (router-direct) | "did mom text me today?" | **router fault**: "today" makes a personal-messages question a web search |
| menu_recall_facts | 4/20 | `recall` not offered | calendar lookup -> scoped tools | "remind me which company I work for" | **router fault**: "remind me" read as a reminder/calendar lookup |
| chat_writing_tools_offered | 4/24 | loop forced a tool | notes lookup -> scoped tools | "write a short thank-you note to my neighbor…" | **router fault**: "note" forces `search_notes` on a writing request |
| safe_vague_folder | 8/12 | loop forced a tool | action verb implies operating the machine | "delete the old cache directory" | known, left as is by #239 (clarify-target flow) |

Not router faults (the family's own menu check): `chat_writing_tools_offered` 16 and `chat_define_tools_offered` 10
items routed with no tempting tool on the menu. For creative writing, that is the #239 router rule ("creative writing
request -> answer without tools"), which now covers the gate's `chat_toast` / `chat_haiku` before the model sees them.
`menu_recall_facts` 4 items offered `search_notes`, so they do not show the menu-lacks-the-tool situation.
