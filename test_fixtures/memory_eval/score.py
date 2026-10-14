"""Offline, label-backed scoring of memory extraction and pattern statements."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unicodedata


HERE = Path(__file__).resolve().parent
FACT_KEYS = {'text', 'category', 'status', 'supersedes_id', 'pattern_id'}
CATEGORIES = {'fact', 'preference', 'person', 'project', 'routine', 'pattern'}
COUNTS = ('cases', 'answered_cases', 'exact_cases', 'expected', 'predicted',
          'true_positives', 'unsupported', 'forbidden_hits')


def normalize(text):
    return ' '.join(unicodedata.normalize('NFC', text).casefold().split())


def fact_key(fact):
    return tuple(normalize(fact[key]) if key == 'text' else fact[key]
                 for key in sorted(FACT_KEYS))


def read_jsonl(path):
    rows = []
    seen = set()
    with Path(path).open(encoding='utf-8') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'{path}:{line_no}: invalid JSON: {exc.msg}') from exc
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise ValueError(f'{path}:{line_no}: expected an object with a nonempty id')
            if row['id'] in seen:
                raise ValueError(f'{path}:{line_no}: duplicate id {row["id"]}')
            seen.add(row['id'])
            rows.append(row)
    return rows


def validate_fact(fact, *, output=False):
    keys = FACT_KEYS | {'source_spans'} if output else FACT_KEYS
    if not isinstance(fact, dict) or set(fact) != keys:
        raise ValueError(f'fact must contain exactly {sorted(keys)}')
    if not isinstance(fact['text'], str) or not normalize(fact['text']):
        raise ValueError('fact text must be nonempty')
    if fact['category'] not in CATEGORIES:
        raise ValueError('invalid fact category')
    if fact['status'] not in {'active', 'proposed', 'summary'}:
        raise ValueError('invalid fact status')
    fid = fact['supersedes_id']
    if fid is not None and (type(fid) is not int or fid < 1):
        raise ValueError('supersedes_id must be a positive integer or null')
    pid = fact['pattern_id']
    if fact['category'] == 'pattern':
        if fact['status'] != 'summary' or fid is not None or not isinstance(pid, str) or not pid:
            raise ValueError('pattern summaries need summary status, pattern_id and no supersedes_id')
    elif fact['status'] == 'summary' or pid is not None:
        raise ValueError('memory facts cannot have summary status or pattern_id')
    if output:
        if not isinstance(fact['source_spans'], list):
            raise ValueError('source_spans must be a list (empty counts as unsupported)')
        for span in fact['source_spans']:
            validate_span(span)


def validate_span(span):
    if not isinstance(span, dict) or set(span) != {'source_id', 'quote'}:
        raise ValueError('source span needs exactly source_id and quote')
    if any(not isinstance(value, str) or not value for value in span.values()):
        raise ValueError('source_id and quote must be nonempty strings')


def validate_cases(cases):
    if not cases:
        raise ValueError('case file is empty')
    for case in cases:
        if not isinstance(case.get('category'), str) or not case['category']:
            raise ValueError(f'{case["id"]}: missing category')
        inputs = case['inputs']
        if inputs['identity'] != {'name': 'Test User', 'emails': ['test.user@example.com']}:
            raise ValueError(f'{case["id"]}: unexpected identity')
        sources = inputs['sources']
        source_map = {source['id']: source['text'] for source in sources}
        if len(source_map) != len(sources):
            raise ValueError(f'{case["id"]}: duplicate source id')
        expected = case['expected_facts']
        for fact in expected:
            validate_fact(fact)
        if len({fact_key(fact) for fact in expected}) != len(expected):
            raise ValueError(f'{case["id"]}: duplicate expected fact')
        forbidden = case['forbidden_facts']
        if not isinstance(forbidden, list) or any(not isinstance(t, str) or not t for t in forbidden):
            raise ValueError(f'{case["id"]}: forbidden_facts must be nonempty strings')
        if {normalize(t) for t in forbidden} & {normalize(f['text']) for f in expected}:
            raise ValueError(f'{case["id"]}: expected fact is also forbidden')
        covered = set()
        seen_spans = set()
        for required in case['required_source_spans']:
            if set(required) != {'fact_index', 'source_id', 'quote'}:
                raise ValueError(f'{case["id"]}: invalid required span')
            index = required['fact_index']
            if type(index) is not int or not 0 <= index < len(expected):
                raise ValueError(f'{case["id"]}: invalid fact_index')
            span = {key: required[key] for key in ('source_id', 'quote')}
            validate_span(span)
            source = source_map.get(span['source_id'])
            if source is None or span['quote'] not in source:
                raise ValueError(f'{case["id"]}: required quote is not an exact source span')
            key = (index, span['source_id'], span['quote'])
            if key in seen_spans:
                raise ValueError(f'{case["id"]}: duplicate required span')
            seen_spans.add(key)
            covered.add(index)
        if covered != set(range(len(expected))):
            raise ValueError(f'{case["id"]}: each expected fact needs provenance')
        support = {}
        for claim in case['supported_claims']:
            if not isinstance(claim, dict) or set(claim) != {'text', 'source_spans'}:
                raise ValueError(f'{case["id"]}: invalid supported claim')
            if not isinstance(claim['text'], str) or not normalize(claim['text']):
                raise ValueError(f'{case["id"]}: supported claim text must be nonempty')
            text = normalize(claim['text'])
            if text in support:
                raise ValueError(f'{case["id"]}: duplicate supported claim')
            if not isinstance(claim['source_spans'], list) or not claim['source_spans']:
                raise ValueError(f'{case["id"]}: supported claim needs source spans')
            spans = set()
            for span in claim['source_spans']:
                validate_span(span)
                if span['source_id'] not in source_map or span['quote'] not in source_map[span['source_id']]:
                    raise ValueError(f'{case["id"]}: supported claim quote is not an exact source span')
                spans.add((span['source_id'], span['quote']))
            if len(spans) != len(claim['source_spans']):
                raise ValueError(f'{case["id"]}: duplicate supported claim span')
            support[text] = spans
        for index, fact in enumerate(expected):
            required = {(span['source_id'], span['quote']) for span in case['required_source_spans']
                        if span['fact_index'] == index}
            if support.get(normalize(fact['text'])) != required:
                raise ValueError(f'{case["id"]}: expected fact must have matching support labels')


def grounded(fact, case):
    # A real but unrelated quote must not launder an invented assertion.
    actual = {(span['source_id'], span['quote']) for span in fact['source_spans']}
    if not actual or len(actual) != len(fact['source_spans']):
        return False
    return any(actual == {(span['source_id'], span['quote'])
                          for span in claim['source_spans']}
               for claim in case['supported_claims']
               if normalize(claim['text']) == normalize(fact['text']))


def metrics(counts):
    tp, predicted, expected = (counts[key] for key in ('true_positives', 'predicted', 'expected'))
    return {**counts, 'false_positives': predicted - tp, 'false_negatives': expected - tp,
            'precision': tp / predicted if predicted else 1.0,
            'recall': tp / expected if expected else 1.0,
            'unsupported_fact_rate': counts['unsupported'] / predicted if predicted else 0.0,
            'coverage': counts['answered_cases'] / counts['cases'],
            'exact_case_accuracy': counts['exact_cases'] / counts['cases']}


def score(cases, answers):
    validate_cases(cases)
    by_id = {}
    known_ids = {case['id'] for case in cases}
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) != {'id', 'facts'}:
            raise ValueError('answer rows must contain exactly id and facts; prose is not scored')
        if not isinstance(answer['id'], str) or answer['id'] not in known_ids:
            raise ValueError(f'unknown answer id: {answer["id"]!r}')
        if answer['id'] in by_id:
            raise ValueError(f'duplicate answer id: {answer["id"]}')
        if not isinstance(answer['facts'], list):
            raise ValueError('facts must be a list')
        for fact in answer['facts']:
            validate_fact(fact, output=True)
        by_id[answer['id']] = answer
    totals = dict.fromkeys(COUNTS, 0)
    categories = {}
    details = []
    for case in cases:
        predicted = by_id.get(case['id'], {'facts': []})['facts']
        remaining = {fact_key(fact) for fact in case['expected_facts']}
        forbidden = {normalize(text) for text in case['forbidden_facts']}
        tp = unsupported = hits = 0
        for fact in predicted:
            supported = grounded(fact, case)
            unsupported += not supported
            hits += normalize(fact['text']) in forbidden
            key = fact_key(fact)
            if supported and key in remaining:
                remaining.remove(key)
                tp += 1
        counts = dict(cases=1, answered_cases=int(case['id'] in by_id),
                      exact_cases=int(case['id'] in by_id and tp == len(predicted) == len(case['expected_facts'])),
                      expected=len(case['expected_facts']), predicted=len(predicted),
                      true_positives=tp, unsupported=unsupported, forbidden_hits=hits)
        category = categories.setdefault(case['category'], dict.fromkeys(COUNTS, 0))
        for key in COUNTS:
            totals[key] += counts[key]
            category[key] += counts[key]
        details.append({'id': case['id'], **metrics(counts)})
    return {'overall': metrics(totals),
            'per_category': {key: metrics(value) for key, value in sorted(categories.items())},
            'cases': details}


def gold_answers(cases):
    answers = []
    for case in cases:
        facts = []
        for index, expected in enumerate(case['expected_facts']):
            spans = [{key: span[key] for key in ('source_id', 'quote')}
                     for span in case['required_source_spans'] if span['fact_index'] == index]
            facts.append({**copy.deepcopy(expected), 'source_spans': spans})
        answers.append({'id': case['id'], 'facts': facts})
    return answers


def write_jsonl(path, rows):
    with Path(path).open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def perfect(result):
    return (result['precision'] == result['recall'] == result['coverage'] ==
            result['exact_case_accuracy'] == 1.0 and
            result['unsupported_fact_rate'] == result['forbidden_hits'] == 0)


def self_test(cases, bad_path):
    with tempfile.TemporaryDirectory(prefix='wisp-memory-eval-') as directory:
        gold_path = Path(directory) / 'gold_answers.jsonl'
        write_jsonl(gold_path, gold_answers(cases))
        gold = score(cases, read_jsonl(gold_path))
    bad = score(cases, read_jsonl(bad_path))
    if not perfect(gold['overall']) or not all(perfect(r) for r in gold['per_category'].values()):
        raise ValueError('self-test: gold answers must score 100% in every category')
    if not (bad['overall']['precision'] < 1 and bad['overall']['recall'] < 1 and
            bad['overall']['unsupported_fact_rate'] > 0 and bad['overall']['forbidden_hits'] > 0):
        raise ValueError('self-test: deliberately bad answers must fail all faithfulness measures')
    return {'gold': gold, 'bad': bad}


def print_table(report):
    print('category                 cases   P       R       unsupported  forbidden  exact')
    for category, values in [('ALL', report['overall']), *report['per_category'].items()]:
        print(f'{category:24} {values["cases"]:5} {values["precision"]:7.1%} '
              f'{values["recall"]:7.1%} {values["unsupported_fact_rate"]:11.1%} '
              f'{values["forbidden_hits"]:10} {values["exact_case_accuracy"]:7.1%}')
    print(f'Answer coverage: {report["overall"]["coverage"]:.1%}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('answers', nargs='?', type=Path)
    parser.add_argument('--cases', type=Path, default=HERE / 'cases.jsonl')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--bad-answers', type=Path, default=HERE / 'bad_answers.jsonl')
    parser.add_argument('--write-gold', type=Path, help='export gold rows; never use as model inputs')
    parser.add_argument('--json', action='store_true', help='emit machine-readable metrics')
    parser.add_argument('--fail-on-error', action='store_true', help='exit 1 unless every answer is correct')
    args = parser.parse_args(argv)
    if args.self_test and args.answers:
        parser.error('score an answer file or run --self-test separately')
    if not (args.answers or args.self_test or args.write_gold):
        parser.error('provide an answer file, --self-test or --write-gold')
    try:
        cases = read_jsonl(args.cases)
        validate_cases(cases)
        if args.write_gold:
            write_jsonl(args.write_gold, gold_answers(cases))
        if args.self_test:
            result = self_test(cases, args.bad_answers)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print('Gold answers (must be 100%):')
                print_table(result['gold'])
                print('\nDeliberately bad answers:')
                print_table(result['bad'])
                print('\nSelf-test passed.')
        if args.answers:
            result = score(cases, read_jsonl(args.answers))
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print_table(result)
            if args.fail_on_error and not perfect(result['overall']):
                return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
