#!/usr/bin/env python3
"""Fresh synthetic argument routing examples; standard-library, inert, deterministic."""
import argparse
import json
from pathlib import Path

SEED = 610220261007  # Deterministic indexing rather than pseudorandom sampling.
ROOT = Path(__file__).resolve().parents[2]
SYSTEM = (ROOT / 'router-system.txt').read_text()
SCHEMA = json.loads((ROOT / 'intent.schema.v1.json').read_text())

# Families are declared before slot expansion. Distinct split scene/composition sets.
# Every three-element surface list is independently authored, not machine paraphrased.
TIME_TRAIN = [
 ('gallery-install', 'calendar', 'records', 'query', ['For the gallery installation, find calendar entries matching {query} {period}.', 'Look up {query} in my calendar {period}; I am planning the installation.', 'My installation planning needs the calendar records for {query} {period}.']),
 ('choir-rota', 'calendar', 'overview', 'account', ['Summarize the choir calendar {period}{account}.', 'Give me a digest of my choir schedule {period}{account}.', 'I am checking the choir rota; show calendar highlights {period}{account}.']),
 ('workshop-break', 'calendar', 'free_time', 'minutes', ['Find {minutes} minutes free in my calendar {period} for a workshop break.', 'For the workshop break, look for a calendar opening of {minutes} minutes {period}.', 'Check calendar availability {period}; I need a {minutes}-minute workshop break.']),
 ('archive-receipts', 'email', 'records', 'query_account', ['Find emails matching {query} {period}{account} for the archive receipts.', 'For my archive receipts, search email for {query} {period}{account}.', 'Retrieve email records containing {query} {period}{account}; these are archive receipts.']),
 ('league-mail', 'email', 'overview', 'unread_count', ['Give me a league email digest {period}{unread}{count}.', 'Summarize my league inbox {period}{unread}{count}.', 'I am catching up on league mail; show email highlights {period}{unread}{count}.']),
 ('map-club-thread', 'messages', 'overview', 'conversation_count', ['Summarize messages in {conversation} {period}{count} before map club.', 'Catch me up on the {conversation} message conversation {period}{count}; map club is next.', 'For map club preparation, give me message highlights from {conversation} {period}{count}.']),
 ('glaze-experiment', 'notes', 'records', 'query_count', ['Find notes matching {query} {period}{count} for my glaze experiment.', 'Search my notes for {query} {period}{count}; I need the glaze records.', 'Retrieve my glaze experiment notes containing {query} {period}{count}.']),
 ('bike-maintenance', 'reminders', 'records', 'query', ['Find reminders matching {query} {period} for bike maintenance.', 'For bike maintenance, search reminders for {query} {period}.', 'Look up reminder records containing {query} {period}; I am checking bike maintenance.']),
 ('ferry-visits', 'calendar', 'overview', 'none', ['Summarize my calendar {period} while I plan ferry visits.', 'For ferry visit planning, show my calendar highlights {period}.', 'Give me a calendar digest {period} for the ferry visits.']),
 ('mentoring-hours', 'calendar', 'records', 'account', ['Show calendar records {period}{account} for my mentoring hours.', 'Retrieve my mentoring calendar entries {period}{account}.', 'I am reviewing mentoring hours; list the calendar records {period}{account}.']),
 ('studio-gap', 'calendar', 'free_time', 'minutes_optional', ['Check free time in my calendar {period}{duration} for studio cleanup.', 'For studio cleanup, find calendar availability {period}{duration}.', 'Show openings in my calendar {period}{duration}; I need a cleanup gap.']),
 ('repair-invoices', 'email', 'records', 'query_unread_count', ['Search email for {query} {period}{unread}{count} to review repair invoices.', 'For repair invoice review, find emails containing {query} {period}{unread}{count}.', 'Look up my repair invoice email records matching {query} {period}{unread}{count}.']),
 ('committee-inbox', 'email', 'overview', 'account_count', ['Summarize my email {period} for committee review{account}{count}.', 'Give me a committee inbox digest {period}{account}{count}.', 'Show email highlights {period}{account}{count} for committee preparation.']),
 ('tile-sample-chat', 'messages', 'records', 'query_count', ['Find messages containing {query} {period}{count} about tile samples.', 'Search my messages for {query} {period}{count}; I need the sample discussion.', 'Retrieve tile sample message records matching {query} {period}{count}.']),
 ('garden-journal', 'notes', 'overview', 'count', ['Summarize my notes {period}{count} for garden journal review.', 'Give me a notes digest {period}{count} for garden journal review.', 'Show note highlights {period}{count} while I review the garden journal.']),
 ('tax-folder', 'email', 'records', 'query_account_count', ['Find email containing {query} {period}{account}{count} for my tax folder.', 'For the tax folder, search emails matching {query} {period}{account}{count}.', 'Retrieve tax folder emails for the literal text {query} {period}{account}{count}.']),
 ('volunteer-brief', 'messages', 'overview', 'none', ['Summarize messages {period} for my volunteer briefing.', 'Give me message highlights {period} before I prepare the volunteer brief.', 'Catch me up on messages {period}; I am writing a volunteer briefing here.']),
 ('book-index', 'notes', 'records', 'query', ['Find notes containing {query} {period} for my book index.', 'For the book index, search my notes for {query} {period}.', 'Look up notebook records matching {query} {period}; I need the book references.']),
 ('kitchen-checklist', 'reminders', 'overview', 'none', ['Summarize my reminders {period} while I review the kitchen checklist.', 'Give me a reminder digest {period} for kitchen checklist review.', 'Show reminder highlights {period} while I review the kitchen checklist.']),
 ('museum-slots', 'calendar', 'records', 'query_account', ['Find calendar records matching {query} {period}{account} for museum shifts.', 'For museum shifts, look up calendar entries containing {query} {period}{account}.', 'Search the calendar for {query} {period}{account}; I need the museum slots.']),
 ('language-practice', 'calendar', 'free_time', 'minutes', ['Find a {minutes}-minute calendar opening {period} for language practice.', 'Check my calendar {period} for {minutes} minutes free to practice vocabulary.', 'For language practice, show calendar availability of {minutes} minutes {period}.']),
 ('newsletter-review', 'email', 'overview', 'unread', ['Summarize my email {period} for newsletter review{unread}.', 'Give me newsletter inbox highlights {period}{unread}.', 'I am reviewing newsletters; provide an email digest {period}{unread}.']),
 ('tool-share-chat', 'messages', 'overview', 'conversation', ['Catch me up on messages with {conversation} {period} for tool sharing.', 'Summarize the {conversation} message thread {period}; I am reviewing tool sharing.', 'Give me message highlights in {conversation} {period} before the tool exchange.']),
 ('quilt-patterns', 'notes', 'records', 'query_count', ['Find notes containing {query} {period}{count} for quilt patterns.', 'Look up my quilt notes matching {query} {period}{count}.', 'For quilt pattern research, search notes for {query} {period}{count}.']),
 ('parcel-followups', 'reminders', 'records', 'query', ['Find reminders matching {query} {period} for parcel followups.', 'Search reminders for {query} {period}; I need my parcel followups.', 'For parcel followups, show reminder records containing {query} {period}.']),
 ('rehearsal-roster', 'calendar', 'overview', 'account', ['Summarize my calendar {period}{account} for rehearsal roster review.', 'Give me calendar highlights {period}{account} for the rehearsal roster.', 'I am checking the rehearsal roster; digest my calendar {period}{account}.']),
 ('registration-mail', 'email', 'records', 'query_account_unread', ['Find email containing {query} {period}{account}{unread} for course registration.', 'Search my registration emails for {query} {period}{account}{unread}.', 'For course registration, retrieve email records matching {query} {period}{account}{unread}.']),
 ('orchard-log', 'notes', 'overview', 'none', ['Summarize my notes {period} before I review the orchard log.', 'Give me note highlights {period} while I review orchard work.', 'I am reviewing the orchard log; provide a notes digest {period}.']),
 ('costume-chat', 'messages', 'records', 'query', ['Find messages containing {query} {period} for costume fitting.', 'Search messages for {query} {period}; I need the costume discussion.', 'For costume fitting, retrieve messages matching {query} {period}.']),
 ('grant-correspondence', 'email', 'overview', 'account_unread_count', ['Summarize my email {period} for grant review{account}{unread}{count}.', 'Give me a grant correspondence digest {period}{account}{unread}{count}.', 'Show my email highlights {period}{account}{unread}{count} for grant review.']),
]
TIME_DEV = [
 ('repair-cafe-shifts', 'calendar', 'records', 'query', ['Before repair cafe staffing, retrieve calendar entries for {query} {period}.', 'I need the staffing entries: search my calendar {period} for {query}.', 'Read calendar records {period} matching {query} so I can check repair cafe staffing.']),
 ('reading-nook-gap', 'calendar', 'free_time', 'minutes_optional', ['Look for calendar space {period}{duration} for arranging the reading nook.', 'For arranging the reading nook, report calendar availability {period}{duration}.', 'I need room to rearrange the reading nook; check calendar openings {period}{duration}.']),
 ('costume-rental-mail', 'email', 'records', 'query_account_unread', ['Retrieve costume rental emails for {query} {period}{account}{unread}.', 'Read email records {period}{account}{unread} that contain {query}; I am checking costume rentals.', 'I need the costume rental correspondence: look up {query} in email {period}{account}{unread}.']),
 ('shared-workbench', 'messages', 'overview', 'conversation_count', ['For the shared workbench handover, digest messages with {conversation} {period}{count}.', 'Read message highlights {period} from {conversation}{count}; I need the workbench handover.', 'I am checking the handover for our workbench; summarize {conversation} messages {period}{count}.']),
 ('seed-swap-notes', 'notes', 'records', 'query_count', ['Retrieve seed swap notebook entries for {query} {period}{count}.', 'Read notes {period} containing {query}{count} before I sort seed swaps.', 'I need my seed swap entries; look up {query} in notes {period}{count}.']),
 ('loom-reminders', 'reminders', 'records', 'query', ['Retrieve loom maintenance reminders for {query} {period}.', 'Look through reminders {period} for {query}; I am reviewing the loom checklist.', 'I need the loom checklist entries; search reminders {period} for {query}.']),
 ('community-train-mail', 'email', 'overview', 'account_count', ['Digest my train excursion inbox {period}{account}{count}.', 'For our train excursion preparation, read email highlights {period}{account}{count}.', 'I need the train excursion mail summary {period}{account}{count}.']),
 ('sculpture-calendar', 'calendar', 'overview', 'none', ['Read calendar highlights {period} for the sculpture transport schedule.', 'I need the sculpture transport schedule summarized from my calendar {period}.', 'For planning sculpture transport, give me a digest of calendar entries {period}.']),
 ('printmaking-notes', 'notes', 'overview', 'count', ['Read my note highlights {period}{count} before printmaking review.', 'I need a digest of my notes {period}{count} before printmaking review.', 'For reviewing printmaking work, summarize notebook entries {period}{count}.']),
]

