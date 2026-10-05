"""Original product-taxonomy corpus. Do not regenerate after the seal is made.

Families, rather than random rows, separate development and held-out testing.
This is deterministic authorship, not sampling historical bug logs or evals.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES = ['Avery', 'Kai', 'Mina', 'Jules', 'Noor', 'Ren', 'Sasha', 'Dee']
DATES = ['2026-10-06', '2026-11-01', '2026-03-08', '2027-01-01', '2026-12-31', '2026-02-28', '2028-02-29', '2026-10-31']
PERIODS = ['today', 'tomorrow', 'this week', 'next week', 'this month', 'next month', 'yesterday', 'last week']
CLOCKS = ['2026-10-05T12:00:00-07:00', '2026-11-01T01:30:00-07:00', '2026-03-08T01:30:00-08:00', '2026-12-31T23:30:00-08:00', '2026-01-01T00:30:00-08:00', '2026-02-28T23:30:00-08:00', '2028-02-29T12:00:00-08:00', '2026-10-31T23:30:00-07:00']


def call(name, **args):
    return {'name': name, 'args': args}


def family(name, categories, build):
    rows = []
    for i in range(8):
        row = build(i)
        row.update(id=f'{name}-{i+1:02}', family=name, categories=categories, clock=CLOCKS[i])
        row.setdefault('context', [])
        exp = row['expected']
        exp.setdefault('effects', 0)
        exp.setdefault('excluded_sources', [])
        exp.setdefault('answer_facts', {})
        answer = f'Count: {i+2}\nDirection: inbound\nCoverage: fixture-only\nReference: F-{name.upper()}-{i+1:02}'
        row.setdefault('fixture_answer', answer)
        if exp.get('calls'):
            exp.setdefault('answer_literals', [f'F-{name.upper()}-{i+1:02}'])
            row.setdefault('model_script', [{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'fixture_{j}', 'type': 'function', 'function': {'name': c['name'], 'arguments': json.dumps(c['args'])}}
                for j, c in enumerate(exp['calls'])]}])
        rows.append(row)
    return rows


def read(prompt, calls, sources, **extras):
    return dict(prompt=prompt, expected=dict(calls=calls, sources=sources), **extras)


def no_tool(prompt, **extras):
    return read(prompt, [], [], **extras)


def excluded(row, *sources):
    row['expected']['excluded_sources'] = list(sources)
    return row


def facts(row, i):
    row['expected']['answer_facts'] = {'Count': str(i+2), 'Direction': 'inbound', 'Coverage': 'fixture-only'}
    return row


def with_context(row, content, tools, reply='Synthetic preceding fixture result.'):
    row['context'] = [{'role': 'user', 'content': content}, {'role': 'assistant', 'content': reply, 'tool_digest': tools}]
    return row


def dataset():
    test = []
    add = lambda name, cats, build: test.extend(family(name, cats, build))
    add('calendar-exact', ['calendar', 'exact_date', 'dst', 'year_transition'], lambda i: read(f'Which calendar appointments do I have on {DATES[i]}?', [call('get_upcoming', period=DATES[i], calendar_only=True)], ['calendar']))
    add('calendar-range', ['calendar', 'week', 'day', 'month'], lambda i: read(f'Give me a calendar overview for {PERIODS[i]}.', [call('get_past_events', days=1 if i == 6 else 7)] if i >= 6 else [call('get_upcoming', period=PERIODS[i], calendar_only=True)], ['calendar']))
    add('calendar-rolling', ['calendar', 'count', 'duration'], lambda i: read(f'List my calendar meetings over the next {i+2} days.', [call('get_upcoming', days=i+2, calendar_only=True)], ['calendar']))
    add('calendar-filter', ['calendar', 'identity_literal'], lambda i: read(f'Which upcoming meetings mention {NAMES[i]}? Search only my calendar.', [call('get_upcoming', calendar_only=True, query=NAMES[i])], ['calendar']))
    add('calendar-past', ['calendar', 'past', 'count'], lambda i: read(f'Find calendar meetings with {NAMES[i]} in the past {i+3} days.', [call('get_past_events', days=i+3, query=NAMES[i])], ['calendar']))
    add('calendar-free', ['calendar', 'free_time', 'exact_date'], lambda i: read(f'Find a {15*(i+1)} minute free calendar slot on {DATES[i]}.', [call('find_free_time', minutes=15*(i+1), period=DATES[i])], ['calendar']))
    add('calendar-account', ['calendar', 'account_literal'], lambda i: read(f'Show appointments on my Studio-{i+1} calendar this week.', [call('get_upcoming', period='this week', calendar_only=True, account=f'Studio-{i+1}')], ['calendar']))
    add('reminders-only', ['reminders', 'excluded_source'], lambda i: excluded(read(f'Find my reminders about renewal-{i+1}; leave calendar meetings out.', [call('search_reminders', query=f'renewal-{i+1}', scope='all')], ['reminders']), 'calendar'))
    add('reminders-today', ['reminders', 'day', 'scope'], lambda i: read(f'What reminders about task-{i+1} are due today?', [call('search_reminders', query=f'task-{i+1}', scope='today')], ['reminders']))
    add('reminders-overdue', ['reminders', 'past', 'scope'], lambda i: read(f'Find overdue reminders mentioning task-{i+1}.', [call('search_reminders', query=f'task-{i+1}', scope='overdue')], ['reminders']))
    add('schedule-combined', ['multi_source', 'calendar', 'reminders'], lambda i: read(f'Show my meetings and reminders for the next {i+2} days.', [call('get_upcoming', days=i+2)], ['calendar', 'reminders']))
    add('mail-day', ['email', 'exact_date', 'dst'], lambda i: read(f'Summarize the emails received on {DATES[i]}.', [call('summarize_emails', day=DATES[i])], ['email']))
    add('mail-range', ['email', 'week', 'month'], lambda i: read(f'Give me a summary of mail for {PERIODS[i]}.', [call('summarize_emails', period=PERIODS[i])], ['email']))
    add('mail-unread', ['email', 'unread', 'count'], lambda i: read(f'Summarize my latest {i+2} unread emails.', [call('summarize_emails', count=i+2, unread=True)], ['email']))
    add('mail-literal', ['email', 'identity_literal', 'records'], lambda i: read(f'Read the email containing confirmation code ZX-{i+104}. Preserve the wording.', [call('view_emails', query=f'ZX-{i+104}')], ['email']))
    add('mail-account', ['email', 'account_literal'], lambda i: read(f'Summarize today’s emails from my Work-{i+1} account.', [call('summarize_emails', day='today', account=f'Work-{i+1}')], ['email']))
    add('mail-filter', ['email', 'query_literal'], lambda i: read(f'Find my email about receipt {i+7001}.', [call('view_emails', query=f'receipt {i+7001}')], ['email']))
    add('messages-day', ['messages', 'day', 'dst'], lambda i: read(f'Summarize my texts from {DATES[i]}.', [call('summarize_messages', day=DATES[i])], ['messages']))
    add('messages-range', ['messages', 'week', 'month'], lambda i: read(f'Catch me up on iMessages from {PERIODS[i]}.', [call('summarize_messages', period=PERIODS[i])], ['messages']))
    add('messages-person', ['messages', 'identity_literal'], lambda i: excluded(read(f'Summarize the Messages conversation with {NAMES[i]}.', [call('summarize_messages', conversation=NAMES[i])], ['messages']), 'email'))
    add('messages-records', ['messages', 'records', 'query_literal'], lambda i: read(f'Find the text that contains pickup code PK-{i+801}.', [call('view_messages', query=f'PK-{i+801}')], ['messages']))
    add('messages-count', ['messages', 'count'], lambda i: read(f'Summarize my last {i+3} text messages.', [call('summarize_messages', count=i+3)], ['messages']))
    add('notes-query', ['notes', 'query_literal'], lambda i: read(f'Find my note titled Project {NAMES[i]}.', [call('search_notes', query=f'Project {NAMES[i]}')], ['notes']))
    add('notes-exact', ['notes', 'exact_date'], lambda i: read(f'Show my notes from {DATES[i]}.', [call('search_notes', day=DATES[i])], ['notes']))
    add('notes-count', ['notes', 'count'], lambda i: read(f'Show my latest {i+2} notes.', [call('search_notes', count=i+2)], ['notes']))
    add('notes-month', ['notes', 'month', 'query_literal'], lambda i: read(f'Find notes about concept-{i+1} from this month.', [call('search_notes', query=f'concept-{i+1}', period='this month')], ['notes']))
    add('combined-private', ['multi_source', 'email', 'messages'], lambda i: read(f'Summarize email and texts for {PERIODS[i]}.', [call('summarize_emails', period=PERIODS[i]), call('summarize_messages', period=PERIODS[i])], ['email', 'messages']))
    add('mixed-public-private', ['multi_source', 'public_personal'], lambda i: read(f'Show my calendar for tomorrow and check the weather in Testville-{i+1}.', [call('get_upcoming', period='tomorrow', calendar_only=True), call('get_weather', location=f'Testville-{i+1}')], ['calendar', 'public']))
    add('exclude-calendar', ['negation', 'excluded_source'], lambda i: excluded(read(f'Only my emails from {DATES[i]}, no calendar or reminders.', [call('summarize_emails', day=DATES[i])], ['email']), 'calendar', 'reminders'))
    add('exclude-mail', ['negation', 'excluded_source'], lambda i: excluded(read(f'Only summarize my texts from {DATES[i]}, do not check email.', [call('summarize_messages', day=DATES[i])], ['messages']), 'email'))
    add('context-date', ['context_followup', 'exact_date'], lambda i: with_context(read(f'And for {DATES[i]}?', [call('summarize_emails', day=DATES[i])], ['email']), 'Summarize my emails today.', 'summarize_emails'))
    add('context-person', ['context_followup', 'identity_literal'], lambda i: with_context(read(f'Now just the conversation with {NAMES[i]}.', [call('summarize_messages', conversation=NAMES[i])], ['messages']), 'Summarize my text messages.', 'summarize_messages'))
    add('mail-typo', ['typo', 'email', 'day'], lambda i: read(f'Pls sumary my emials for {DATES[i]}.', [call('summarize_emails', day=DATES[i])], ['email']))
    add('literal-quoted-action', ['quoted_text', 'no_tool', 'negation'], lambda i: no_tool(f'Explain the phrase “send an email to {NAMES[i]}” without doing it.'))
    add('ambiguous-source', ['ambiguity', 'no_tool'], lambda i: no_tool(f'Check that thing from {NAMES[i]}.'))
    add('no-tool-writing', ['no_tool', 'draft_vs_send'], lambda i: no_tool(f'Write two possible greetings for {NAMES[i]} here in chat; do not create a draft or send anything.'))
    add('draft-only', ['draft_vs_send', 'effect', 'identity_literal'], lambda i: dict(prompt=f'Draft a text to {NAMES[i]} saying “ID-{i+401} is ready”, but do not send it.', expected={'calls': [call('draft_message', to=NAMES[i], text=f'ID-{i+401} is ready')], 'sources': [], 'effects': 1}, approve=True, tool_results={'draft_message': [f'Message draft ready for {NAMES[i]}.']}))
    add('partial-private-failure', ['partial_failure', 'multi_source', 'recovery'], lambda i: read(f'Summarize my email and text messages from {DATES[i]}.', [call('summarize_emails', day=DATES[i]), call('summarize_messages', day=DATES[i])], ['email', 'messages'], tool_results={'summarize_messages': ['(error: synthetic Messages source unavailable)']}))
    add('unavailable-read', ['unavailable_tools', 'recovery'], lambda i: read(f'Find notes matching missing-{i+1}.', [], [], unavailable_tools=['search_notes']))
    add('derived-facts', ['groundtruth', 'count', 'direction', 'coverage'], lambda i: facts(read(f'How many emails are in my last {i+2} message sample, and which direction and coverage does that sample represent?', [call('view_emails', count=i+2)], ['email']), i))

    dev = []
    dev_add = lambda name, cats, build: dev.extend(family(name, cats, build))
    dev_add('dev-social', ['no_tool'], lambda i: no_tool(['Hello there.', 'Good afternoon.', 'Thanks for the help.', 'Hi Wisp.', 'Much appreciated.', 'Good evening.', 'Thank you.', 'Hey there.'][i]))
    dev_add('dev-mail-recent', ['email'], lambda i: read(['Catch me up on my inbox.', 'What is important in my inbox?', 'Give me the recent email overview.', 'Summarize my inbox please.', 'What should I know from my mail?', 'Give me a mail digest.', 'What are my inbox highlights?', 'Summarize recent email.'][i], [call('summarize_emails')], ['email']))
    dev_add('dev-texts-recent', ['messages'], lambda i: excluded(read(['Catch me up on my texts.', 'Summarize my messages.', 'What are my iMessage highlights?', 'Give me an SMS overview.', 'Summarize recent text messages.', 'What did people text me?', 'Give me the Messages digest.', 'Show my recent text summary.'][i], [call('summarize_messages')], ['messages']), 'email'))
    dev_add('dev-calendar-simple', ['calendar'], lambda i: read(f'What appointments are on my calendar tomorrow? Question {i+1}.', [call('get_upcoming', period='tomorrow', calendar_only=True)], ['calendar']))
    dev_add('dev-notes-simple', ['notes'], lambda i: read(f'Find my note about example-{i+1}.', [call('search_notes', query=f'example-{i+1}')], ['notes']))
    dev_add('dev-reminder-search', ['reminders'], lambda i: read(f'Search reminders for sample-{i+1}.', [call('search_reminders', query=f'sample-{i+1}', scope='all')], ['reminders']))
    dev_add('dev-explicit-prohibition', ['negation', 'no_tool'], lambda i: no_tool(f'Do not open the application Sample-{i+1}.'))
    dev_add('dev-clause-mentioned', ['quoted_text', 'no_tool'], lambda i: no_tool(f'Translate “check my emails {i+1}” into Spanish.'))
    dev_add('dev-send-denied', ['draft_vs_send', 'effect'], lambda i: dict(prompt=f'Text {NAMES[i]} “Fixture-{i+1} ready”.', expected={'calls': [], 'sources': [], 'effects': 0}, approve=False, model_script=[{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'fixture_send', 'type': 'function', 'function': {'name': 'send_message', 'arguments': json.dumps({'to': NAMES[i], 'text': f'Fixture-{i+1} ready'})}}]}]))
    dev_add('dev-fixture-failure', ['failure', 'recovery'], lambda i: read(f'Summarize my email for today; sample {i+1}.', [call('summarize_emails', day='today')], ['email'], tool_results={'summarize_emails': ['(error: synthetic mail permission unavailable)']}))
    return {'dev': dev, 'test': test}


def main():
    if (HERE / 'seal.json').exists():
        raise SystemExit('Corpus already sealed. Never overwrite it for candidate tuning.')
    splits = dataset()
    families = [{row['family'] for row in rows} for rows in splits.values()]
    assert not families[0] & families[1]
    assert len({row['prompt'] for rows in splits.values() for row in rows}) == sum(map(len, splits.values()))
    manifest = {'authorship': 'independent original product taxonomy; no historical raw prompts imported',
                'sealed_at': '2026-10-05', 'base_sha': '4994caa15533c0cf84c07208c9097e4197f2815b',
                'unit': 'scenario', 'independence_unit': 'family', 'splits': {}}
    for split, rows in splits.items():
        data = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows).encode()
        (HERE / f'{split}.jsonl').write_bytes(data)
        categories = sorted({category for row in rows for category in row['categories']})
        manifest['splits'][split] = {'count': len(rows), 'families': len({row['family'] for row in rows}),
                                    'categories': categories, 'sha256': hashlib.sha256(data).hexdigest()}
    (HERE / 'seal.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest))  # counts/categories/hash only; no heldout prompt output.


if __name__ == '__main__':
    main()
