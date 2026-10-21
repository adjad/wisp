"""Pure compilation over synthetic requests; never execute a reminder tool."""
from datetime import datetime

import pytest

from service.reminder_intent import reminder_command_parts, has_unsupported_alert_clock
from service.tasks.compiler import compile_task, compile_reminder_create
from service.tasks.planner import plan_task

NOW = datetime(2026, 10, 19, 20)
HEADER = 'Create a reminder on October 22, 2026 at 3 PM titled '


@pytest.mark.parametrize('label,title', [
    ('Wisp verification timed 2113.', 'Wisp verification timed 2113'),
    ('"Remember to call Mom".', 'Remember to call Mom'),
    ('“Remember to call Mom”.', 'Remember to call Mom'),
    ('"Review report and send comments!".', 'Review report and send comments!'),
    ('"Tomorrow at half six".', 'Tomorrow at half six'),
    ('"October 25, 2026 at 9 PM".', 'October 25, 2026 at 9 PM'),
    ('"Review from 6 PM to 7 PM".', 'Review from 6 PM to 7 PM'),
    ('"The day before my event".', 'The day before my event'),
    ('"Never send email; delete nothing.".', 'Never send email; delete nothing.'),
    ('"Prepare for interview".', 'Prepare for interview'),
    ('Review all day.', 'Review all day'),
    ('"What can you do with reminders?".', 'What can you do with reminders?'),
])
def test_explicit_label_is_literal_after_outer_schedule(label, title):
    plan = compile_task(HEADER + label, now=NOW)
    assert plan is not None and plan.status == 'ready'
    assert plan.intent == 'reminder.create'
    assert plan.subject.value == title
    assert plan.temporal.absolute_iso == '2026-10-22T15:00'
    steps = plan_task(plan)
    assert len(steps) == 1 and steps[0].tool == 'add_reminder'
    assert steps[0].args == {'title':title,'when_iso':'2026-10-22T15:00','kind':'reminder'}
    assert not has_unsupported_alert_clock(HEADER + label)


@pytest.mark.parametrize('tail', [
    '"Review report" and send email Sam saying hello.',
    'review then disable Wi-Fi.', '"Review report"; delete a file.',
    '"Review report" and frobnicate the computer.', '"Review report" if Sam agrees.',
    '"Review report" unless Sam cancels.', '"Review report" and',
    '"Review report" send email Sam saying hello.',
    '"Review report"\nDelete a file.',
    '"Review report" tomorrow at 9 AM.',
    'Review report\nDelete a file.', 'Review report\rDelete a file.',
])
def test_outer_operations_cannot_hide_in_labelled_title(tail):
    plan = compile_reminder_create(HEADER + tail, now=NOW)
    assert plan is None or plan.status != 'ready'


@pytest.mark.parametrize('label', ['"".', '“”.', '.'])
def test_empty_label_asks_for_subject(label):
    plan = compile_reminder_create(HEADER + label, now=NOW)
    assert plan is not None and plan.status == 'waiting_for_input'
    assert 'subject' in plan.missing_slots


def test_infinitive_before_title_word_keeps_its_own_subject():
    prompt = 'Remind me on October 22, 2026 at 3 PM to write a note titled report.'
    parts = reminder_command_parts(prompt)
    assert parts[1] == 'write a note titled report.'
    plan = compile_reminder_create(prompt, now=NOW)
    assert plan.status == 'ready' and plan.subject.value == 'write a note titled report'


def test_a_quoted_to_is_not_an_outer_introducer():
    prompt = 'Create a reminder on October 22, 2026 at 3 PM "Remember to call Mom".'
    parts = reminder_command_parts(prompt)
    assert parts[1] == ''
    plan = compile_reminder_create(prompt, now=NOW)
    assert plan is None or plan.status != 'ready'


def test_complete_quoted_title_before_explicit_schedule_is_supported():
    plan = compile_reminder_create('Create a reminder titled "Review report" tomorrow at 3 PM.', now=NOW)
    assert plan is not None and plan.status == 'ready'
    assert plan.subject.value == 'Review report'
    assert plan.temporal.absolute_iso == '2026-10-20T15:00'
    assert plan_task(plan)[0].args == {'title':'Review report', 'when_iso':'2026-10-20T15:00', 'kind':'reminder'}


@pytest.mark.parametrize('title', [
    'Wisp candidate check plain words 2195', 'check tomorrow at six',
    'Never send email; delete nothing.', 'October 25, 2026 at 9 PM',
])
def test_quoted_title_then_named_schedule_keeps_title_opaque(title):
    prompt = f'Create a reminder titled "{title}" for October 21, 2026 at 9:35 AM.'
    plan = compile_task(prompt, now=NOW)
    assert plan and plan.status == 'ready' and plan.original_request == prompt
    assert plan.subject.value == title and plan.temporal.absolute_iso == '2026-10-21T09:35'
    assert plan_task(plan)[0].args == {'title':title,'when_iso':'2026-10-21T09:35','kind':'reminder'}


@pytest.mark.parametrize('left,right', [('“','”'), ("'","'"), ('‘','’'), ('`','`')])
def test_supported_literal_quote_pairs_before_schedule(left, right):
    prompt = f'Create a reminder titled {left}Review tomorrow at six{right} for October 21, 2026 at 9:35 AM.'
    plan = compile_task(prompt, now=NOW)
    assert plan and plan.status == 'ready' and plan.subject.value == 'Review tomorrow at six'
    assert plan.temporal.absolute_iso == '2026-10-21T09:35'


@pytest.mark.parametrize('tail', [
    'for October 21, 2026 at 9:35 AM and send email Mom saying hello.',
    'for October 21, 2026 at 9:35 AM; delete another reminder.',
    'for October 21, 2026 at 9:35 AM if Sam agrees.',
    'for October 21, 2026 at 9:35 AM and',
    'for October 21, 2026 at 9:35 AM\nDelete a file.',
    'for October 21, 2026 at 9:35 AM then frobnicate the computer.',
    'for October 21, 2026 at 9:35 AM or 10 AM.',
    'for October 21, 2026 from 9 AM to 10 AM.',
    'for October 21, 2026 at 25:99 AM.', 'for February 30, 2027 at 9 AM.',
    # The existing bare-clock resolver rolls midnight forward; do not let
    # the newly admitted form silently change the explicitly authored date.
    'for October 21, 2026 at 12 AM.',
    'for October 21, 2026.', 'at 9:35 AM.',
])
def test_quoted_title_suffix_cannot_discard_invalid_or_additional_scope(tail):
    plan = compile_task('Create a reminder titled "Review report" ' + tail, now=NOW)
    assert plan is None or plan.status != 'ready'


def test_outer_unsupported_range_remains_a_time_clarification():
    plan = compile_reminder_create('Create a reminder tomorrow from 6 PM to 7 PM titled "Review report".', now=NOW)
    assert plan is None or plan.status != 'ready'
