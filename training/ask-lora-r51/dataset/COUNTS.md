# r5.1 dataset counts (before = r5-final, after = r5.1)

Rows: 7673 -> 8228 (dropped 67 rows in 32 turns; new rows 622; train 7452, valid 776).

## Family

| Family | before | after | delta |
|---|---:|---:|---:|
| act_dont_ask | 81 | 81 | +0 |
| act_dont_promise | 80 | 80 | +0 |
| all_sources_r5 | 276 | 276 | +0 |
| capability_honest | 76 | 76 | +0 |
| chat_contrast_r5 | 132 | 132 | +0 |
| chat_direct_r4 | 52 | 52 | +0 |
| chat_voice_r4 | 245 | 245 | +0 |
| compose_r3 | 404 | 402 | -2 |
| concise_gentle | 62 | 62 | +0 |
| contacts_r3 | 125 | 125 | +0 |
| day_answers_r4 | 275 | 273 | -2 |
| day_chat_r4 | 462 | 453 | -9 |
| debug_log_r5 | 166 | 154 | -12 |
| device_r3 | 110 | 110 | +0 |
| exact_args | 87 | 87 | +0 |
| finish_the_write | 117 | 117 | +0 |
| followup_r3 | 216 | 214 | -2 |
| gather_then_send_r4 | 424 | 409 | -15 |
| grounded_only | 72 | 72 | +0 |
| ignore_embedded_instructions | 55 | 55 | +0 |
| info_answers_r4 | 210 | 210 | +0 |
| instructions_r4 | 283 | 283 | +0 |
| knowledge_direct | 62 | 62 | +0 |
| long_email_r5 | 80 | 80 | +0 |
| lookup_answers_r4 | 293 | 293 | +0 |
| mail_r3 | 156 | 156 | +0 |
| memory_contrast_r5 | 203 | 203 | +0 |
| multi_source | 57 | 57 | +0 |
| no_write_for_reads | 52 | 52 | +0 |
| picture_turn_r3 | 193 | 192 | -1 |
| picture_turn_r5 | 48 | 40 | -8 |
| recover_from_tool_error | 76 | 76 | +0 |
| relay_receipt | 71 | 71 | +0 |
| reminders_r3 | 122 | 118 | -4 |
| restraint_r4 | 258 | 258 | +0 |
| right_source | 92 | 92 | +0 |
| right_write_tool | 86 | 86 | +0 |
| safe_effects | 128 | 126 | -2 |
| safe_restraint | 280 | 280 | +0 |
| safety_contrast_r51 | 0 | 622 | +622 |
| session_chat_r4 | 146 | 146 | +0 |
| session_recall_r4 | 199 | 194 | -5 |
| tool_choice_r5 | 726 | 721 | -5 |
| weather_and_web | 51 | 51 | +0 |
| web_r3 | 133 | 133 | +0 |
| write_contrast_r5 | 151 | 151 | +0 |

## Target kind

| Target kind | before | after | delta |
|---|---:|---:|---:|
| call | 3691 | 3962 | +271 |
| final | 3959 | 4241 | +282 |
| load | 23 | 25 | +2 |

## Target tool

| Target tool | before | after | delta |
|---|---:|---:|---:|
| add_calendar_event | 100 | 108 | +8 |
| add_reminder | 117 | 118 | +1 |
| append_note | 16 | 16 | +0 |
| calculate | 13 | 13 | +0 |
| cancel_event | 19 | 31 | +12 |
| clear_past_reminders | 23 | 23 | +0 |
| clear_reminders | 25 | 53 | +28 |
| clipboard_read | 2 | 2 | +0 |
| complete_reminder | 19 | 28 | +9 |
| contact_dates | 8 | 8 | +0 |
| convert_units | 27 | 27 | +0 |
| create_note | 37 | 37 | +0 |
| daily_brief | 5 | 5 | +0 |
| draft_email | 80 | 104 | +24 |
| draft_message | 59 | 56 | -3 |
| find_files | 11 | 11 | +0 |
| find_free_time | 39 | 39 | +0 |
| find_place | 0 | 7 | +7 |
| forget | 12 | 12 | +0 |
| get_my_day_plan | 485 | 476 | -9 |
| get_past_events | 17 | 17 | +0 |
| get_recent_activity | 11 | 11 | +0 |
| get_stock_price | 70 | 69 | -1 |
| get_tool_schemas | 23 | 25 | +2 |
| get_upcoming | 345 | 370 | +25 |
| get_weather | 142 | 147 | +5 |
| list_contacts | 2 | 2 | +0 |
| list_dir | 22 | 22 | +0 |
| list_scheduled_sends | 7 | 7 | +0 |
| lookup_contact | 81 | 110 | +29 |
| mac_mute | 1 | 1 | +0 |
| mac_set_volume | 4 | 4 | +0 |
| manage_contacts | 35 | 35 | +0 |
| manage_timers | 41 | 41 | +0 |
| move_path | 8 | 8 | +0 |
| organize_files | 32 | 32 | +0 |
| predict_my_day | 3 | 3 | +0 |
| read_file | 30 | 30 | +0 |
| recall | 171 | 171 | +0 |
| remember | 22 | 22 | +0 |
| reply_to_email | 6 | 18 | +12 |
| schedule_send | 43 | 42 | -1 |
| search_notes | 197 | 202 | +5 |
| search_reminders | 185 | 199 | +14 |
| send_email | 143 | 158 | +15 |
| send_message | 267 | 292 | +25 |
| set_timer | 13 | 13 | +0 |
| summarize_emails | 89 | 89 | +0 |
| summarize_messages | 26 | 26 | +0 |
| system_status | 63 | 63 | +0 |
| toggle_setting | 22 | 22 | +0 |
| update_event | 72 | 83 | +11 |
| update_reminder | 28 | 29 | +1 |
| view_emails | 416 | 459 | +43 |
| view_messages | 235 | 246 | +11 |
| web_fetch | 9 | 9 | +0 |
| web_search | 108 | 108 | +0 |
| wisp_capabilities | 64 | 64 | +0 |
| wisp_status | 3 | 3 | +0 |
| world_time | 23 | 23 | +0 |

## Intent

| Intent | before | after | delta |
|---|---:|---:|---:|
| answer | 3650 | 3626 | -24 |
| authorized_write | 629 | 760 | +131 |
| clarify | 2 | 69 | +67 |
| direct_send | 384 | 459 | +75 |
| draft | 129 | 182 | +53 |
| embedded_instruction | 0 | 32 | +32 |
| leave_alone | 0 | 36 | +36 |
| leave_alone_or_clarify | 309 | 309 | +0 |
| read_only | 16 | 112 | +96 |
| read_or_answer | 2470 | 2453 | -17 |
| refused | 0 | 43 | +43 |
| reply | 41 | 105 | +64 |
| schedule_send | 43 | 42 | -1 |

Intent rule: write rows take the audit label; new rows their recorded gold intent; other rows are read_or_answer (call/load targets) or answer (finals), except restraint finals (family in ['restraint_r4', 'safe_restraint'] or meta.clarify) which are leave_alone_or_clarify.
Combined sha256 covers: all.jsonl, drop_manifest.jsonl, meta.jsonl, sample_labels.jsonl, train.jsonl, train.meta.jsonl, valid.jsonl, valid.meta.jsonl, validate.log (not stats.json / COUNTS.md).

Combined sha256: 193387e309e5b87aa7ff0c0a40dacc3861c3e8a7739b511aafe3d8c0cec5d6bb
