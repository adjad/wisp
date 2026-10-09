#!/usr/bin/env python3
"""Fresh, deterministic context/exclusions routing examples; stdlib only."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

SEED = 610220261006 + 202
SURFACES = [
    'Please help with this: ', 'Here is my request: ', 'For this task: ',
    'I would like the following: ', 'Could you handle this request: ',
    'My request is: ', 'Please interpret the following: ', 'I need this: ',
    'For my next request: ', 'Can you help me with this: ',
    'This is what I want: ', 'Please use this instruction: ',
    'I have this request: ', 'What I am asking for is: ',
    'For this lookup task: ', 'Please take this as my request: ',
    'I would appreciate help with this: ', 'The request I have is: ',
    'Please work from this instruction: ', 'To clarify what I want: ',
]
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SYSTEM = (ROOT / 'router-system.txt').read_text(encoding='utf-8')
SCHEMA = json.loads((ROOT / 'intent.schema.v1.json').read_text(encoding='utf-8'))

# The scene partition is declared before slot or surface expansion. The dev
# dialogues have different compositions and wording, not held-out slot values.
TRAIN_FOLLOWUP = [
 'archive_inbox_to_research_notes', 'club_chat_to_rehearsal_calendar',
 'work_calendar_to_inbox_digest', 'field_notes_to_errand_reminders',
 'packing_reminders_to_family_chat', 'unread_inbox_to_project_messages',
 'bounded_chat_search_to_mail_search', 'vendor_calendar_to_design_notes',
 'reading_notes_to_free_time', 'overdue_chores_to_mail_digest',
 'limited_inbox_to_reminder_search', 'weekend_chat_to_plain_notes',
 'include_read_mail_preserve_mail_context', 'remove_mail_account_preserve_other_filters',
 'unlimit_notes_preserve_search_and_period', 'remove_chat_name_preserve_period_and_limit',
 'replace_inbox_range_preserve_other_filters', 'rename_calendar_search_preserve_account_and_day',
 'change_reminder_scope_preserve_search', 'replace_note_search_preserve_period_and_limit',
]
DEV_FOLLOWUP = [
 'festival_mail_to_open_calendar', 'travel_calendar_to_photo_notes',
 'maintenance_notes_to_reminder_digest', 'museum_reminders_to_message_search',
 'remove_mail_unread_keep_range_account_limit', 'replace_chat_day_keep_group_limit',
 'remove_note_period_keep_search_limit', 'replace_reminder_search_keep_tomorrow',
]
TRAIN_MULTI = [
 'inbox_day_calendar_week', 'chat_search_note_search', 'errands_and_mail_exclude_notes',
 'today_personal_agenda', 'tomorrow_personal_agenda', 'week_personal_agenda',
 'month_personal_agenda', 'calendar_reminders_prohibit_mail_and_chat',
 'mail_chat_separate_limits', 'notes_calendar_account_binding',
 'mail_notes_distinct_literal_searches', 'chat_digest_and_reminder_scope',
 'three_source_work_digest', 'note_search_with_entity_exclusion',
 'free_slot_and_reference_notes', 'mail_digest_and_overdue_reminders',
 'day_agenda_plus_notebook_lookup', 'calendar_only_explicit_reminder_prohibition',
]
DEV_MULTI = [
 'townhall_mail_and_zoning_notes', 'night_shift_chat_and_next_day_calendar',
 'weekly_agenda_with_recipe_notes', 'upcoming_checks_and_archived_mail',
 'historical_mail_range_and_calendar_month', 'literal_message_query_and_note_lookup',
 'matched_calendar_and_reminder_searches', 'tomorrow_agenda_exclude_web_and_mail',
 'notes_mail_with_negative_person_constraint',
]
TRAIN_BOUNDARY = [
 'pure_source_prohibition', 'public_email_explanation', 'ordinary_question_with_chat_prohibition',
 'reply_wording_here', 'invitation_wording_here', 'send_or_draft_reply',
 'delete_or_find_note', 'save_or_draft_note', 'create_or_outline_calendar_event',
 'send_effect_plus_inbox_read', 'delete_effect_plus_note_read',
 'literal_action_words_email_search', 'literal_rule_words_note_search',
 'message_unread_constraint', 'calendar_negative_person_constraint',
 'reminder_completion_constraint',
]
DEV_BOUNDARY = [
 'public_delivery_protocol_question', 'apology_draft_or_send', 'calendar_reminder_prohibition_only',
 'save_memo_plus_reminder_read', 'literal_delete_message_search',
 'message_read_status_constraint', 'calendar_location_constraint',
 'mail_prohibition_with_general_time_management_question',
]


def dumps(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def source(domain, operation='overview', **filters):
    return {'domain': domain, 'operation': operation, 'filters': filters}


def semantic(requests=(), kind='read', excluded=(), unsupported=()):
    return {'kind': kind, 'requests': list(requests), 'excluded': list(excluded), 'unsupported': list(unsupported)}


def target(s):
    return {'version': 1, 'kind': s['kind'], 'sources': [dict(domain=r['domain'], operation=r['operation'], **r['filters']) for r in s['requests']], 'excluded_sources': s['excluded'], 'unsupported_constraints': s['unsupported']}


def ev(field, value, span, index=1):
    return {'field': field, 'value': value, 'message_index': index, 'span': span}


def filter_evidence(req, spans, index=1, number=0):
    out = []
    for key, value in req['filters'].items():
        out.append(ev(f'sources[{number}].{key}', value, spans[key], index))
    return out


def slot(split, family, n):
    # Pair members share every scene slot. Dates are explicit, not inferred.
    k = n // 2
    year = 2029 if split == 'train' else 2030
    day = 1 + k % 20
    account = f'{family.replace("_", "-")}-{k}@mail.invalid'
    topics = ['ceramic glaze', 'river survey', 'stage cables', 'garden plan', 'bike lights', 'studio booking', 'water samples', 'paper lanterns']
    names = ['Mira Vale', 'Owen Reed', 'Tara Finch', 'Inez Brook', 'Leo Moss', 'Nora Wells', 'Arun Pike', 'Sana Frost']
    topic = f'{topics[(k + SEED) % len(topics)]} {k + 1}'
    return {'k': k, 'date': f'{year}-04-{day:02}', 'date2': f'{year}-05-{day:02}', 'start': f'{year}-03-{day:02}', 'end': f'{year}-03-{day + 3:02}', 'start2': f'{year}-06-{day:02}', 'end2': f'{year}-06-{day + 3:02}', 'month': f'{year}-07', 'account': account, 'account2': '+backup-' + account, 'q': topic, 'q2': f'{topic}: revised?', 'person': names[(k + SEED) % len(names)], 'group': f'{names[(k + SEED) % len(names)]} workshop', 'count': 3 + k % 12, 'newcount': 20 + k % 12, 'minutes': 25 + 5 * (k % 5), 'lead': SURFACES[k % len(SURFACES)]}


def followup(scene, split, n):
    z = slot(split, scene, n)
    q, q2, a, a2, c, c2, d, d2 = [z[k] for k in ('q', 'q2', 'account', 'account2', 'count', 'newcount', 'date', 'date2')]
    group, person, month = z['group'], z['person'], z['month']
    tday, tweek = {'date': d}, {'named': 'this week'}
    trange = {'start': z['start'], 'end': z['end']}
    trange2 = {'start': z['start2'], 'end': z['end2']}
    b = n % 2
    removed = {}
    edits = {}
    axis = 'explicit_new_filter'
    if scene == TRAIN_FOLLOWUP[0]:
        initial = source('email', time=tday, account=a, unread=True, count=c)
        first = f'Summarize my unread email on {d} in account {a}, at most {c} messages.'
        spans = dict(time=d, account=a, unread='unread', count=str(c))
        latest = f'Correction: switch to notes and search for "{q}".' + (f' Limit the notes to {c2}.' if b else '')
        final = source('notes', 'records', query=q, **({'count': c2} if b else {}))
        now = dict(query=q, count=str(c2))
    elif scene == TRAIN_FOLLOWUP[1]:
        initial = source('messages', conversation=group, time=tweek, count=c)
        first = f'Catch me up on messages in {group} this week, with {c} items.'
        spans = dict(conversation=group, time='this week', count=str(c))
        latest = 'I meant the calendar instead: give me its overview.' + (f' Use {d} for the calendar.' if b else '')
        final = source('calendar', **({'time': tday} if b else {}))
        now = dict(time=d)
    elif scene == TRAIN_FOLLOWUP[2]:
        initial = source('calendar', 'records', query=q, account=a, time={'month': month})
        first = f'Find calendar entries containing "{q}" in account {a} during {month}.'
        spans = dict(query=q, account=a, time=month)
        latest = 'Actually switch to an email overview.' + (f' For email, use account {a2}.' if b else '')
        final = source('email', **({'account': a2} if b else {}))
        now = dict(account=a2)
    elif scene == TRAIN_FOLLOWUP[3]:
        initial = source('notes', 'records', query=q, time={'month': month}, count=c)
        first = f'Find {c} notes containing "{q}" from {month}.'
        spans = dict(query=q, time=month, count=str(c))
        latest = 'Change the source to reminders and summarize them.' + (' Restrict reminders to overdue.' if b else '')
        final = source('reminders', **({'scope': 'overdue'} if b else {}))
        now = dict(scope='overdue')
    elif scene == TRAIN_FOLLOWUP[4]:
        initial = source('reminders', 'records', query=q, scope='tomorrow')
        first = f'Look up reminders matching "{q}" for tomorrow.'
        spans = dict(query=q, scope='tomorrow')
        latest = 'Use messages instead, as a digest.' + (f' In messages, use the conversation {group}.' if b else '')
        final = source('messages', **({'conversation': group} if b else {}))
        now = dict(conversation=group)
    elif scene == TRAIN_FOLLOWUP[5]:
        initial = source('email', account=a, unread=True, time=tday, count=c)
        first = f'Give me {c} unread email highlights for {d} from account {a}.'
        spans = dict(account=a, unread='unread', time=d, count=str(c))
        latest = f'Switch the lookup to messages; search literally for "{q}".' + (f' Return {c2} matching messages.' if b else '')
        final = source('messages', 'records', query=q, **({'count': c2} if b else {}))
        now = dict(query=q, count=str(c2))
    elif scene == TRAIN_FOLLOWUP[6]:
        initial = source('messages', 'records', query=q, time=trange, count=c)
        first = f'Find {c} messages containing "{q}" from {z["start"]} through {z["end"]}, inclusive.'
        spans = dict(query=q, time=f'{z["start"]} through {z["end"]}', count=str(c))
        latest = f'No, search email instead for "{q2}".' + (f' Use account {a2} for the email search.' if b else '')
        final = source('email', 'records', query=q2, **({'account': a2} if b else {}))
        now = dict(query=q2, account=a2)
    elif scene == TRAIN_FOLLOWUP[7]:
        initial = source('calendar', 'records', query=q, time=tday, account=a)
        first = f'Search my calendar for "{q}" on {d} in account {a}.'
        spans = dict(query=q, time=d, account=a)
        latest = f'Correction: search notes for "{q2}" instead.' + (f' For notes, keep only {c2} results.' if b else '')
        final = source('notes', 'records', query=q2, **({'count': c2} if b else {}))
        now = dict(query=q2, count=str(c2))
    elif scene == TRAIN_FOLLOWUP[8]:
        initial = source('notes', 'records', query=q, count=c, time={'rolling_days': 9})
        first = f'Look up {c} notes with "{q}" from the rolling 9-day window.'
        spans = dict(query=q, count=str(c), time='rolling 9-day window')
        latest = f'I meant calendar availability: find free time on {d}.' + (f' I need a {z["minutes"]}-minute opening.' if b else '')
        final = source('calendar', 'free_time', time=tday, **({'minutes': z['minutes']} if b else {}))
        now = dict(time=d, minutes=f'{z["minutes"]}-minute')
    elif scene == TRAIN_FOLLOWUP[9]:
        initial = source('reminders', 'records', query=q, scope='overdue')
        first = f'Find overdue reminders containing "{q}".'
        spans = dict(query=q, scope='overdue')
        latest = f'Switch to email; summarize email on {d}.' + (' Only unread email.' if b else '')
        final = source('email', time=tday, **({'unread': True} if b else {}))
        now = dict(time=d, unread='unread')
    elif scene == TRAIN_FOLLOWUP[10]:
        initial = source('email', count=c, account=a, unread=False)
        first = f'Summarize {c} emails in account {a}, including both already-read and unread mail.'
        spans = dict(count=str(c), account=a, unread='including both already-read and unread')
        latest = f'Change that to a reminder search for "{q}".' + (' Search all reminder scopes.' if b else '')
        final = source('reminders', 'records', query=q, **({'scope': 'all'} if b else {}))
        now = dict(query=q, scope='all')
    elif scene == TRAIN_FOLLOWUP[11]:
        initial = source('messages', time={'named': 'this weekend'}, conversation=group, count=c)
        first = f'Summarize {c} messages in {group} from this weekend.'
        spans = dict(time='this weekend', conversation=group, count=str(c))
        latest = 'I meant a general overview of my notes instead.' + (' Use notes from yesterday.' if b else '')
        final = source('notes', **({'time': {'named': 'yesterday'}} if b else {}))
        now = dict(time='yesterday')
    elif scene == TRAIN_FOLLOWUP[12]:
        initial = source('email', account=a, unread=True, time=tday, count=c)
        first = f'Summarize {c} unread emails from {d} in account {a}.'
        spans = dict(account=a, unread='unread', time=d, count=str(c))
        latest = 'Keep that email request, but ' + ('include both already-read and unread mail.' if b else 'remove the unread filter entirely.')
        final = copy.deepcopy(initial)
        if b:
            final['filters']['unread'] = False
            edits = {'unread': False}; now = dict(unread='include both already-read and unread mail')
        else:
            del final['filters']['unread']; removed = {'unread': True}; now = {}
        axis = 'explicit_false_versus_removed'
    elif scene == TRAIN_FOLLOWUP[13]:
        initial = source('email', account=a, unread=True, time=tday, count=c)
        first = f'Email digest: unread mail on {d}, account {a}, no more than {c}.'
        spans = dict(account=a, unread='unread', time=d, count=str(c))
        latest = 'Keep the email digest and its other filters, but ' + (f'change the account to {a2}.' if b else 'remove the account filter.')
        final = copy.deepcopy(initial); now = dict(account=a2)
        if b: final['filters']['account'] = a2; edits = {'account': a2}
        else: del final['filters']['account']; removed = {'account': a}
        axis = 'replace_versus_remove_account'
    elif scene == TRAIN_FOLLOWUP[14]:
        initial = source('notes', 'records', query=q, time=tweek, count=c)
        first = f'Search for "{q}" in notes from this week; return {c}.'
        spans = dict(query=q, time='this week', count=str(c))
        latest = 'For the same note search, preserve the phrase and week; ' + (f'change the limit to {c2}.' if b else 'remove the result limit.')
        final = copy.deepcopy(initial); now = dict(count=str(c2))
        if b: final['filters']['count'] = c2; edits = {'count': c2}
        else: del final['filters']['count']; removed = {'count': c}
        axis = 'replace_versus_remove_count'
    elif scene == TRAIN_FOLLOWUP[15]:
        initial = source('messages', conversation=group, time=tday, count=c)
        first = f'Summarize {c} messages in {group} on {d}.'
        spans = dict(conversation=group, time=d, count=str(c))
        latest = 'Keep the message digest date and limit; ' + (f'change the conversation to {person}.' if b else 'remove the named conversation filter.')
        final = copy.deepcopy(initial); now = dict(conversation=person)
        if b: final['filters']['conversation'] = person; edits = {'conversation': person}
        else: del final['filters']['conversation']; removed = {'conversation': group}
        axis = 'replace_versus_remove_conversation'
    elif scene == TRAIN_FOLLOWUP[16]:
        initial = source('email', account=a, unread=True, time=trange, count=c)
        first = f'Give me {c} unread email highlights in account {a} between {z["start"]} and {z["end"]}, inclusive.'
        spans = dict(account=a, unread='unread', time=f'{z["start"]} and {z["end"]}', count=str(c))
        latest = 'For that same email digest, replace only its date range with ' + (f'{z["start2"]} through {z["end2"]}, inclusive.' if b else f'{d2} only.')
        final = copy.deepcopy(initial); v = trange2 if b else {'date': d2}; final['filters']['time'] = v; edits = {'time': v}
        now = dict(time=f'{z["start2"]} through {z["end2"]}' if b else d2); axis = 'range_versus_exact_day'
    elif scene == TRAIN_FOLLOWUP[17]:
        initial = source('calendar', 'records', query=q, account=a, time=tday)
        first = f'Find calendar records for "{q}" on {d} in account {a}.'
        spans = dict(query=q, account=a, time=d)
        latest = f'For the same calendar search, change only the literal search to "{q2 if b else person}".'
        final = copy.deepcopy(initial); final['filters']['query'] = q2 if b else person; edits = {'query': q2 if b else person}; now = dict(query=q2 if b else person); axis = 'query_replacement'
    elif scene == TRAIN_FOLLOWUP[18]:
        initial = source('reminders', 'records', query=q, scope='today')
        first = f'Find today\'s reminders matching "{q}".'
        spans = dict(query=q, scope='today')
        value = 'tomorrow' if b else 'upcoming'
        latest = f'Keep that reminder search phrase; change only its scope to {value}.'
        final = copy.deepcopy(initial); final['filters']['scope'] = value; edits = {'scope': value}; now = dict(scope=value); axis = 'reminder_scope_replacement'
    elif scene == TRAIN_FOLLOWUP[19]:
        initial = source('notes', 'records', query=q, time={'month': month}, count=c)
        first = f'Get {c} notes with "{q}" from {month}.'
        spans = dict(query=q, time=month, count=str(c))
        latest = f'Keep the note month and limit, but search for "{q2 if b else person}" instead.'
        final = copy.deepcopy(initial); final['filters']['query'] = q2 if b else person; edits = {'query': q2 if b else person}; now = dict(query=q2 if b else person); axis = 'query_replacement'
    elif scene == DEV_FOLLOWUP[0]:
        initial = source('email', 'records', query=q, time={'named': 'last week'}, unread=True, count=c, account=a)
        first = f'For the festival archive, find {c} unread emails about "{q}" from last week using {a} as my mail account.'
        spans = dict(query=q, time='last week', unread='unread', count=str(c), account=a)
        latest = 'Wrong source: please give a calendar summary instead.' + (' Limit the calendar period to next weekend.' if b else '')
        final = source('calendar', **({'time': {'named': 'next weekend'}} if b else {})); now = dict(time='next weekend')
    elif scene == DEV_FOLLOWUP[1]:
        initial = source('calendar', 'records', query=q, account=a, time=trange)
        first = f'For my travel schedule, look up "{q}" on calendar account {a}, from {z["start"]} to {z["end"]}, both days included.'
        spans = dict(query=q, account=a, time=f'{z["start"]} to {z["end"]}')
        latest = f'That source was a mistake. Look in notes for "{q2}" instead.' + (f' Restrict the notes to {d2}.' if b else '')
        final = source('notes', 'records', query=q2, **({'time': {'date': d2}} if b else {})); now = dict(query=q2, time=d2)
    elif scene == DEV_FOLLOWUP[2]:
        initial = source('notes', 'records', query=q, count=c, time={'last_n_days': 6})
        first = f'Find {c} maintenance notes with "{q}" from the last 6 days.'
        spans = dict(query=q, count=str(c), time='last 6 days')
        latest = 'Correction to the source: summarize reminders instead.' + (' Show only today\'s reminders.' if b else '')
        final = source('reminders', **({'scope': 'today'} if b else {})); now = dict(scope='today')
    elif scene == DEV_FOLLOWUP[3]:
        initial = source('reminders', 'records', query=q, scope='upcoming')
        first = f'I need upcoming reminders whose text matches "{q}" for the museum visit.'
        spans = dict(query=q, scope='upcoming')
        latest = f'Use a message search instead: look for the exact text "{q2}".' + (f' Cap message results at {c2}.' if b else '')
        final = source('messages', 'records', query=q2, **({'count': c2} if b else {})); now = dict(query=q2, count=str(c2))
    elif scene == DEV_FOLLOWUP[4]:
        initial = source('email', account=a, unread=True, count=c, time=trange)
        first = f'For the equipment audit, make a digest of {c} unread emails in {a}, my email account, covering {z["start"]} through {z["end"]} inclusive.'
        spans = dict(account=a, unread='unread', count=str(c), time=f'{z["start"]} through {z["end"]}')
        latest = 'Continue the same email digest with its account, range, and cap; ' + ('include both already-read and unread email.' if b else 'drop its unread constraint.')
        final = copy.deepcopy(initial)
        if b: final['filters']['unread'] = False; edits = {'unread': False}; now = dict(unread='include both already-read and unread email')
        else: del final['filters']['unread']; removed = {'unread': True}; now = {}
        axis = 'explicit_false_versus_removed'
    elif scene == DEV_FOLLOWUP[5]:
        initial = source('messages', conversation=group, count=c, time=tday)
        first = f'For the volunteer check-in, give highlights of {c} messages in {group}, dated {d}.'
        spans = dict(conversation=group, count=str(c), time=d)
        value = {'date': d2} if b else {'named': 'yesterday'}
        latest = f'Keep the same group and item cap, but use {d2 if b else "yesterday"} as the message period.'
        final = copy.deepcopy(initial); final['filters']['time'] = value; edits = {'time': value}; now = dict(time=d2 if b else 'yesterday'); axis = 'message_time_replacement'
    elif scene == DEV_FOLLOWUP[6]:
        initial = source('notes', 'records', query=q, count=c, time={'month': month})
        first = f'My recipe notebook lookup should find {c} notes containing "{q}" written in {month}.'
        spans = dict(query=q, count=str(c), time=month)
        latest = 'Keep that phrase and result count; ' + ('change the notes period to last month.' if b else 'remove the notes period filter.')
        final = copy.deepcopy(initial)
        if b: final['filters']['time'] = {'named': 'last month'}; edits = {'time': {'named': 'last month'}}; now = dict(time='last month')
        else: del final['filters']['time']; removed = {'time': {'month': month}}; now = {}
        axis = 'replace_versus_remove_time'
    elif scene == DEV_FOLLOWUP[7]:
        initial = source('reminders', 'records', query=q, scope='tomorrow')
        first = f'For tomorrow\'s cleanup, retrieve reminders containing "{q}".'
        spans = dict(query=q, scope='tomorrow')
        value = q2 if b else person
        latest = f'The scope stays tomorrow. Replace only the reminder search phrase with "{value}".'
        final = copy.deepcopy(initial); final['filters']['query'] = value; edits = {'query': value}; now = dict(query=value); axis = 'reminder_query_replacement'
    else:
        raise ValueError(scene)
    switch = initial['domain'] != final['domain']
    if switch:
        edits = copy.deepcopy(final['filters'])
        evidence = filter_evidence(final, now, 3)
        removed = copy.deepcopy(initial['filters'])
        retained = []
        transition = 'source_switch_reset_all_old_filters'
    else:
        evidence = []
        for field, value in final['filters'].items():
            use_new = field in edits
            evidence.append(ev(f'sources[0].{field}', value, (now if use_new else spans)[field], 3 if use_new else 1))
        retained = [field for field in final['filters'] if field not in edits]
        transition = 'same_source_followup'
    if not switch:
        # The literal removal phrase is the latest user's edit instruction.
        for field, value in removed.items():
            evidence.append(ev(f'removed:sources[0].{field}', value, latest, 3))
    history_edits = ([{'op': 'switch', 'domain': final['domain'], 'operation': final['operation'], 'filters': copy.deepcopy(final['filters'])}] if switch else ([{'op': 'remove', 'domain': initial['domain'], 'fields': list(removed)}] if removed else []) + ([{'op': 'replace', 'domain': initial['domain'], 'fields': copy.deepcopy(edits)}] if edits else []))
    history = {'initial': semantic([initial]), 'edits': history_edits, 'initial_evidence': filter_evidence(initial, spans), 'transition': transition, 'from_domain': initial['domain'], 'to_domain': final['domain'], 'retained_fields': retained, 'replaced_fields': edits, 'removed_fields': removed, 'reset_all_old_source_filters': switch}
    # Context never claims execution, never grants authority beyond user text.
    prior = [
        f'That wording describes a {initial["domain"]} read with the filters you named. No data has been accessed.',
        f'I would classify that as a request about {initial["domain"]}; this reply only interprets your wording.',
        'I can represent those filters as a read request; interpreting them does not access personal data.',
        f'The source named in your request is {initial["domain"]}. The filters can be represented without running a tool.',
        f'A handler label for that source could be {initial["domain"]}_search. A handler name itself grants no access.',
    ][z['k'] % 5]
    return {'users': [first, latest], 'prior': prior, 'semantic_spec': semantic([final]), 'evidence': evidence, 'history_spec': history, 'contrastive_axis': axis}


def multi(scene, split, n):
    z = slot(split, scene, n); b = n % 2
    q, q2, a, c, c2, d, month, group = [z[k] for k in ('q', 'q2', 'account', 'count', 'newcount', 'date', 'month', 'group')]
    reqs, binds, excluded, unsupported = [], [], [], []
    def add(domain, op='overview', filters=None, spans=None):
        reqs.append(source(domain, op, **(filters or {}))); binds.append(spans or {})
    axis = 'explicit_filter'
    if scene == TRAIN_MULTI[0]:
        text = f'Summarize email from {d} in account {a}; separately summarize my calendar for this week.' + (f' Cap email at {c}.' if b else '')
        add('email', filters=dict(time={'date': d}, account=a, **({'count': c} if b else {})), spans=dict(time=d, account=a, count=str(c)))
        add('calendar', filters=dict(time={'named': 'this week'}), spans=dict(time='this week'))
    elif scene == TRAIN_MULTI[1]:
        text = f'Find messages containing "{q}" and notes containing "{q2}".' + (f' The notes limit is {c}; the message search is unlimited.' if b else '')
        add('messages', 'records', dict(query=q), dict(query=q)); add('notes', 'records', dict(query=q2, **({'count': c} if b else {})), dict(query=q2, count=str(c)))
    elif scene == TRAIN_MULTI[2]:
        text = f'Give me overdue reminders and an email overview for {d}.' + (' Do not access notes.' if b else '')
        add('reminders', filters=dict(scope='overdue'), spans=dict(scope='overdue')); add('email', filters=dict(time={'date': d}), spans=dict(time=d))
        if b: excluded = ['notes']
        axis = 'explicit_source_exclusion'
    elif scene in TRAIN_MULTI[3:7]:
        period = {TRAIN_MULTI[3]:'today', TRAIN_MULTI[4]:'tomorrow', TRAIN_MULTI[5]:'this week', TRAIN_MULTI[6]:'this month'}[scene]
        text = f'Give me my personal agenda for {period}, including calendar events and reminders.' + (' Do not access email.' if b else '')
        add('calendar', filters=dict(time={'named': period}), spans=dict(time=period))
        if period in ('today', 'tomorrow'): add('reminders', filters=dict(scope=period), spans=dict(scope=period))
        else: add('reminders'); unsupported = [period]
        if b: excluded = ['email']
        axis = 'explicit_source_exclusion'
    elif scene == TRAIN_MULTI[7]:
        text = f'Overview of calendar entries on {d}, plus upcoming reminders. Do not access email or messages.' + (f' Use account {a} only for the calendar.' if b else '')
        add('calendar', filters=dict(time={'date': d}, **({'account': a} if b else {})), spans=dict(time=d, account=a)); add('reminders', filters=dict(scope='upcoming'), spans=dict(scope='upcoming')); excluded = ['email', 'messages']
    elif scene == TRAIN_MULTI[8]:
        text = f'Summarize {c} emails from yesterday, and {c2} messages in {group} from this week.' + (' Only the emails should be unread.' if b else '')
        add('email', filters=dict(count=c, time={'named':'yesterday'}, **({'unread':True} if b else {})), spans=dict(count=str(c),time='yesterday',unread='unread')); add('messages', filters=dict(count=c2, conversation=group,time={'named':'this week'}), spans=dict(count=str(c2),conversation=group,time='this week'))
    elif scene == TRAIN_MULTI[9]:
        text = f'Find notes containing "{q}"; give a calendar overview for {month} using calendar account {a}.' + (f' Limit the notes to {c}.' if b else '')
        add('notes','records',dict(query=q, **({'count':c} if b else {})),dict(query=q,count=str(c))); add('calendar',filters=dict(time={'month':month},account=a),spans=dict(time=month,account=a))
    elif scene == TRAIN_MULTI[10]:
        text = f'Search email for the literal subject "{q}" and notes for the literal phrase "{q2}".' + (f' For email only, use account {a}.' if b else '')
        add('email','records',dict(query=q,**({'account':a} if b else {})),dict(query=q,account=a)); add('notes','records',dict(query=q2),dict(query=q2))
    elif scene == TRAIN_MULTI[11]:
        text = f'Give a message digest in {group} for today and summarize reminders that are due tomorrow.' + (f' Include only {c} messages.' if b else '')
        add('messages',filters=dict(conversation=group,time={'named':'today'},**({'count':c} if b else {})),spans=dict(conversation=group,time='today',count=str(c))); add('reminders',filters=dict(scope='tomorrow'),spans=dict(scope='tomorrow'))
    elif scene == TRAIN_MULTI[12]:
        text = f'For my work review, summarize email in account {a}, messages in {group}, and notes from {d}.' + (' Do not access the calendar.' if b else '')
        add('email',filters=dict(account=a),spans=dict(account=a)); add('messages',filters=dict(conversation=group),spans=dict(conversation=group)); add('notes',filters=dict(time={'date':d}),spans=dict(time=d))
        if b: excluded=['calendar']
        axis='explicit_source_exclusion'
    elif scene == TRAIN_MULTI[13]:
        text = f'Find notes containing "{q}" and summarize calendar entries for tomorrow.' + (f' For notes, exclude items mentioning {z["person"]}.' if b else '')
        add('notes','records',dict(query=q),dict(query=q)); add('calendar',filters=dict(time={'named':'tomorrow'}),spans=dict(time='tomorrow'))
        if b: unsupported=[f'exclude items mentioning {z["person"]}']
        axis='negative_entity_constraint'
    elif scene == TRAIN_MULTI[14]:
        text = f'Find a {z["minutes"]}-minute opening in my calendar on {d}, and find notes containing "{q}".' + (f' Limit the note search to {c}.' if b else '')
        add('calendar','free_time',dict(minutes=z['minutes'],time={'date':d}),dict(minutes=f'{z["minutes"]}-minute',time=d)); add('notes','records',dict(query=q,**({'count':c} if b else {})),dict(query=q,count=str(c)))
    elif scene == TRAIN_MULTI[15]:
        text = f'Summarize unread emails for this month and overdue reminders matching "{q}".' + (f' Use account {a} for email.' if b else '')
        add('email',filters=dict(unread=True,time={'named':'this month'},**({'account':a} if b else {})),spans=dict(unread='unread',time='this month',account=a)); add('reminders','records',dict(scope='overdue',query=q),dict(scope='overdue',query=q))
    elif scene == TRAIN_MULTI[16]:
        text = f'Give my personal agenda for today, with calendar and reminders, and find notes containing "{q}".' + (' Exclude messages as a source.' if b else '')
        add('calendar',filters=dict(time={'named':'today'}),spans=dict(time='today')); add('reminders',filters=dict(scope='today'),spans=dict(scope='today')); add('notes','records',dict(query=q),dict(query=q))
        if b: excluded=['messages']
        axis='explicit_source_exclusion'
    elif scene == TRAIN_MULTI[17]:
        text = f'Summarize only my calendar for {d}.' + (' Do not access reminders.' if b else '')
        add('calendar',filters=dict(time={'date':d}),spans=dict(time=d))
        if b: excluded=['reminders']
        axis='explicit_source_exclusion'
    elif scene == DEV_MULTI[0]:
        text = f'For the town hall, retrieve email with subject "{q}" in mail account {a}, plus notes with the phrase "{q2}".' + (f' Cap the email lookup at {c}.' if b else '')
        add('email','records',dict(query=q,account=a,**({'count':c} if b else {})),dict(query=q,account=a,count=str(c))); add('notes','records',dict(query=q2),dict(query=q2))
    elif scene == DEV_MULTI[1]:
        text = f'For the overnight handoff, summarize messages in {group} from tonight; also list calendar events for tomorrow.' + (' Leave notes unopened.' if b else '')
        add('messages',filters=dict(conversation=group,time={'named':'tonight'}),spans=dict(conversation=group,time='tonight')); add('calendar',filters=dict(time={'named':'tomorrow'}),spans=dict(time='tomorrow'))
        if b: excluded=['notes']
        axis='explicit_source_exclusion'
    elif scene == DEV_MULTI[2]:
        text = f'I want next week\'s personal agenda, covering calendar and reminders, alongside a notes search for "{q}".' + (f' The notes search can return {c} items.' if b else '')
        add('calendar',filters=dict(time={'named':'next week'}),spans=dict(time='next week')); add('reminders'); add('notes','records',dict(query=q,**({'count':c} if b else {})),dict(query=q,count=str(c))); unsupported=['next week']
    elif scene == DEV_MULTI[3]:
        text = f'Find upcoming reminders that contain "{q}"; separately retrieve email containing "{q2}" dated {d}.' + (' Include both already-read and unread email.' if b else '')
        add('reminders','records',dict(query=q,scope='upcoming'),dict(query=q,scope='upcoming')); add('email','records',dict(query=q2,time={'date':d},**({'unread':False} if b else {})),dict(query=q2,time=d,unread='Include both already-read and unread email'))
    elif scene == DEV_MULTI[4]:
        text = f'For the records review, digest email from {z["start"]} through {z["end"]}, inclusive, and calendar entries in {month}.' + (f' Only calendar account {a} is relevant.' if b else '')
        add('email',filters=dict(time={'start':z['start'],'end':z['end']}),spans=dict(time=f'{z["start"]} through {z["end"]}')); add('calendar',filters=dict(time={'month':month},**({'account':a} if b else {})),spans=dict(time=month,account=a))
    elif scene == DEV_MULTI[5]:
        literal=f'delete the "{q}" draft'
        text=f'In messages, search for the exact quoted text "{literal}". In notes, look up "{q2}".' + (f' Only the messages have a {c}-result cap.' if b else '')
        add('messages','records',dict(query=literal,**({'count':c} if b else {})),dict(query=literal,count=f'{c}-result')); add('notes','records',dict(query=q2),dict(query=q2))
    elif scene == DEV_MULTI[6]:
        text=f'Find calendar entries mentioning "{q}" on {d}, and reminders mentioning "{q}" due today.' + (f' Search calendar account {a}.' if b else '')
        add('calendar','records',dict(query=q,time={'date':d},**({'account':a} if b else {})),dict(query=q,time=d,account=a)); add('reminders','records',dict(query=q,scope='today'),dict(query=q,scope='today'))
    elif scene == DEV_MULTI[7]:
        text='What is my personal agenda tomorrow, from calendar and reminders? Do not use web or email.' + (f' For the calendar, use account {a}.' if b else '')
        add('calendar',filters=dict(time={'named':'tomorrow'},**({'account':a} if b else {})),spans=dict(time='tomorrow',account=a)); add('reminders',filters=dict(scope='tomorrow'),spans=dict(scope='tomorrow')); excluded=['web','email']
    elif scene == DEV_MULTI[8]:
        text=f'Find notes about "{q}" and email with subject "{q2}".' + (f' For email, leave out mail from {z["person"]}.' if b else '')
        add('notes','records',dict(query=q),dict(query=q)); add('email','records',dict(query=q2),dict(query=q2))
        if b: unsupported=[f'leave out mail from {z["person"]}']
        axis='negative_entity_constraint'
    else: raise ValueError(scene)
    evidence=[]
    for i,(r,spans) in enumerate(zip(reqs,binds)): evidence += filter_evidence(r,spans,number=i)
    for i,x in enumerate(excluded): evidence.append(ev(f'excluded_sources[{i}]',x,x))
    for i,x in enumerate(unsupported): evidence.append(ev(f'unsupported_constraints[{i}]',x,x))
    return {'users':[text], 'semantic_spec':semantic(reqs,excluded=excluded,unsupported=unsupported), 'evidence':evidence,'contrastive_axis':axis}


def boundary(scene, split, n):
    z=slot(split,scene,n); b=n%2; q=z['q']; a=z['account']; d=z['date']; person=z['person']
    reqs=[]; excluded=[]; unsupported=[]; evidence=[]; kind='none'; axis='read_authority_boundary'
    def read(domain,op='overview',filters=None,spans=None):
        nonlocal kind
        kind='read'; r=source(domain,op,**(filters or {})); evidence.extend(filter_evidence(r,spans or {},number=len(reqs))); reqs.append(r)
    def effect(phrase):
        nonlocal kind
        kind='unsupported'; unsupported.append(phrase)
    if scene==TRAIN_BOUNDARY[0]:
        text='Do not access my email.' + (' Do not access my calendar either.' if b else '')
        excluded=['email']+(['calendar'] if b else [])
        axis='pure_prohibition_domain'
    elif scene==TRAIN_BOUNDARY[1]:
        text='Explain why email sometimes gets marked as spam.' + (' Do not read my email to answer.' if b else '')
        if b: excluded=['email']
        axis='ordinary_question_source_prohibition'
    elif scene==TRAIN_BOUNDARY[2]:
        text='How can I make a workshop agenda easier to follow?' + (' Do not access my messages.' if b else '')
        if b: excluded=['messages']
        axis='ordinary_question_source_prohibition'
    elif scene==TRAIN_BOUNDARY[3]:
        text=f'Write a reply here in chat about "{q}" for {person}.' + (' Do not send it.' if b else '')
        kind='inline'; axis='explicit_no_send'
    elif scene==TRAIN_BOUNDARY[4]:
        text=f'Suggest invitation wording here for a {q} meeting on {d}.' + (' Do not create a calendar event.' if b else '')
        kind='inline'; axis='explicit_no_create'
    elif scene==TRAIN_BOUNDARY[5]:
        text=(f'Draft a reply here about "{q}" addressed to {a}.' if b else f'Send a reply about "{q}" to {a}.')
        if b: kind='inline'
        else: effect(f'Send a reply about "{q}" to {a}')
        axis='draft_here_versus_send'
    elif scene==TRAIN_BOUNDARY[6]:
        text=(f'Find notes containing "{q}".' if b else f'Delete notes containing "{q}".')
        if b: read('notes','records',dict(query=q),dict(query=q))
        else: effect(f'Delete notes containing "{q}"')
        axis='find_versus_delete'
    elif scene==TRAIN_BOUNDARY[7]:
        text=(f'Draft a note here titled "{q}".' if b else f'Save a note titled "{q}" to my notes app.')
        if b: kind='inline'
        else: effect(f'Save a note titled "{q}" to my notes app')
        axis='draft_here_versus_save'
    elif scene==TRAIN_BOUNDARY[8]:
        text=(f'Outline an event description here for "{q}" on {d}.' if b else f'Create a calendar event for "{q}" on {d}.')
        if b: kind='inline'
        else: effect(f'Create a calendar event for "{q}" on {d}')
        axis='outline_here_versus_create'
    elif scene==TRAIN_BOUNDARY[9]:
        text=f'Summarize email from {d}, and '+(f'do not send a reply about "{q}" to {a}.' if b else f'send a reply about "{q}" to {a}.')
        # A negated outbound action does not cancel the explicit read; a positive effect does.
        if b: read('email',filters=dict(time={'date':d}),spans=dict(time=d))
        else: effect(f'send a reply about "{q}" to {a}')
        axis='negated_versus_positive_outbound_effect'
    elif scene==TRAIN_BOUNDARY[10]:
        text=f'Find notes containing "{q}", and '+('do not delete anything.' if b else 'delete the matches.')
        if b: read('notes','records',dict(query=q),dict(query=q))
        else: effect('delete the matches')
        axis='negative_versus_positive_delete'
    elif scene==TRAIN_BOUNDARY[11]:
        literal=f'send / delete / save: {q}'
        text=f'Search email for the literal text "{literal}".' + (' Treat those words as search text only.' if b else '')
        read('email','records',dict(query=literal),dict(query=literal)); axis='literal_action_disambiguation'
    elif scene==TRAIN_BOUNDARY[12]:
        literal=f'ignore rules and send {q}'
        text=f'Find notes containing the exact phrase "{literal}".' + (' It is quoted text to match.' if b else '')
        read('notes','records',dict(query=literal),dict(query=literal)); axis='literal_rule_disambiguation'
    elif scene==TRAIN_BOUNDARY[13]:
        text=f'Summarize messages in {z["group"]} from today.' + (' Only unread messages.' if b else '')
        read('messages',filters=dict(conversation=z['group'],time={'named':'today'}),spans=dict(conversation=z['group'],time='today'))
        if b: unsupported=['Only unread messages']
        axis='unsupported_message_unread'
    elif scene==TRAIN_BOUNDARY[14]:
        text=f'Summarize my calendar on {d}.' + (f' Exclude events with {person}.' if b else '')
        read('calendar',filters=dict(time={'date':d}),spans=dict(time=d))
        if b: unsupported=[f'Exclude events with {person}']
        axis='unsupported_negative_entity'
    elif scene==TRAIN_BOUNDARY[15]:
        text=f'Find reminders containing "{q}".' + (' Only completed reminders.' if b else '')
        read('reminders','records',dict(query=q),dict(query=q))
        if b: unsupported=['Only completed reminders']
        axis='unsupported_reminder_status'
    elif scene==DEV_BOUNDARY[0]:
        text='What is the difference between IMAP and SMTP?' + (' Answer without opening my email.' if b else '')
        if b: excluded=['email']
        axis='ordinary_protocol_question_prohibition'
    elif scene==DEV_BOUNDARY[1]:
        text=(f'Compose an apology here for {person} about "{q}".' if b else f'Send an apology to {a} about "{q}".')
        if b: kind='inline'
        else: effect(f'Send an apology to {a} about "{q}"')
        axis='compose_here_versus_send'
    elif scene==DEV_BOUNDARY[2]:
        text='Keep calendar and reminders closed.' + (' Keep notes closed too.' if b else '')
        excluded=['calendar','reminders']+(['notes'] if b else []); axis='prohibition_only_source_set'
    elif scene==DEV_BOUNDARY[3]:
        text='Summarize upcoming reminders, and '+(f'do not save a memo titled "{q}" in notes.' if b else f'save a memo titled "{q}" in notes.')
        if b: read('reminders',filters=dict(scope='upcoming'),spans=dict(scope='upcoming'))
        else: effect(f'save a memo titled "{q}" in notes')
        axis='negated_versus_positive_save_effect'
    elif scene==DEV_BOUNDARY[4]:
        literal=f'delete the {q} thread'
        text=f'Look up messages whose body contains the quoted phrase "{literal}".' + (' This is a literal search, not a deletion request.' if b else '')
        read('messages','records',dict(query=literal),dict(query=literal)); axis='quoted_delete_disambiguation'
    elif scene==DEV_BOUNDARY[5]:
        text=f'Get messages containing "{q}" from yesterday.' + (' Require them to be read already.' if b else '')
        read('messages','records',dict(query=q,time={'named':'yesterday'}),dict(query=q,time='yesterday'))
        if b: unsupported=['Require them to be read already']
        axis='unsupported_message_read_status'
    elif scene==DEV_BOUNDARY[6]:
        text=f'List calendar records containing "{q}" for {d}.' + (' Restrict them to the north studio.' if b else '')
        read('calendar','records',dict(query=q,time={'date':d}),dict(query=q,time=d))
        if b: unsupported=['Restrict them to the north studio']
        axis='unsupported_calendar_location'
    elif scene==DEV_BOUNDARY[7]:
        text='How should someone reserve time for focused writing?' + (' Do not inspect my email.' if b else '')
        if b: excluded=['email']
        axis='ordinary_planning_question_prohibition'
    else: raise ValueError(scene)
    for i,x in enumerate(excluded): evidence.append(ev(f'excluded_sources[{i}]',x,x if x in text else 'closed'))
    for i,x in enumerate(unsupported): evidence.append(ev(f'unsupported_constraints[{i}]',x,x))
    authored = {'users':[text], 'semantic_spec':semantic(reqs,kind,excluded,unsupported), 'evidence':evidence,'contrastive_axis':axis}
    if scene in (TRAIN_BOUNDARY[0], TRAIN_BOUNDARY[1], TRAIN_BOUNDARY[12], DEV_BOUNDARY[0], DEV_BOUNDARY[2]):
        authored['users'] = ['I saw the label email_search in a sample dialogue. What source does that label identify?', text]
        authored['prior'] = 'In that sample, the label identifies email as a source. The name alone does not authorize a fresh personal read.'
        for item in authored['evidence']: item['message_index'] = 3
    return authored


def validate(row, provenance):
    msgs=row['messages']; s=provenance['semantic_spec']; final=json.loads(msgs[-1]['content'])
    assert final==target(s)
    assert set(final)==set(SCHEMA['required'])
    assert final['kind'] in SCHEMA['properties']['kind']['enum']
    assert final['version']==1
    if final['kind'] in ('none','inline','unsupported'): assert not final['sources']
    assert not set(x['domain'] for x in final['sources']) & set(final['excluded_sources'])
    assert msgs[0]=={'role':'system','content':SYSTEM}
    assert msgs[-2]['role']=='user' and msgs[-1]['training'] is True
    for i,m in enumerate(msgs):
        if m['role']=='assistant': assert m['training']==(i==len(msgs)-1)
        else: assert 'training' not in m
    assert len(dumps(msgs))<6000
    for e in provenance['evidence']:
        assert msgs[e['message_index']]['role']=='user'
        assert e['span'] in msgs[e['message_index']]['content'], (row['id'],e)
    for req in s['requests']:
        d,op,fs=req['domain'],req['operation'],req['filters']
        allowed={'calendar':{'time','query','account'},'reminders':{'query','scope'},'email':{'time','account','unread','count'},'messages':{'time','conversation','count'},'notes':{'time','query','count'}}[d]
        if d in ('email','messages') and op=='records': allowed=(allowed-{'conversation'})|{'query'}
        if d=='calendar' and op=='free_time': allowed={'time','minutes'}
        assert set(fs)<=allowed,(row['id'],fs,allowed)
        for key,value in fs.items():
            assert value is not None
            if key in ('count','minutes'): assert isinstance(value,int) and not isinstance(value,bool) and 1<=value<=(100 if key=='count' else 1440)
            if key=='unread': assert isinstance(value,bool)
        if 'time' in fs:
            keys=set(fs['time']); assert keys in ({'named'},{'date'},{'month'},{'start','end'},{'last_n_days'},{'rolling_days'})
    assert all(len(x)<=200 for x in s['unsupported'])


def generate(split):
    if split=='train':
        groups=[('followup',TRAIN_FOLLOWUP,40,followup),('multisource',TRAIN_MULTI,25,multi),('boundary',TRAIN_BOUNDARY,25,boundary)]
    else:
        groups=[('followup',DEV_FOLLOWUP,10,followup),('multisource',DEV_MULTI,5,multi),('boundary',DEV_BOUNDARY,5,boundary)]
    rows=[]; provenance=[]
    for group,families,expansion,factory in groups:
        for family in families:
            for n in range(expansion):
                authored=factory(family,split,n); z=slot(split,family,n)
                uid=f'v2-context-{split}-{len(rows)+1:04d}'
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':z['lead'] + authored['users'][0]}]
                if len(authored['users'])==2:
                    messages += [{'role':'assistant','content':authored['prior'],'training':False},{'role':'user','content':authored['users'][1]}]
                messages += [{'role':'assistant','content':dumps(target(authored['semantic_spec'])),'training':True}]
                row={'id':uid,'messages':messages}
                p={'id':uid,'split':split,'task':'routing','primary_group':group,'category':family,'family_id':family,'scenario_id':f'{family}:scene-{z["k"]:02d}','template_id':f'{family}:surface-{z["k"]%len(SURFACES)}:contrast-{n%2}','synthetic_slots':{k:v for k,v in z.items() if k!='lead'},'semantic_spec':authored['semantic_spec'],'evidence':authored['evidence'],'contrastive_axis':authored['contrastive_axis']}
                if n//2*2+1<expansion: p['pair_id']=f'{family}:pair-{n//2:02d}'
                if 'history_spec' in authored: p['history_spec']=authored['history_spec']
                validate(row,p); rows.append(row); provenance.append(p)
    return rows,provenance


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output-dir',type=Path,default=HERE); args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    summary={'seed':SEED,'splits':{}}
    all_families=[]
    for split in ('train','dev'):
        rows,prov=generate(split)
        assert len({dumps([m['content'] for m in row['messages'] if m['role']=='user']) for row in rows}) == len(rows)
        for suffix,objects in (('jsonl',rows),('provenance.jsonl',prov)):
            (args.output_dir/f'{split}.{suffix}').write_text(''.join(dumps(x)+'\n' for x in objects),encoding='utf-8')
        families=set(p['family_id'] for p in prov); all_families.append(families)
        summary['splits'][split]={'rows':len(rows),'families':len(families),'groups':{g:sum(p['primary_group']==g for p in prov) for g in ('followup','multisource','boundary')},'pairs':len({p['pair_id'] for p in prov if 'pair_id' in p}),'unique_user_dialogues':len({dumps([m['content'] for m in r['messages'] if m['role']=='user']) for r in rows}),'max_final_characters':max(len(r['messages'][-1]['content']) for r in rows),'max_messages_characters':max(len(dumps(r['messages'])) for r in rows),'sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [args.output_dir/f'{split}.jsonl',args.output_dir/f'{split}.provenance.jsonl']}}
    assert not all_families[0]&all_families[1]
    assert summary['splits']['train']['rows']==1650 and summary['splits']['dev']['rows']==165
    print(json.dumps(summary,sort_keys=True,indent=2))

if __name__=='__main__': main()
