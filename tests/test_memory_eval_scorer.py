"""Memory eval scoring stays offline and rejects citation laundering."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'test_fixtures' / 'memory_eval' / 'score.py'
SPEC = importlib.util.spec_from_file_location('memory_eval_score', SCRIPT)
scorer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scorer)


@pytest.fixture
def cases():
    return scorer.read_jsonl(SCRIPT.with_name('cases.jsonl'))


def test_self_test_scores_gold_and_deliberately_bad_file():
    result = subprocess.run([sys.executable, str(SCRIPT), '--self-test', '--json'],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    gold, bad = report['gold']['overall'], report['bad']['overall']
    assert gold['cases'] == 42
    assert gold['precision'] == gold['recall'] == gold['exact_case_accuracy'] == 1
    assert gold['unsupported_fact_rate'] == gold['forbidden_hits'] == 0
    assert bad['true_positives'] == 3
    assert bad['forbidden_hits'] == 42
    assert bad['unsupported'] == bad['predicted'] - 7
    assert bad['precision'] < 0.1 and bad['recall'] < 0.2
    assert bad['unsupported_fact_rate'] > 0.85
    assert all(scorer.perfect(row) for row in report['gold']['per_category'].values())


@pytest.mark.parametrize('mutation', ['missing', 'wrong_id', 'wrong_quote', 'unrelated', 'extra'])
def test_citations_must_match_the_fact_and_exact_source(cases, mutation):
    case = cases[0]
    answer = scorer.gold_answers([case])[0]
    fact = answer['facts'][0]
    if mutation == 'missing':
        fact['source_spans'] = []
    elif mutation == 'wrong_id':
        fact['source_spans'][0]['source_id'] = 'not-a-source'
    elif mutation == 'wrong_quote':
        fact['source_spans'][0]['quote'] = "My wife's birthday is March 4."
    elif mutation == 'unrelated':
        fact['text'] = 'I live in Cedar City.'
    else:
        fact['source_spans'].append({'source_id': 'not-a-source', 'quote': 'Extra assertion.'})
    result = scorer.score([case], [answer])['overall']
    assert result['true_positives'] == 0
    assert result['false_positives'] == result['false_negatives'] == result['unsupported'] == 1


def test_duplicate_facts_cannot_inflate_recall(cases):
    case = cases[0]
    answer = scorer.gold_answers([case])[0]
    answer['facts'].append(copy.deepcopy(answer['facts'][0]))
    result = scorer.score([case], [answer])['overall']
    assert result['true_positives'] == result['false_positives'] == 1
    assert result['precision'] == 0.5 and result['recall'] == 1
    assert result['unsupported'] == 0
    assert result['exact_case_accuracy'] == 0


def test_wrong_update_status_is_incorrect_even_with_good_evidence(cases):
    case = next(c for c in cases if c['id'] == 'correct_residence')
    answer = scorer.gold_answers([case])[0]
    answer['facts'][0]['status'] = 'active'
    answer['facts'][0]['supersedes_id'] = 21
    result = scorer.score([case], [answer])['overall']
    assert result['precision'] == result['recall'] == 0
    assert result['unsupported'] == 0


def test_missing_abstention_is_not_a_perfect_run(cases):
    case = next(c for c in cases if c['id'] == 'abstain_other_news')
    result = scorer.score([case], [])['overall']
    assert result['precision'] == result['recall'] == 1
    assert result['coverage'] == result['exact_case_accuracy'] == 0
    assert not scorer.perfect(result)
    assert scorer.perfect(scorer.score([case], [{'id': case['id'], 'facts': []}])['overall'])


@pytest.mark.parametrize('case_id', ['abstain_duplicate', 'abstain_one_off_plan',
                                    'abstain_sensitive_auto', 'abstain_forgotten_source', 'gate_no_request'])
def test_supported_but_ineligible_fact_is_not_a_hallucination(cases, case_id):
    case = next(c for c in cases if c['id'] == case_id)
    claim = case['supported_claims'][0]
    fact = dict(text=claim['text'], category='fact', status='active',
                supersedes_id=None, pattern_id=None, source_spans=claim['source_spans'])
    result = scorer.score([case], [{'id': case['id'], 'facts': [fact]}])['overall']
    assert result['precision'] == 0
    assert result['false_positives'] == result['forbidden_hits'] == 1
    assert result['unsupported'] == 0


def test_negation_and_qualifications_are_not_normalized_away(cases):
    case = next(c for c in cases if c['id'] == 'extract_qualification')
    answer = scorer.gold_answers([case])[0]
    answer['facts'][0]['text'] = 'I prefer morning meetings.'
    result = scorer.score([case], [answer])['overall']
    assert result['unsupported'] == result['forbidden_hits'] == 1
    assert scorer.normalize('I never drink coffee.') != scorer.normalize('I drink coffee.')


@pytest.mark.parametrize('invention', [' because you want to lose weight.', ' Seen 12 of 12 times.'])
def test_pattern_embellishments_are_unsupported_even_with_real_code_quote(cases, invention):
    case = next(c for c in cases if c['id'] == 'pattern_habit')
    answer = scorer.gold_answers([case])[0]
    answer['facts'][0]['text'] += invention
    result = scorer.score([case], [answer])['overall']
    assert result['unsupported'] == result['false_positives'] == result['false_negatives'] == 1


def test_text_normalization_does_not_relax_verbatim_citations(cases):
    case = cases[0]
    answer = scorer.gold_answers([case])[0]
    answer['facts'][0]['text'] = "  MY WIFE'S  BIRTHDAY IS MARCH 3.  "
    assert scorer.perfect(scorer.score([case], [answer])['overall'])
    answer['facts'][0]['source_spans'][0]['quote'] = "MY WIFE'S BIRTHDAY IS MARCH 3."
    assert scorer.score([case], [answer])['overall']['unsupported'] == 1


@pytest.mark.parametrize('mutation', ['unknown_id', 'duplicate_id', 'extra_prose', 'missing_fields', 'boolean_id'])
def test_invalid_output_is_rejected(cases, mutation):
    answers = scorer.gold_answers([cases[0]])
    if mutation == 'unknown_id':
        answers[0]['id'] = 'unknown'
    elif mutation == 'duplicate_id':
        answers.append(copy.deepcopy(answers[0]))
    elif mutation == 'extra_prose':
        answers[0]['reply'] = "Your wife's birthday is March 3."
    elif mutation == 'missing_fields':
        answers[0]['facts'][0].pop('source_spans')
    else:
        answers[0]['facts'][0]['supersedes_id'] = True
    with pytest.raises(ValueError):
        scorer.score([cases[0]], answers)


@pytest.mark.parametrize('content', ['not json\n', '{"id":"a"}\n{"id":"a"}\n'])
def test_jsonl_reader_rejects_malformed_or_duplicate_rows(tmp_path, content):
    path = tmp_path / 'invalid.jsonl'
    path.write_text(content)
    with pytest.raises(ValueError):
        scorer.read_jsonl(path)


def test_cli_exit_codes_and_gold_export(tmp_path):
    gold_path = tmp_path / 'gold.jsonl'
    export = subprocess.run([sys.executable, str(SCRIPT), '--write-gold', str(gold_path)],
                            capture_output=True, text=True, timeout=20)
    assert export.returncode == 0, export.stderr
    gold = subprocess.run([sys.executable, str(SCRIPT), str(gold_path), '--fail-on-error'],
                          capture_output=True, text=True, timeout=20)
    assert gold.returncode == 0, gold.stderr
    bad = subprocess.run([sys.executable, str(SCRIPT), str(SCRIPT.with_name('bad_answers.jsonl')), '--fail-on-error'],
                         capture_output=True, text=True, timeout=20)
    assert bad.returncode == 1, bad.stderr
    invalid_path = tmp_path / 'invalid.jsonl'
    invalid_path.write_text('{"id":"unknown","facts":[]}\n')
    invalid = subprocess.run([sys.executable, str(SCRIPT), str(invalid_path)],
                             capture_output=True, text=True, timeout=20)
    assert invalid.returncode == 2 and 'unknown answer id' in invalid.stderr


def test_label_spans_are_validated_before_scoring(cases):
    broken = copy.deepcopy(cases[:1])
    broken[0]['required_source_spans'][0]['quote'] = 'No such statement.'
    with pytest.raises(ValueError, match='exact source span'):
        scorer.score(broken, [])


def test_support_labels_cannot_cite_nonexistent_source(cases):
    broken = copy.deepcopy(cases[:1])
    broken[0]['supported_claims'][0]['source_spans'][0]['source_id'] = 'unknown'
    with pytest.raises(ValueError, match='exact source span'):
        scorer.score(broken, [])
