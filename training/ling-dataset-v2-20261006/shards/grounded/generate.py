#!/usr/bin/env python3
"""Deterministic, synthetic, fixture-only Wisp summary authoring. Stdlib only."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

SEED = 610220261006 + 307
SYSTEM = ('Summarize the synthetic Wisp source results supplied in the user message. '
          'Use only that fixture; no source access or actions occur. Give a concise, readable overview '
          'responsive to the request. Preserve exact dates, timezones, counts, speakers, and draft or '
          'plan status where relevant. Distinguish empty results from unavailable results and state '
          'partial or truncated coverage. Do not infer urgency, deadlines, permission, or completed effects.')
# Family partition is fixed here, BEFORE slots or wording are expanded.
TRAIN_FAMILIES = (
    'two_event_day', 'overlapping_day', 'cross_zone_day', 'empty_calendar_day',
    'due_reminder_pair', 'overdue_with_open', 'undated_reminder', 'partial_reminders',
    'email_decision', 'draft_and_sent', 'email_timeout', 'truncated_inbox',
    'attributed_exchange', 'proposed_meeting', 'message_correction', 'note_tasks',
    'conflicting_notes', 'truncated_note', 'calendar_reminder_agenda', 'mail_calendar_disagreement',
    'note_message_proposal', 'five_source_partial', 'empty_and_denied', 'three_source_preparation',
)
DEV_FAMILIES = (
    'zone_handover_with_task', 'scattered_week', 'canceled_and_active', 'reminder_dependency',
    'mail_revision_sequence', 'three_speaker_roles', 'note_decision_history', 'travel_draft_overview',
    'five_source_availability', 'calendar_note_unlinked',
)
assert set(TRAIN_FAMILIES).isdisjoint(DEV_FAMILIES)
REQUESTS = {
 'two_event_day': ('Give me a short calendar overview for {day}.', 'What is on my calendar on {day}? Keep it brief.', 'Summarize the supplied events for {day} in time order.'),
 'overlapping_day': ('Highlight the overlap in this calendar day, {day}.', 'What stands out in these events on {day}?', 'Give a concise overview of the two appointments on {day}, including the timing conflict.'),
 'cross_zone_day': ('Summarize these calendar entries, keeping their timezones.', 'Give a brief overview of these two calls without converting their supplied local times.', 'What are the calls for {day}? Include each listed timezone.'),
 'empty_calendar_day': ('What does this calendar result say about {day}?', 'Briefly summarize the returned calendar for {day}.', 'Give me the event count for {day} from the supplied result.'),
 'due_reminder_pair': ('Summarize these reminders for {day}.', 'What are the two dated tasks in this reminder result?', 'Give a concise overview of the supplied due reminders.'),
 'overdue_with_open': ('Summarize the overdue and undated reminders as of {day}.', 'What is overdue in these results, and what has no date?', 'Give me a short status overview of these reminders using the fixture reference date.'),
 'undated_reminder': ('Briefly summarize this reminder without adding a deadline.', 'What does this single task say?', 'Give me a short overview of the supplied undated reminder.'),
 'partial_reminders': ('Summarize the available reminders and the coverage limit.', 'What can these partial reminder results tell me?', 'Give a brief task overview that says how much of the source was returned.'),
 'email_decision': ('Summarize the decision in this email.', 'What was agreed in the supplied email?', 'Give a short inbox overview focused on the stated decision.'),
 'draft_and_sent': ('Summarize these emails, distinguishing draft and sent.', 'What is the status of the two supplied messages?', 'Give a concise overview of this sent email and saved draft.'),
 'email_timeout': ('What can you tell me from this email result?', 'Summarize the inbox result and its availability.', 'Give me a brief email overview based on the supplied response.'),
 'truncated_inbox': ('Summarize the returned inbox items and state the limit.', 'Give me a brief overview of the visible emails, with the total match count.', 'What is in this capped email result?'),
 'attributed_exchange': ('Summarize who said what in this conversation.', 'Give a concise overview of this message exchange with speaker attribution.', 'What did each participant say in these messages?'),
 'proposed_meeting': ('Summarize the planning status in this conversation.', 'What is proposed here, and what is confirmed?', 'Give a brief message overview without treating a suggestion as an appointment.'),
 'message_correction': ('Summarize the latest correction in these messages.', 'What changed in this conversation?', 'Give me the current detail from this message exchange and mention the earlier value.'),
 'note_tasks': ('Summarize the two tasks in this note.', 'What actionable facts does this note contain?', 'Give a concise overview of the note without assigning extra deadlines.'),
 'conflicting_notes': ('Summarize the disagreement between these notes.', 'What do these two notes say about the venue?', 'Give a brief note overview preserving the conflicting details.'),
 'truncated_note': ('Summarize the visible part of this note and its limit.', 'What does this note excerpt tell me?', 'Give a brief overview of the returned text; acknowledge the truncation.'),
 'calendar_reminder_agenda': ('Give me a short agenda for {day} from these results.', 'Summarize the appointment and due task on {day}.', 'What is the supplied calendar-and-reminder agenda for {day}?'),
 'mail_calendar_disagreement': ('Summarize the mismatch between this email and calendar event.', 'What differs between these schedule sources?', 'Give a concise overview that keeps both listed meeting times.'),
 'note_message_proposal': ('Summarize this note and conversation, keeping plan status clear.', 'What is settled and what is suggested in these results?', 'Give a short overview of the recorded decision and the later proposal.'),
 'five_source_partial': ('Give a concise overview of these five source results and their limits.', 'What do the available results say, and which sources are unavailable?', 'Summarize the supplied Wisp sources without claiming complete coverage.'),
 'empty_and_denied': ('Summarize these empty and unavailable results.', 'What do these two source responses establish?', 'Give a brief overview that distinguishes zero returned emails from denied notes.'),
 'three_source_preparation': ('Summarize the meeting preparation facts across these sources.', 'Give a short overview of the event, email request, and note checklist.', 'What preparation details are supplied for this meeting?'),
 'zone_handover_with_task': ('Summarize this handover call and the related task.', 'What are the supplied handover details? Preserve the two local timestamps.', 'Give a short agenda for the handover, including the reminder.'),
 'scattered_week': ('Summarize these nonconsecutive appointments for the stated week.', 'Give a brief week overview of the supplied calendar entries.', 'Which dates have events in this returned week?'),
 'canceled_and_active': ('Summarize this cancellation and the remaining event.', 'What is canceled, and what is still confirmed in this result?', 'Give a concise calendar status overview of these two events.'),
 'reminder_dependency': ('Summarize the dependency between these tasks.', 'What must happen before the second reminder can proceed?', 'Give a brief overview of these blocked and open tasks.'),
 'mail_revision_sequence': ('Summarize the earlier estimate and the later email revision.', 'What changed between these two emails?', 'Give the latest stated estimate, with its email attribution.'),
 'three_speaker_roles': ('Summarize the different roles in this three-person conversation.', 'Who offers what in these messages?', 'Give a short overview that preserves each speaker’s commitment or question.'),
 'note_decision_history': ('Summarize this note’s proposal and final decision.', 'What was considered, and what was decided in the note?', 'Give a concise account of the note’s planning history.'),
 'travel_draft_overview': ('Summarize these travel facts and the draft reply.', 'What is recorded for the trip, and which email remains a draft?', 'Give a short travel overview across the supplied sources.'),
 'five_source_availability': ('Summarize availability across all five sources.', 'Which sources returned no items, and which could not be read?', 'Give a concise coverage overview from these source responses.'),
 'calendar_note_unlinked': ('Summarize the event and note without assuming they are linked.', 'Give a brief overview of these two supplied records.', 'What do the calendar and note each say here?'),
}

class Scene:
    def __init__(self, split, family, n):
        self.split, self.family, self.n = split, family, n
        self.surface = n % 3
        self.day = (dt.date(2029, 3, 5) + dt.timedelta(days=n * 9 + (37 if split == 'dev' else 0))).isoformat()
        self.nextday = (dt.date.fromisoformat(self.day) + dt.timedelta(days=1)).isoformat()
        self.end = (dt.date.fromisoformat(self.day) + dt.timedelta(days=6)).isoformat()
        self.project = ('Cedar', 'Juniper', 'Harbor', 'Orchard', 'Willow')[n % 5] + ' ' + str(n + 1)
        self.a, self.b, self.c = [('Mira', 'Jonah', 'Tess'), ('Ari', 'Leena', 'Omar'), ('Nia', 'Felix', 'Inez')][n % 3]
        self.zone = ('America/Los_Angeles', 'Europe/London', 'Asia/Kolkata')[n % 3]
        self.results, self.facts, self.phrases, self.lines = [], [], [], []
    def result(self, source, status='ok', items=(), **coverage):
        base = {'returned_count': len(items), 'complete': status == 'ok'}
        base.update(coverage)
        self.results.append({'source': source, 'status': status, 'coverage': base, 'items': list(items)})
    def item(self, source, item_id, **fields):
        for k, v in fields.items():
            self.facts.append({'source': source, 'item_id': item_id, 'field': k, 'value': v})
            if isinstance(v, str):
                self.phrases.append(v)
        return {'id': item_id, **fields}
    def say(self, *forms):
        self.lines.append(forms[self.surface % len(forms)])
    def must(self, *phrases):
        self.phrases.extend(phrases)
    def summary(self):
        # A short paragraph or natural bullets: never a large enumerated data dump.
        if self.surface == 1 and len(self.lines) > 1:
            return '\n'.join('- ' + line for line in self.lines)
        return ' '.join(self.lines)


def author(s):
    f, d, p, a, b, c, z = s.family, s.day, s.project, s.a, s.b, s.c, s.zone
    I, R, S = s.item, s.result, s.say
    if f == 'two_event_day':
        x, y = p + ' review', p + ' wrap-up'
        R('calendar', items=[I('calendar', 'c1', title=x, date=d, start='09:00', end='09:30', timezone=z), I('calendar', 'c2', title=y, date=d, start='15:00', end='15:20', timezone=z)], date=d)
        S(f'{d} has 2 events in {z}: {x}, 09:00–09:30, then {y}, 15:00–15:20.', f'On {d}, the 2 events are {x} (09:00–09:30) and {y} (15:00–15:20), both in {z}.', f'The 2 events for {d} are {x} from 09:00 to 09:30 and {y} from 15:00 to 15:20 ({z}).'); s.must('2 events')
    elif f == 'overlapping_day':
        x, y = p + ' workshop', p + ' check-in'
        R('calendar', items=[I('calendar', 'c1', title=x, date=d, start='10:00', end='11:00', timezone=z), I('calendar', 'c2', title=y, date=d, start='10:30', end='10:50', timezone=z)], date=d)
        S(f'On {d} ({z}), {x} runs 10:00–11:00 and {y} runs 10:30–10:50.', f'{d} includes {x} at 10:00–11:00 and {y} at 10:30–10:50 in {z}.', f'The calendar lists {x}, 10:00–11:00, and {y}, 10:30–10:50, for {d} in {z}.')
        S('The events overlap from 10:30 to 10:50.', 'The overlap is 10:30–10:50.', 'Both events occupy 10:30–10:50.'); s.must('10:30', '10:50')
    elif f == 'cross_zone_day':
        x, y = p + ' west call', p + ' east call'
        R('calendar', items=[I('calendar', 'c1', title=x, date=d, start='08:00', timezone='America/Los_Angeles'), I('calendar', 'c2', title=y, date=d, start='18:00', timezone='Europe/London')])
        S(f'For {d}, {x} is at 08:00 America/Los_Angeles; {y} is at 18:00 Europe/London.', f'The calls on {d} are {x} at 08:00 (America/Los_Angeles) and {y} at 18:00 (Europe/London).', f'{d}: {x}, 08:00 in America/Los_Angeles, followed in the supplied list by {y}, 18:00 in Europe/London.')
    elif f == 'empty_calendar_day':
        R('calendar', date=d, timezone=z, total_count=0)
        S(f'The complete calendar result for {d} ({z}) contains 0 events.', f'For {d} in {z}, the calendar returned 0 events with complete coverage.', f'Calendar coverage is complete for {d} ({z}): 0 events were returned.'); s.must(d,z,'0 events','complete')
    elif f == 'due_reminder_pair':
        x, y = p + ' outline', p + ' receipt'
        R('reminders', items=[I('reminders', 'r1', title=x, due_date=d, due_time='12:00', timezone=z, state='open'), I('reminders', 'r2', title=y, due_date=d, due_time='17:00', timezone=z, state='open')], reference_date=d)
        S(f'There are 2 open tasks due {d} in {z}: {x} at 12:00 and {y} at 17:00.', f'{x} is due at 12:00 and {y} at 17:00 on {d} ({z}); both are open.', f'On {d}, the open reminders are {x} (12:00) and {y} (17:00), with times in {z}.')
    elif f == 'overdue_with_open':
        old=(dt.date.fromisoformat(d)-dt.timedelta(days=2)).isoformat(); x,y=p+' invoice',p+' sketches'
        R('reminders',items=[I('reminders','r1',title=x,due_date=old,state='overdue'),I('reminders','r2',title=y,state='open',due_status='no due date')],reference_date=d)
        S(f'As of {d}, {x} is overdue from {old}. {y} is open with no due date.', f'{x} was due {old} and is overdue as of {d}; {y} is open and has no due date.', f'The result marks {x} overdue (due {old}, reference date {d}) and {y} open with no due date.');s.must(d)
    elif f == 'undated_reminder':
        x=p+' samples'
        R('reminders',items=[I('reminders','r1',title=x,state='open',due_status='no due date')])
        S(f'{x} is open with no due date.', f'The supplied reminder is {x}: open, with no due date.', f'One open reminder, {x}, is listed with no due date.')
    elif f == 'partial_reminders':
        x=p+' reference pack'
        R('reminders','partial',[I('reminders','r1',title=x,state='open',due_date=d)],returned_lists=1,requested_lists=3,total_count_known=False)
        S(f'{x} is open and due {d}. Only 1 of 3 requested reminder lists was returned; coverage is partial.', f'The partial result includes {x}, open and due {d}, from 1 of 3 requested reminder lists.', f'Available task: {x}, open, due {d}. Reminder coverage is partial: 1 of 3 requested lists returned.');s.must('partial','1 of 3')
    elif f == 'email_decision':
        title=p+' format'; body='Use the compact layout for the next review.'
        R('email',items=[I('email','e1',subject=title,sender=a,date=d,body=body)],total_count=1)
        S(f'In {title} ({d}), {a} states: {body}',f'{a} wrote in {title} on {d}: {body}',f'The email {title}, from {a} on {d}, records this decision: {body}')
    elif f == 'draft_and_sent':
        x,y=p+' invitation',p+' follow-up'
        R('email',items=[I('email','e1',subject=x,date=d,status='sent',recipient=a),I('email','e2',subject=y,date=d,status='draft',recipient=b)],total_count=2)
        S(f'On {d}, {x} to {a} is sent; {y} to {b} is a draft.', f'The 2 emails dated {d} are {x} to {a} (sent) and {y} to {b} (draft).', f'{x} addressed to {a} has status sent, while {y} addressed to {b} has status draft; both are dated {d}.')
    elif f == 'email_timeout':
        R('email','timeout',requested_date=d,attempted_account='work-'+str(s.n+1)+'@example.invalid')
        S(f'The email request for {d} ended in a timeout, so no inbox items are available to summarize.',f'Email coverage for {d} is unavailable because of a timeout; the result does not establish an empty inbox.',f'No email overview is available for {d}: the source reported a timeout.');s.must(d,'timeout')
    elif f == 'truncated_inbox':
        x,y=p+' quote',p+' design';t=7+s.n%4
        R('email','truncated',[I('email','e1',subject=x,sender=a,date=d),I('email','e2',subject=y,sender=b,date=d)],total_count=t,limit=2)
        S(f'The 2 returned emails on {d} are {x} from {a} and {y} from {b}.',f'Visible for {d}: {x} ({a}) and {y} ({b}), 2 returned emails.',f'{x} from {a} and {y} from {b} are the 2 returned emails dated {d}.')
        S(f'Coverage is truncated: 2 of {t} matches are shown.',f'The result is truncated to 2 of {t} matches.',f'Only 2 of {t} matches are visible because the result is truncated.');s.must('truncated',f'2 of {t}')
    elif f == 'attributed_exchange':
        x='The diagram is ready.';y='I will review the diagram.'
        R('messages',items=[I('messages','m1',speaker=a,date=d,text=x),I('messages','m2',speaker=b,date=d,text=y)],conversation=p)
        S(f'On {d}, {a} said, “{x}” {b} replied, “{y}”',f'{a}: “{x}” {b}: “{y}” Both messages are dated {d}.',f'The {d} exchange has {a} reporting, “{x}” and {b} saying, “{y}”')
    elif f == 'proposed_meeting':
        x=f'Could we meet on {s.nextday} at 14:00?'; y='I have not confirmed that time.'
        R('messages',items=[I('messages','m1',speaker=a,date=d,text=x,plan_status='proposed'),I('messages','m2',speaker=b,date=d,text=y,plan_status='unconfirmed')])
        S(f'On {d}, {a} asked, “{x}” The meeting is proposed. {b} said, “{y}” so it remains unconfirmed.',f'{a}’s {d} message is a proposed meeting: “{x}” {b}’s response, “{y}”, leaves it unconfirmed.',f'The {d} exchange records a proposed time from {a}: “{x}” {b} replied, “{y}” Status: unconfirmed.')
    elif f == 'message_correction':
        x='Room Maple was listed earlier.';y='Correction: use Room Birch.'
        R('messages',items=[I('messages','m1',speaker=a,date=d,text=x),I('messages','m2',speaker=a,date=d,text=y)])
        S(f'On {d}, {a} first said, “{x}” and then corrected it: “{y}”',f'{a} updated the room on {d}: “{x}” followed by “{y}”',f'The latest message from {a} on {d} is “{y}” It revises the earlier “{x}”')
    elif f == 'note_tasks':
        title=p+' checklist';body='Compare the two samples. Save the chosen reference locally.'
        R('notes',items=[I('notes','n1',title=title,date=d,text=body)])
        S(f'{title} ({d}) lists two tasks: {body}',f'The note {title}, dated {d}, says: {body}',f'In {title} on {d}, the checklist is: {body}')
    elif f == 'conflicting_notes':
        x,y=p+' plan A',p+' plan B';v1,v2='Venue: Studio North.','Venue: Studio South.'
        R('notes',items=[I('notes','n1',title=x,date=d,text=v1),I('notes','n2',title=y,date=d,text=v2)])
        S(f'{x} ({d}) says “{v1}” {y} ({d}) says “{v2}”',f'The {d} notes disagree: {x} records “{v1}” while {y} records “{v2}”',f'For {d}, {x}: “{v1}” {y}: “{v2}”')
        S('The fixture does not resolve which venue is final.','No final venue is established by these two notes.','Neither note identifies a final choice.')
    elif f == 'truncated_note':
        title=p+' research';body='The first sample has a rough edge.'
        R('notes','truncated',[I('notes','n1',title=title,date=d,visible_text=body)],text_complete=False,visible_characters=len(body))
        S(f'The visible text of {title} ({d}) says: {body} The note is truncated, so its remaining contents are unavailable.',f'{title}, dated {d}, is truncated. Its visible text is: {body}',f'In the truncated note {title} ({d}), the available excerpt is: {body}');s.must('truncated')
    elif f == 'calendar_reminder_agenda':
        x,y=p+' review',p+' print packet'
        R('calendar',items=[I('calendar','c1',title=x,date=d,start='13:00',timezone=z)])
        R('reminders',items=[I('reminders','r1',title=y,due_date=d,due_time='11:00',timezone=z,state='open')])
        S(f'For {d} in {z}, the reminder {y} is open and due 11:00; {x} is on the calendar at 13:00.',f'{d} agenda ({z}): the open reminder {y} is due 11:00, and the calendar event {x} starts at 13:00.',f'On {d}, {x} is on the calendar at 13:00 in {z}. The open reminder {y} is due that date at 11:00 in {z}.')
    elif f == 'mail_calendar_disagreement':
        title=p+' session';body='The session starts at 16:00.'
        R('calendar',items=[I('calendar','c1',title=title,date=d,start='15:00',timezone=z)])
        R('email',items=[I('email','e1',subject=title,sender=a,date=d,body=body,timezone=z)])
        S(f'For {title} on {d} ({z}), the calendar says 15:00, while {a}’s email says “{body}”',f'The {d} sources differ for {title}: calendar 15:00 in {z}; email from {a}, “{body}” in {z}.',f'{title} is listed on the calendar at 15:00 on {d} in {z}, but {a}’s email dated {d} states, “{body}” ({z}).')
        S('The supplied results do not establish which time is authoritative.','The correct time remains unresolved in the fixture.','No authoritative time is identified here.')
    elif f == 'note_message_proposal':
        title=p+' decision';body='Use the green cover.';msg='Could we try the blue cover instead?'
        R('notes',items=[I('notes','n1',title=title,date=d,text=body,status='confirmed')])
        R('messages',items=[I('messages','m1',speaker=b,date=s.nextday,text=msg,status='proposed')])
        S(f'The note {title} on {d} is confirmed: “{body}” On {s.nextday}, {b}’s message offered a proposed change: “{msg}”',f'The confirmed note {title} ({d}) says “{body}” {b}’s {s.nextday} message, “{msg}”, is proposed.',f'A confirmed choice appears in the note {title}, dated {d}: “{body}” The message from {b} on {s.nextday} is only proposed: “{msg}”')
    elif f == 'five_source_partial':
        x,y=p+' sync',p+' figures'
        R('calendar',items=[I('calendar','c1',title=x,date=d,start='09:30',timezone=z)])
        R('reminders','partial',[I('reminders','r1',title=y,state='open')],returned_lists=1,requested_lists=2)
        R('email','denied');R('messages','timeout');R('notes','missing')
        S(f'The calendar lists {x} at 09:30 on {d} ({z}); {y} is an open reminder.',f'The calendar shows {x} on {d} at 09:30 in {z}; the reminder {y} is open.',f'The calendar shows {x} at 09:30 in {z} on {d}. The reminder {y} is open.')
        S('Reminder coverage is partial (1 of 2 lists). Email is denied, messages timeout, and notes missing.', 'Only 1 of 2 reminder lists returned: partial coverage. Other source statuses: email denied, messages timeout, notes missing.', 'Limits: partial reminders, 1 of 2 lists; email denied; messages timeout; notes missing.');s.must('partial','1 of 2','denied','timeout','missing')
    elif f == 'empty_and_denied':
        R('email',date=d,total_count=0);R('notes','denied',requested_date=d)
        S(f'For {d}, email returned 0 items with complete coverage. Notes access was denied, so their contents are unknown.',f'The complete email result for {d} has 0 items; notes were denied and cannot be summarized.',f'Email is empty for {d}: 0 items, complete coverage. Notes are unavailable because access was denied.');s.must(d,'0 items','complete','denied')
    elif f == 'three_source_preparation':
        title=p+' review';body='Please bring the sample board.';note='Checklist: sample board and ruler.'
        R('calendar',items=[I('calendar','c1',title=title,date=d,start='14:30',timezone=z)])
        R('email',items=[I('email','e1',subject=title,sender=a,date=d,body=body)])
        R('notes',items=[I('notes','n1',title=title,date=d,text=note)])
        S(f'The calendar lists {title} on {d} at 14:30 ({z}). {a}’s email dated {d} says “{body}”',f'For {title} on {d}, the calendar lists 14:30 in {z}, and {a}’s email says “{body}”',f'The {d} calendar entry {title} starts at 14:30 in {z}; {a}’s email on {d} says, “{body}”')
        S(f'The {d} note {title} adds “{note}”',f'{title}’s note on {d} reads “{note}”',f'The note titled {title} ({d}) records “{note}”')
    elif f == 'zone_handover_with_task':
        title=p+' handover';task=p+' export'
        R('calendar',items=[I('calendar','c1',title=title,start_date=d,start='23:30',start_timezone='Asia/Kolkata',other_date=s.nextday,other_time='03:00',other_timezone='Asia/Tokyo')])
        R('reminders',items=[I('reminders','r1',title=task,state='open',due_date=d,due_time='21:00',timezone='Asia/Kolkata')])
        S(f'The calendar lists {title} as {d} at 23:30 Asia/Kolkata, also {s.nextday} at 03:00 Asia/Tokyo.',f'The calendar timestamps for {title} are {d}, 23:30 Asia/Kolkata, and {s.nextday}, 03:00 Asia/Tokyo.',f'Calendar entry {title}: {d} 23:30 (Asia/Kolkata), with the other timestamp {s.nextday} 03:00 (Asia/Tokyo).')
        S(f'The reminder {task} is open and due {d} at 21:00 Asia/Kolkata.',f'The open reminder {task} is due 21:00 Asia/Kolkata on {d}.',f'Reminder {task} remains open, due {d}, 21:00 in Asia/Kolkata.')
    elif f == 'scattered_week':
        third=(dt.date.fromisoformat(d)+dt.timedelta(days=4)).isoformat();x,y=p+' kickoff',p+' demo'
        R('calendar','truncated',items=[I('calendar','c1',title=x,date=d,start='10:15',timezone=z),I('calendar','c2',title=y,date=third,start='16:45',timezone=z)],start=d,end=s.end,total_count=5,limit=2)
        S(f'For {d}–{s.end} in {z}, the returned events are {x} on {d} at 10:15 and {y} on {third} at 16:45.',f'Visible from {d} through {s.end}: {x} ({d}, 10:15) and {y} ({third}, 16:45), both in {z}.',f'The {d} to {s.end} calendar result lists {x}, {d} at 10:15, and {y}, {third} at 16:45 ({z}).')
        S('The result is truncated to 2 of 5 events.','Coverage is truncated: 2 of 5 events are visible.','Only 2 of 5 events are shown in this truncated result.');s.must(s.end,'2 of 5','truncated')
    elif f == 'canceled_and_active':
        x,y=p+' rehearsal',p+' presentation'
        R('calendar',items=[I('calendar','c1',title=x,date=d,start='11:00',timezone=z,status='canceled'),I('calendar','c2',title=y,date=d,start='13:30',timezone=z,status='confirmed')])
        S(f'On {d} ({z}), {x} at 11:00 is canceled; {y} at 13:30 is confirmed.',f'{x} is canceled for {d}, 11:00 in {z}. {y} remains confirmed at 13:30 that date in {z}.',f'The {d} calendar statuses are canceled for {x} (11:00) and confirmed for {y} (13:30), in {z}.')
    elif f == 'reminder_dependency':
        x,y=p+' measurements',p+' order';dep='Wait for measurements before placing the order.'
        R('reminders','partial',items=[I('reminders','r1',title=x,state='open',due_status='no due date'),I('reminders','r2',title=y,state='blocked',dependency=dep,due_status='no due date')],returned_lists=1,requested_lists=2)
        S(f'{x} is open. {y} is blocked: {dep} Both have no due date.',f'The open task is {x}; {y} is blocked with this dependency: {dep} Each has no due date.',f'{y} is blocked by the stated dependency, “{dep}” {x} is open, and both tasks have no due date.')
        S('Coverage is partial: 1 of 2 reminder lists returned.','Only 1 of 2 reminder lists returned; coverage is partial.','The source is partial, covering 1 of 2 reminder lists.');s.must('partial','1 of 2')
    elif f == 'mail_revision_sequence':
        x,y=p+' estimate',p+' estimate revised';old='Estimate: 12 units.';new='Revised estimate: 9 units.'
        R('email',items=[I('email','e1',subject=x,sender=a,date=d,body=old),I('email','e2',subject=y,sender=b,date=s.nextday,body=new)])
        S(f'{a}’s {x} on {d} says “{old}” The later {y} from {b} on {s.nextday} says “{new}”',f'The latest estimate is in {y}, from {b} on {s.nextday}: “{new}” Earlier, {a} wrote {x} on {d}: “{old}”',f'{x} ({a}, {d}) states “{old}” Its later revision, {y} ({b}, {s.nextday}), states “{new}”')
    elif f == 'three_speaker_roles':
        m1='I can bring the easel.';m2='I will bring the paper.';m3='Who can bring the clips?'
        R('messages',items=[I('messages','m1',speaker=a,date=d,text=m1),I('messages','m2',speaker=b,date=d,text=m2),I('messages','m3',speaker=c,date=d,text=m3)])
        S(f'On {d}, {a} offered, “{m1}” {b} committed, “{m2}” {c} asked, “{m3}”',f'{d} messages: {a}, “{m1}” {b}, “{m2}” {c}, “{m3}”',f'{a}’s {d} message is “{m1}” {b} says “{m2}” and {c} asks “{m3}”')
        S('No clip provider is named.','The clips remain unassigned in these messages.','The supplied exchange does not identify who will bring clips.')
    elif f == 'note_decision_history':
        title=p+' materials';proposal='Proposal: use fabric.';decision='Decision: use recycled paper.'
        R('notes',items=[I('notes','n1',title=title,date=d,proposal=proposal,decision=decision)])
        S(f'{title} ({d}) records “{proposal}” followed by “{decision}”',f'The {d} note {title} considered “{proposal}” and settled on “{decision}”',f'In {title}, dated {d}, the planning history is “{proposal}” then “{decision}”')
    elif f == 'travel_draft_overview':
        event=p+' train';draft=p+' arrival reply';note=p+' bag list';body='I expect to arrive after 18:00.';items='Pack the charger and sketchbook.'
        R('calendar',items=[I('calendar','c1',title=event,date=d,start='15:10',timezone=z,status='confirmed')])
        R('email',items=[I('email','e1',subject=draft,date=d,recipient=a,status='draft',body=body)])
        R('notes',items=[I('notes','n1',title=note,date=d,text=items)])
        S(f'The calendar entry {event} is confirmed for {d} at 15:10 in {z}. The email {draft} to {a}, dated {d}, is a draft: “{body}”',f'For {d}, the calendar lists {event} as confirmed at 15:10 ({z}); the email {draft} to {a} remains a draft dated {d}, saying “{body}”',f'The calendar lists {event} as confirmed, {d} at 15:10 in {z}. The email {draft}, addressed to {a} on {d}, has status draft: “{body}”')
        S(f'The note {note} ({d}) says “{items}”',f'The note {note} on {d} records “{items}”',f'Packing note {note}, dated {d}: “{items}”')
    elif f == 'five_source_availability':
        R('calendar',date=d,total_count=0);R('reminders',total_count=0);R('email','missing');R('messages','denied');R('notes','timeout')
        S(f'Calendar coverage for {d} is complete with 0 events; reminders are complete with 0 items.',f'The complete results show 0 calendar events for {d} and 0 reminder items.',f'For {d}, calendar returned 0 events. Reminders returned 0 items; both results have complete coverage.')
        S('Email is missing, messages denied, and notes timeout; those sources do not establish empty results.', 'Unavailable sources: email missing, messages denied, notes timeout. Their item counts are unknown.', 'Email reported missing, messages denied, and notes timeout, so their contents are unavailable.');s.must(d,'complete','0 events' if s.surface!=1 else '0 calendar events','0 items' if s.surface!=1 else '0 reminder items','missing','denied','timeout')
    elif f == 'calendar_note_unlinked':
        title=p+' gallery visit';note=p+' reflection';body='Consider a quieter color palette.'
        R('calendar',items=[I('calendar','c1',title=title,date=d,start='12:20',timezone=z)])
        R('notes',items=[I('notes','n1',title=note,date=d,text=body)])
        S(f'{title} is on the calendar for {d} at 12:20 ({z}). The note {note}, dated {d}, says “{body}”',f'The calendar lists {title} for {d}, 12:20 in {z}; {note} on {d} records “{body}”',f'The calendar for {d} includes {title} at 12:20 in {z}. In the separate note {note} ({d}): “{body}”')
        S('The fixture does not link the note to the visit.','No relationship between the note and event is supplied.','These results do not establish that the note concerns the visit.')
    else:
        raise AssertionError(f)
    return s


def compact(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def build(split, families, variants):
    rows, provenance = [], []
    for family in families:
        for n in range(variants):
            s = author(Scene(split, family, n))
            row_id = f'v2-grounded-{split}-{len(rows)+1:04d}'
            fixture = {'synthetic': True, 'results': s.results}
            prompt = REQUESTS[family][s.surface].format(day=s.day)
            answer = s.summary()
            phrases = list(dict.fromkeys(s.phrases))
            missing = [p for p in phrases if p not in answer]
            assert not missing, (row_id, family, missing, answer)
            forbidden = ['I sent the', 'I created the', 'I deleted the', 'I checked your',
                         'I accessed your', 'Everything is complete', 'No other tasks exist']
            if family in {'draft_and_sent','travel_draft_overview'}:
                forbidden += ['The draft was sent', 'I sent the draft', 'Both emails were sent']
            if family in {'proposed_meeting','note_message_proposal'}:
                forbidden += ['The proposal is confirmed', 'The meeting is confirmed', 'The change was approved']
            if family in {'conflicting_notes','mail_calendar_disagreement'}:
                forbidden += ['The conflict is resolved', 'The final venue is', 'The authoritative time is']
            if family in {'truncated_inbox','truncated_note','partial_reminders','five_source_partial','scattered_week','reminder_dependency'}:
                forbidden += ['All results are shown', 'Full coverage is available', 'Nothing else exists']
            if family in {'undated_reminder','reminder_dependency','note_tasks'}:
                forbidden += ['Due tomorrow', 'Due today', 'Finish urgently']
            if family == 'calendar_note_unlinked':
                forbidden += ['The note is for the visit', 'The note confirms the visit']
            for result in s.results:
                if result['status'] in {'denied','timeout','missing'}:
                    source = result['source']
                    forbidden += [source + ' is empty', source + ' returned no matches',
                                  'There are no ' + source + ' items']
            assert not any(p in answer for p in forbidden)
            user = prompt + '\n\nSynthetic source results:\n' + compact(fixture)
            assert len(user) < 6000, (row_id, len(user))
            assert len(answer) < 1700, (row_id, len(answer))  # Character bound; NOT a tokenizer audit.
            rows.append({'id': row_id, 'messages': [
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': user},
                {'role': 'assistant', 'content': answer, 'training': True},
            ]})
            provenance.append({
                'id': row_id, 'split': split, 'task': 'grounded', 'primary_group': 'grounded',
                'category': 'fixture_overview', 'family_id': family,
                'scenario_id': f'{family}-scene-{n:02d}',
                'template_id': f'{family}-surface-{s.surface}',
                'synthetic_slots': {'date': s.day, 'project': s.project, 'speakers': [s.a,s.b,s.c], 'timezone': s.zone},
                'contrastive_axis': None, 'fixture': fixture,
                'rubric': {'required_facts': s.facts,
                           'source_statuses': {r['source']:r['status'] for r in s.results},
                           'required_phrases': phrases, 'forbidden_phrases': forbidden},
            })
    return rows, provenance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    # Read only frozen contracts via source location. They are not grounding inputs.
    root = Path(__file__).resolve().parents[2]
    spec = (root/'SPEC.md').read_text()
    schema = json.loads((root/'intent.schema.v1.json').read_text())
    router_system = (root/'router-system.txt').read_text()
    assert 'Grounded system instruction' in spec
    assert schema['properties']['version']['enum'] == [1]
    assert router_system and SYSTEM != router_system
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    for split, families, variants in [('train',TRAIN_FAMILIES,25),('dev',DEV_FAMILIES,6)]:
        rows, prov = build(split, families, variants)
        for name, objects in [(f'{split}.jsonl',rows),(f'{split}.provenance.jsonl',prov)]:
            payload = ''.join(compact(obj)+'\n' for obj in objects)
            (args.output_dir/name).write_text(payload, encoding='utf-8')
            stats[name] = {'rows':len(objects),'sha256':hashlib.sha256(payload.encode()).hexdigest()}
    print(compact({'seed':SEED,'files':stats,'train_families':len(TRAIN_FAMILIES),'dev_families':len(DEV_FAMILIES)}))

if __name__ == '__main__':
    main()