FILTER_TRAIN = [
 ('receipt-literal', 'email', 'records', 'query', ['For the repair ledger, find emails containing exactly {query}{extras}.', 'Search email for the literal text {query}{extras}; I need ledger evidence.', 'Look up {query} verbatim in email{extras} for the repair ledger.']),
 ('two-inboxes', 'email', 'overview', 'account_count', ['Summarize email for the allotment committee{extras}.', 'Give me an allotment inbox digest{extras}.', 'Show email highlights for allotment planning{extras}.']),
 ('read-state-ledger', 'email', 'overview', 'unread_count', ['Summarize my bursary email{extras}.', 'Give me a bursary inbox catch-up{extras}.', 'Show email highlights for my bursary review{extras}.']),
 ('sender-is-text', 'email', 'records', 'query_account', ['Find email containing the exact text {query}{extras} for a sender audit.', 'Search my email literally for {query}{extras}; I am checking a sender spelling.', 'Retrieve email matching {query} verbatim{extras} for my sender audit.']),
 ('calendar-title', 'calendar', 'records', 'query_account', ['For the ceramics roster, find calendar entries containing {query}{extras}.', 'Search my calendar for the exact title text {query}{extras}.', 'Look up calendar records matching {query} literally{extras} for the ceramics roster.']),
 ('reminder-scopes', 'reminders', 'records', 'query_scope', ['Find reminders containing {query}{extras} for my freezer inventory.', 'Search the reminder list for {query}{extras}; I am checking freezer inventory.', 'Retrieve reminder records matching {query}{extras} before inventory review.']),
 ('named-message-digest', 'messages', 'overview', 'conversation_count', ['Summarize messages in {conversation}{extras} for our folding table handover.', 'Catch me up on the {conversation} conversation{extras} before the table handover.', 'Give me message highlights from {conversation}{extras}; I am checking the table handover.']),
 ('message-literal', 'messages', 'records', 'query_count', ['For lantern assembly, find messages containing exactly {query}{extras}.', 'Search messages for the literal string {query}{extras} about the lantern parts.', 'Look up {query} verbatim in message records{extras} for lantern assembly.']),
 ('note-literal-limit', 'notes', 'records', 'query_count', ['Find notes containing {query}{extras} for the weaving samples.', 'Search my notes for literal text {query}{extras} before sample review.', 'Retrieve note records matching {query} exactly{extras} for weaving samples.']),
 ('availability-duration', 'calendar', 'free_time', 'minutes_optional', ['Check my calendar for an opening tomorrow{extras} for sharpening tools.', 'Find calendar availability tomorrow{extras} so I can sharpen tools.', 'For tool sharpening, show free time in my calendar tomorrow{extras}.']),
 ('unread-message-limit', 'messages', 'records', 'query_message_unread', ['Find messages containing {query}{extras} for festival setup.', 'Search message records for {query}{extras}; I need the setup discussion.', 'For festival setup, retrieve messages matching {query}{extras}.']),
 ('reminder-location', 'reminders', 'records', 'query_unsupported', ['Find reminders containing {query}{extras} for cupboard repairs.', 'Search reminders for {query}{extras}; I am reviewing cupboard repairs.', 'For cupboard repairs, retrieve reminders matching {query}{extras}.']),
 ('calendar-status', 'calendar', 'records', 'query_unsupported', ['Find calendar entries containing {query}{extras} for the grant meeting.', 'Search calendar records for {query}{extras}; I need the grant meeting.', 'For grant meeting review, retrieve calendar entries matching {query}{extras}.']),
]
FILTER_DEV = [
 ('badge-proof-mail', 'email', 'records', 'query_count', ['Read emails containing the verbatim string {query}{extras} for badge proofs.', 'Retrieve badge proof mail matching {query} literally{extras}.', 'I need badge proof correspondence; look up the exact string {query} in email{extras}.']),
 ('rail-inbox-choice', 'email', 'overview', 'account', ['Digest my rail-pass mail{extras}.', 'Read email highlights for rail-pass planning{extras}.', 'I need a summary of rail-pass correspondence{extras}.']),
 ('photo-mail-state', 'email', 'records', 'query_unread_count', ['Retrieve photography emails containing {query}{extras}.', 'Read email records for literal {query}{extras} before photo selection.', 'I need photo selection mail; find emails matching {query}{extras}.']),
 ('supplier-address-text', 'email', 'records', 'query_account_unread', ['Read emails containing exactly {query}{extras} for supplier reconciliation.', 'Retrieve supplier correspondence matching {query} verbatim{extras}.', 'For supplier reconciliation, look up literal {query} in email{extras}.']),
 ('fitting-calendar-title', 'calendar', 'records', 'query', ['Read calendar records matching {query} literally{extras} for uniform fitting.', 'Retrieve fitting calendar entries containing the exact text {query}{extras}.', 'I need uniform fitting entries; search the calendar for verbatim {query}{extras}.']),
 ('pantry-list-scope', 'reminders', 'overview', 'scope', ['Digest my reminders{extras} before pantry restocking.', 'Read reminder highlights for pantry restocking{extras}.', 'I need a reminder summary{extras} before pantry restocking.']),
 ('bridge-club-digest', 'messages', 'overview', 'conversation', ['Read message highlights with {conversation}{extras} for bridge club.', 'Digest the {conversation} message conversation{extras} before bridge club.', 'I need a bridge club catch-up from {conversation} messages{extras}.']),
 ('fabric-chat-search', 'messages', 'records', 'query_count', ['Read messages containing verbatim {query}{extras} about fabric lengths.', 'Retrieve fabric length messages matching {query} literally{extras}.', 'I need the fabric length discussion; find exact {query} in messages{extras}.']),
 ('camera-notes-selection', 'notes', 'overview', 'query_count', ['Read note highlights matching {query}{extras} for camera testing.', 'Digest camera test notes containing {query}{extras}.', 'I need a camera testing summary from notes matching {query}{extras}.']),
 ('quiet-reading-slot', 'calendar', 'free_time', 'minutes_optional', ['Report calendar availability today{extras} for quiet reading.', 'Read my free calendar openings today{extras} so I can read.', 'I need a quiet reading slot; check calendar free time today{extras}.']),
 ('message-read-state', 'messages', 'overview', 'conversation_message_unread', ['Digest messages with {conversation}{extras} for the costume handover.', 'Read the {conversation} message highlights{extras} before costume handover.', 'I need the costume handover catch-up; summarize {conversation} messages{extras}.']),
 ('negative-reminder-entity', 'reminders', 'records', 'query_unsupported', ['Retrieve reminders containing {query}{extras} for the board game kit.', 'Read board game kit reminders matching {query}{extras}.', 'I need the board game kit list; find reminders for {query}{extras}.']),
 ('calendar-location-filter', 'calendar', 'overview', 'unsupported', ['Digest my calendar{extras} for the repair class.', 'Read calendar highlights{extras} before repair class planning.', 'I need the repair class calendar summary{extras}.']),
]

