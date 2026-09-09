# Wisp user-style workflow test prompts

These prompts are frozen before execution. Live outbound cases use Mom or `johnstandark@gmail.com`; the runner denies every confirmation, so nothing is sent. Draft cases use Wisp test mode and do not open applications.

Strict failure conditions: wrong or missing route, wrong/extra/reordered tool, wrong bound recipient/channel, missing confirmation, any source/tool error, grounding rejection, incomplete action, or runtime over the case threshold.

## complete live workflows

### core_01_calendar_mom_messages

Calendar summary to Mom through Messages

1. **Send Mom a summary of my calendar tomorrow via Messages.**

Expected: get_upcoming → lookup_contact → send_message

### core_02_calendar_john_email

Calendar summary to an exact email address

1. **Email johnstandark@gmail.com a summary of my calendar for this week.**

Expected: get_upcoming → send_email

### core_03_inbox_mom_messages

Inbox summary delivered over Messages

1. **Send Mom a message with a summary of my emails from today.**

Expected: summarize_emails → lookup_contact → send_message

### core_04_stock_names_mom

Historical stock comparison using company names

1. **Send Mom a message comparing NVIDIA and AMD share prices now with two weeks ago. Be descriptive.**

Expected: get_stock_price → lookup_contact → send_message

### core_05_calendar_stock_mom

Two-source report to Mom

1. **Send Mom a Messages update with my calendar for tomorrow and AAPL and NVDA stock prices over the last week.**

Expected: get_upcoming → get_stock_price → lookup_contact → send_message

### core_06_weather_calendar_email

Weather and calendar summary by email

1. **Email johnstandark@gmail.com tomorrow's Dublin, CA weather and my calendar summary.**

Expected: get_upcoming → get_weather → send_email

### core_07_five_tool_compound

Four independent sources followed by one email action

1. **Email johnstandark@gmail.com a report with my calendar and inbox for today, Dublin, CA weather, and AAPL and NVDA performance over the last two weeks.**

Expected: get_upcoming → summarize_emails → get_stock_price → get_weather → send_email

### core_08_historical_scheduled_typo

Historical misspellings must preserve scheduled-send semantics

1. **scedule send a message to mom at 9pm with my schedule for tmrow**

Expected: get_upcoming → lookup_contact → schedule_send

### core_09_scheduled_news_email

Scheduled news report

1. **Schedule a technology news report to johnstandark@gmail.com at 9pm tonight.**

Expected: web_search → schedule_send

### core_10_daily_brief_mom

Daily brief delivered to Mom

1. **Send Mom my daily brief through Messages.**

Expected: daily_brief → lookup_contact → send_message

## historical broader assistant tasks

### regression_01_move_in_date

Historical move-in-date relay should fetch remembered data before sending

1. **Send Mom a message with my move-in date.**

Expected: recall → lookup_contact → send_message

### regression_02_reminder_then_notify

Compound reminder plus Mom notification

1. **Create a reminder for me to finish a Canvas assignment by tonight and tell Mom about it too through Messages.**

Expected: add_reminder → lookup_contact → send_message

## multi-turn context

### follow_01_channel_messages

Channel clarification retains recipient and calendar source

1. **Send Mom my calendar summary for tomorrow.**
2. **Messages**

Expected: Should I deliver that through Messages or email? | get_upcoming → lookup_contact → send_message

### follow_02_channel_address

Email address supplies channel and recipient

1. **Send my calendar summary for tomorrow.**
2. **johnstandark@gmail.com**

Expected: Should I deliver that through Messages or email? | get_upcoming → send_email

### follow_03_missing_recipient

Recipient clarification retains channel and payload

1. **Send my calendar summary for tomorrow through Messages.**
2. **Mom**

Expected: Who should I message it to? | get_upcoming → lookup_contact → send_message

### follow_04_missing_schedule_time

Scheduled delivery waits for a time

1. **Schedule a message to Mom with tomorrow's calendar summary.**
2. **At 9pm tonight**

Expected: When should I send it? | get_upcoming → lookup_contact → schedule_send

### follow_05_missing_weather_location

Weather location clarification retains calendar and email

1. **Email johnstandark@gmail.com my calendar and weather report for tomorrow.**
2. **Dublin, CA**

Expected: Which city should I use for the weather? | get_upcoming → get_weather → send_email

### follow_06_missing_stocks

Stock clarification retains Mom and Messages

1. **Send Mom my stock movements today via Messages.**
2. **Google and Micron**

Expected: Which stock symbols or company names should I include? | get_stock_price → lookup_contact → send_message

### follow_07_two_missing_source_details

Weather and stock clarifications happen without losing either source

1. **Email johnstandark@gmail.com my calendar, weather, and stock report for tomorrow.**
2. **Dublin, CA**
3. **AAPL and NVDA**

Expected: Which city should I use for the weather? | Which stock symbols or company names should I include? | get_upcoming → get_stock_price → get_weather → send_email

### follow_08_cancel_pending

Cancellation terminates a pending workflow

1. **Send Mom my calendar summary.**
2. **Never mind**

Expected: Should I deliver that through Messages or email? | Okay, I cancelled that request.

## non-workflow regression guards

### negative_01_plain_reminder

A reminder must not become a delivery workflow

1. **Create a reminder tomorrow to send my vaccine report to UCSC.**

Expected: add_reminder

### negative_02_calendar_read

A calendar read remains a read

1. **Check my calendar for next month.**

Expected: get_upcoming

### negative_03_inbox_read

An inbox summary remains a read

1. **Summarize my emails for today.**

Expected: summarize_emails

### negative_04_simple_text

A simple text remains on the ordinary message route

1. **Text Mom that I will call her later.**

Expected: send_message

### negative_05_purchase_email_search

Historical inbox search does not become an outbound email

1. **Can you check my email for purchases from PlayStation?**

Expected: view_emails

### negative_06_calendar_read_typo

Misspelled calendar read remains read-only

1. **Check my calender for next month.**

Expected: get_upcoming

## safe dry-run workflows

### dry_01_calendar_dad_draft

Historical calendar draft to Dad

1. **Write a message to my dad with my upcoming calendar events for the next month.**

Expected: get_upcoming → lookup_contact → draft_message

### dry_02_inbox_email_draft

Explicit Mail draft

1. **Draft an email to johnstandark@gmail.com summarizing my inbox today, but do not send it.**

Expected: summarize_emails → draft_email

### dry_03_self_email_defaults_draft

Immediate self-email uses a reviewable draft

1. **Send a news report to my email.**

Expected: web_search → draft_email

### dry_04_self_email_scheduled

Timed self-email remains a scheduled action

1. **Send a news report to my email at 10am tomorrow.**

Expected: web_search → schedule_send

