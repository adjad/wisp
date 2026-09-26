"""Synthetic boundary checks; no browser, user state or credentials."""
import copy
import math
import pytest
from scripts.check_browser_contracts import assert_expected, cases, check_mirrors, run_case
from service.browser.contracts import ContractViolation, negotiate, validate

BROWSER = [c for c in cases() if c.get('contract') not in
           ('ActionableItem', 'ActionProposal', 'ActionReceipt', 'ScheduledBlock', 'ExternalRecord')
           and c['operation'] not in ('proposal', 'completion')]


@pytest.mark.parametrize('case', BROWSER, ids=lambda c: c['name'])
def test_shared_browser_cases(case):
    assert_expected([case], [run_case(case)])


def test_generated_schema_and_typed_records_are_current():
    check_mirrors()


def test_validated_values_are_isolated_from_caller_mutation():
    source = next(c['payload'] for c in BROWSER if c['name'] == 'assignment_snapshot')
    result = validate('BrowserSnapshot', source)
    result['elements'][0]['target_id'] = 'mutated'
    assert source['elements'][0]['target_id'] == 'document1.button1'


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, -1, 0.25, True, '123', 9007199254740992])
def test_invalid_capture_times(value):
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['observed_at_ms'] = value
    with pytest.raises(ContractViolation):
        validate('SourceObservation', payload)


def test_wire_integral_number_accepts_equivalent_json_numeric_representations():
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['observed_at_ms'] = 1.0
    assert validate('SourceObservation', payload)['observed_at_ms'] == 1


def test_negotiation_is_symmetric_but_does_not_grant_authority():
    c = next(c for c in cases() if c['name'] == 'negotiate_common')
    assert negotiate(c['local'], c['remote']) == negotiate(c['remote'], c['local'])
    assert set(negotiate(c['local'], c['remote'])) == {'schema_version', 'capabilities'}


def test_unpaired_surrogate_is_not_a_unicode_scalar():
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['title'] = '\ud800'
    with pytest.raises(ContractViolation):
        validate('SourceObservation', payload)