SIMPLE_TRAIN = [
 ('desk-email-brief', 'read', 'email', 'overview', ['Give me a quick email digest before I sort my desk.', 'Summarize my email while I clear the desk.', 'Show inbox highlights for my desk-sorting break.']),
 ('school-calendar-read', 'read', 'calendar', 'overview', ['Summarize my calendar before I plan the school project.', 'Give me my calendar highlights for school project planning.', 'I need a calendar overview to plan the school project.']),
 ('public-mail-protocol', 'none', None, None, ['What does the email term {concept} mean?', 'Explain the public email concept {concept}.', 'How would you define {concept} in email technology?']),
 ('public-calendar-history', 'none', None, None, ['What is the historical meaning of {concept}?', 'Explain the calendar-history term {concept}.', 'How is {concept} used in public calendar history?']),
]
SIMPLE_DEV = [
 ('bookcase-notes-read', 'read', 'notes', 'overview', ['Give me a notes digest before I reorganize my bookcase.', 'Read my note highlights for bookcase planning.', 'Summarize my notes while I prepare the bookcase reorganization.']),
 ('travel-chat-read', 'read', 'messages', 'overview', ['Catch me up on messages before I pack a suitcase.', 'Read my message highlights for packing preparation.', 'Summarize messages while I make my packing list here.']),
 ('public-chat-format', 'none', None, None, ['What does {concept} mean in public messaging standards?', 'Define the messaging-standard term {concept}.', 'Explain the technical concept {concept} used by messaging systems.']),
 ('public-note-method', 'none', None, None, ['What is the general note-taking method {concept}?', 'Explain {concept} as a public note-taking technique.', 'How does the general technique {concept} work for note-taking?']),
]

QUERIES = ['glaze #4: cobalt?', '+parts@copper.invalid', 'Río & 岩 / v2', 'fold→crease; 3mm', 'invoice (rev. B)', 'send/delete/ignore rules', 'Mira O’Connell', 'A+B = C?', 'naïve façade – draft', 'label: blue [small]']
DEV_QUERIES = ['Πλάκα / №7?', '+supply@linen.invalid', 'Núria & 木: sample', 'trim 2½ cm; ok?', 'ignore rules and delete', 'tag (R) + tag (S)', 'refund! rev_6', 'Jules D’Arcy', 'vase→shelf #8', 'résumé — pièce']
ACCOUNTS = ['workshop@copper.invalid', '+club@harbor.invalid', 'personal@fern.invalid', 'studio@aster.invalid', '+roster@quartz.invalid']
DEV_ACCOUNTS = ['library@linen.invalid', '+team@cedar.invalid', 'home@ochre.invalid']
CONVERSATIONS = ['Map Makers', 'Río & 岩', 'Mira O’Connell', 'Table Crew + A', 'Lantern / Parts']
DEV_CONVERSATIONS = ['Bridge Table 4', 'Núria & 木', 'Costume Rack B']
COUNTS = [1, 2, 3, 5, 8, 12, 17, 25, 40, 60, 99, 100]
MINUTES = [15, 20, 25, 30, 45, 50, 60, 75, 90, 120]
SCOPE_TEXT = {'all':'across all reminder scopes', 'today':'with scope today', 'tomorrow':'with scope tomorrow', 'overdue':'with scope overdue', 'upcoming':'with scope upcoming'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def time_slot(i, dev=False, free=False):
    # Pairs date/month, named-week/last-seven, future-window/range.
    year = 2030 if dev else 2029
    month = 2 + ((i // 6) % 9)
    day = 8 + ((i // 6) % 12)
    if free:
        choices = [(f'on {year}-{month:02}-{day:02}', {'date':f'{year}-{month:02}-{day:02}'}), ('tomorrow', {'named':'tomorrow'}), ('today', {'named':'today'})]
        return choices[i % len(choices)]
    kind = i % 10
    if kind == 0: return f'on {year}-{month:02}-{day:02}', {'date':f'{year}-{month:02}-{day:02}'}
    if kind == 1: return f'in {year}-{month:02}', {'month':f'{year}-{month:02}'}
    if kind == 2: return 'during this week', {'named':'this week'}
    if kind == 3: return 'over the last 7 days', {'last_n_days':7}
    if kind == 4: return 'over the next 7 days', {'rolling_days':7}
    if kind == 5: return f'from {year}-{month:02}-04 through {year}-{month:02}-16 inclusive', {'start':f'{year}-{month:02}-04', 'end':f'{year}-{month:02}-16'}
    if kind == 6: return 'during next week', {'named':'next week'}
    if kind == 7: return 'during last month', {'named':'last month'}
    if kind == 8: return f'in {"February March April May June July August September October".split()[month-2]} {year}', {'month':f'{year}-{month:02}'}
    return 'yesterday', {'named':'yesterday'}


def make_content(family, i, split, primary, n):
    dev = split == 'dev'
    name, domain, operation, mode, surfaces = family
    tokens = mode.split('_')
    pair_variant = i % 2
    slot_index = i // 2
    filter_variant = (slot_index % 2) if primary == 'time' else pair_variant
    optional_fields = [field for field in ['account', 'count', 'unread', 'scope'] if field in tokens]
    if 'message_unread' in mode:
        optional_fields = [field for field in optional_fields if field != 'unread'] + ['message_unread']
    if mode == 'minutes_optional': optional_fields.append('minutes')
    if 'unsupported' in tokens: optional_fields.append('unsupported')
    active_field = optional_fields[slot_index % len(optional_fields)] if optional_fields else ('conversation_literal' if 'conversation' in tokens else 'query_literal')
    def present(field):
        if primary == 'time': return bool(filter_variant)
        return bool(pair_variant) if field == active_field else slot_index % 3 != 2
    query = (DEV_QUERIES if dev else QUERIES)[slot_index % 10]
    if primary == 'filters' and active_field == 'query_literal' and pair_variant:
        query += '!'
    account = (DEV_ACCOUNTS if dev else ACCOUNTS)[slot_index % (3 if dev else 5)]
    conversation = (DEV_CONVERSATIONS if dev else CONVERSATIONS)[slot_index % (3 if dev else 5)]
    if primary == 'filters' and active_field == 'conversation_literal' and pair_variant:
        conversation += '!'
    count = COUNTS[slot_index % len(COUNTS)]
    minutes = MINUTES[slot_index % len(MINUTES)]
    filters, unsupported, items = {}, [], []
    values = {'query': '“'+query+'”', 'account':'', 'count':'', 'unread':'', 'duration':'', 'minutes':minutes, 'conversation':'“'+conversation+'”', 'extras':''}
    def add(field, value, span):
        filters[field] = value
        items.append((f'sources[0].{field}', value, span))
    def constraint(text):
        unsupported.append(text)
        items.append((f'unsupported_constraints[{len(unsupported)-1}]', text, text))
    if 'query' in tokens:
        add('query', query, query)
    if 'conversation' in tokens:
        add('conversation', conversation, conversation)
    if 'account' in tokens and present('account'):
        values['account'] = f', using account {account}'
        add('account', account, account)
    if 'count' in tokens and present('count'):
        values['count'] = f', limited to {count} items'
        add('count', count, str(count))
    if 'unread' in mode and 'message_unread' not in mode:
        state = slot_index % 3  # Each state has a matched presence/absence pair.
        if present('unread') and (state < 2 or (primary == 'filters' and active_field == 'unread')):
            is_unread = (slot_index % 2 == 0) if primary == 'filters' and active_field == 'unread' else state == 0
            literal = 'unread email only' if is_unread else 'include both read and unread email'
            values['unread'] = ', '+literal
            add('unread', is_unread, literal)
    if mode == 'minutes':
        add('minutes', minutes, str(minutes))
    if mode == 'minutes_optional' and present('minutes'):
        values['duration'] = f', lasting {minutes} minutes'
        add('minutes', minutes, str(minutes))
    if 'scope' in tokens and present('scope'):
        scope = list(SCOPE_TEXT)[slot_index % 5]
        values['scope'] = ', '+SCOPE_TEXT[scope]
        add('scope', scope, SCOPE_TEXT[scope])
    else:
        values['scope'] = ''
    if 'message_unread' in mode and present('message_unread'):
        literal = 'unread messages only' if slot_index % 2 == 0 else 'already-read messages only'
        values['message_unread'] = ', '+literal
        constraint(literal)
    else:
        values['message_unread'] = ''
    if 'unsupported' in tokens and present('unsupported'):
        if name == 'reminder-location': literal = 'only at the workshop'
        elif name == 'calendar-status': literal = 'only accepted invitations'
        elif name == 'negative-reminder-entity': literal = 'excluding reminders about dice'
        else: literal = 'only events in the library'
        values['unsupported'] = ', '+literal
        constraint(literal)
    else:
        values['unsupported'] = ''
    if primary == 'time':
        phrase, time = time_slot(i, dev, operation == 'free_time')
        values['period'] = phrase
        if domain == 'reminders':
            if time.get('named') in ['today', 'tomorrow']:
                add('scope', time['named'], phrase)
            else:
                constraint(phrase)
        else:
            add('time', time, phrase)
        axis = 'time_representation'
    else:
        values['extras'] = ''.join(values[key] for key in ['account','unread','count','scope','duration','message_unread','unsupported'])
        if operation == 'free_time':
            phrase = 'today' if dev else 'tomorrow'
            add('time', {'named':phrase}, phrase)
        axis = active_field+'_presence'
    surface_index = slot_index % len(surfaces)
    text = surfaces[surface_index].format(**values)
    if primary == 'filters' or (primary == 'time' and operation == 'free_time'):
        # Irrelevant planning context varies within a family, never a source filter.
        review_contexts = ['sorting loose paper', 'arranging a pencil tray', 'moving my reading lamp', 'clearing my desk corner', 'opening a blank planning notebook', 'choosing an index card', 'laying out a paper folder', 'tidying a shelf nearby', 'setting up a quiet work area', 'putting labels on empty folders', 'organizing stationery', 'making room for a writing pad', 'sharpening a pencil', 'putting a ruler on my desk', 'turning to a blank planning page', 'moving a chair to the table', 'placing a bookmark in my notebook', 'stacking empty envelopes', 'gathering paper clips', 'folding a blank card', 'clearing a spot beside the lamp', 'putting a pen within reach', 'arranging a small paper tray', 'placing a fresh sheet on the table', 'setting out a clipboard']
        text += ' I am ' + review_contexts[slot_index % len(review_contexts)] + '.'
    semantic = {'kind':'read', 'requests':[{'domain':domain,'operation':operation,'filters':filters}], 'excluded':[], 'unsupported':unsupported}
    evidence = [{'field':field,'value':value,'message_index':1,'span':span} for field,value,span in items]
    for e in evidence:
        assert e['span'] in text, (name, text, e)
    slots = {'query':query if 'query' in tokens else None, 'account':account if 'account' in filters else None, 'conversation':conversation if 'conversation' in tokens else None, 'count':count if 'count' in filters else None, 'minutes':minutes if 'minutes' in filters else None, 'period':values.get('period'), 'variant_index':i}
    slots = {k:v for k,v in slots.items() if v is not None}
    provenance = {'task':'routing','primary_group':primary,'category':f'{domain}.{operation}.{mode}','family_id':name,'scenario_id':f'{name}-scene-{slot_index:02d}','template_id':f'{name}-surface-{surface_index+1}','synthetic_slots':slots,'semantic_spec':semantic,'evidence':evidence,'contrastive_axis':axis,'pair_id':f'{name}-{split}-pair-{slot_index:02d}'}
    return text, semantic, provenance


def simple_content(family, i, split):
    name, kind, domain, operation, surfaces = family
    concepts = {
      'public-mail-protocol':['SMTP','IMAP','MIME','DKIM','SPF','POP3','an email header','a Message-ID','a mail relay','a bounce notice','a return path','a reply-to header','a mailing list','a mail queue','an attachment encoding','a domain literal','a mailbox alias','a spam filter','a mail transfer agent','an authentication result'],
      'public-calendar-history':['a leap day','the Gregorian reform','a lunar month','a solar year','an intercalary month','the Julian calendar','an epoch','an equinox','a perpetual calendar','an ordinal date','a sidereal year','a tropical year','a proleptic calendar','a lunisolar calendar','an epact','a calendar era','a bissextile year','a civil year','a solstice','an astronomical year numbering'],
      'public-chat-format':['UTF-8','a message stanza','end-to-end encryption','a protocol extension','a delivery receipt'],
      'public-note-method':['the Cornell method','a concept map','an outline','a commonplace book','a slip-box']}
    surface_index = (i // 2) % 3
    text = surfaces[surface_index].format(concept=concepts.get(name,[''])[i % len(concepts.get(name,['']))])
    # Read scene variations give a small explicit source-local named lookup, paired with overview.
    if kind == 'read':
        context_options = ['sorting a stack of sketches', 'arranging pencils and paper', 'clearing space for a notebook', 'moving a lamp beside my chair', 'putting a blank index card nearby', 'finding a comfortable place to sit', 'organizing a small tray of stationery', 'setting aside a folder for planning', 'tidying a shelf beside me']
        text += ' I am ' + context_options[(i // 6) % len(context_options)] + '.'
    requests = [] if domain is None else [{'domain':domain,'operation':operation,'filters':{}}]
    evidence=[]
    axis='public_concept' if kind=='none' else 'count_or_time_presence'
    if kind=='read' and i % 2:
        if domain in ['email','messages','notes']:
            count = COUNTS[(i//2) % len(COUNTS)]
            span=f'Limit the summary to {count} items.'
            text+=' '+span
            requests[0]['filters']['count']=count
            evidence.append({'field':'sources[0].count','value':count,'message_index':1,'span':str(count)})
        else:
            text+=' Use only tomorrow.'
            requests[0]['filters']['time']={'named':'tomorrow'}
            evidence.append({'field':'sources[0].time','value':{'named':'tomorrow'},'message_index':1,'span':'tomorrow'})
    semantic={'kind':kind,'requests':requests,'excluded':[],'unsupported':[]}
    provenance={'task':'routing','primary_group':'simple','category':f'{kind}.{domain or "public"}','family_id':name,'scenario_id':f'{name}-scene-{i//2:02d}','template_id':f'{name}-surface-{surface_index+1}','synthetic_slots':dict(variant_index=i, **({'concept':concepts[name][i % len(concepts[name])]} if name in concepts else {})),'semantic_spec':semantic,'evidence':evidence,'contrastive_axis':axis}
    if kind=='read': provenance['pair_id']=f'{name}-{split}-pair-{i//2:02d}'
    return text, semantic, provenance


def gold(semantic):
    return {'version':1,'kind':semantic['kind'],'sources':[dict(domain=r['domain'],operation=r['operation'],**r['filters']) for r in semantic['requests']], 'excluded_sources':semantic['excluded'], 'unsupported_constraints':semantic['unsupported']}


def validate(target):
    assert set(target)==set(SCHEMA['required'])
    assert target['kind'] in SCHEMA['properties']['kind']['enum']
    for src in target['sources']:
        assert set(src) <= set(SCHEMA['properties']['sources']['items']['properties'])
        assert {'domain','operation'} <= set(src)
        assert all(v is not None for v in src.values())
        allowed={'calendar':{'time','query','account'},'reminders':{'query','scope'},'email':{'time','account','unread','count','query'},'messages':{'time','conversation','count','query'},'notes':{'time','query','count'}}[src['domain']]
        if src['operation']=='free_time': allowed={'time','minutes'}
        if src['domain']=='email' and src['operation']=='overview': allowed-={'query'}
        if src['domain']=='messages': allowed-={'query'} if src['operation']=='overview' else {'conversation'}
        assert set(src)-{'domain','operation'} <= allowed
        if 'time' in src:
            keys=set(src['time'])
            assert keys in [{'named'},{'date'},{'month'},{'start','end'},{'last_n_days'},{'rolling_days'}]
        for field in ['count','minutes']:
            if field in src:
                assert type(src[field]) is int and 1 <= src[field] <= (100 if field=='count' else 1440)
        if 'unread' in src: assert type(src['unread']) is bool


def build(split):
    plans = [(TIME_TRAIN if split=='train' else TIME_DEV,'time',30 if split=='train' else 10), (FILTER_TRAIN if split=='train' else FILTER_DEV,'filters',50 if split=='train' else 5), (SIMPLE_TRAIN if split=='train' else SIMPLE_DEV,'simple',50 if split=='train' else 5)]
    rows, provenance = [], []
    for families, primary, n in plans:
        for family in families:
            for i in range(n):
                text, semantic, p = simple_content(family,i,split) if primary=='simple' else make_content(family,i,split,primary,n)
                target=gold(semantic)
                validate(target)
                identifier=f'v2-arguments-{split}-{len(rows)+1:04d}'
                rows.append({'id':identifier,'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':text},{'role':'assistant','content':encoded(target),'training':True}]})
                provenance.append(dict(p,id=identifier,split=split))
    assert len(rows)==(1750 if split=='train' else 175)
    return rows, provenance


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent)
    args=parser.parse_args()
    assert not ({f[0] for f in TIME_TRAIN+FILTER_TRAIN+SIMPLE_TRAIN} & {f[0] for f in TIME_DEV+FILTER_DEV+SIMPLE_DEV})
    args.output_dir.mkdir(parents=True,exist_ok=True)
    for split in ['train','dev']:
        rows, provenance=build(split)
        for suffix, data in [('.jsonl',rows),('.provenance.jsonl',provenance)]:
            (args.output_dir/(split+suffix)).write_text(''.join(encoded(row)+'\n' for row in data),encoding='utf-8')
        counts={key:sum(p['primary_group']==key for p in provenance) for key in ['time','filters','simple']}
        print(encoded({'split':split,'rows':len(rows),'groups':counts,'families':len({p['family_id'] for p in provenance}),'surfaces_used':len({p['template_id'] for p in provenance}),'seed':SEED}))

if __name__=='__main__': main()
