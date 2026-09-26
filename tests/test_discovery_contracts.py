"""Obligations, work blocks, external records and evidence remain distinct."""
import copy
import pytest
from scripts.check_browser_contracts import assert_expected, cases, run_case
from service.browser.contracts import ContractViolation, validate
from service.discovery.contracts import validate_completion, validate_proposal

DISCOVERY = [c for c in cases() if c.get('contract') in
             ('ActionableItem', 'ActionProposal', 'ActionReceipt', 'ScheduledBlock', 'ExternalRecord')
             or c['operation'] in ('proposal', 'completion')]


@pytest.mark.parametrize('case', DISCOVERY, ids=lambda c: c['name'])
def test_shared_discovery_cases(case):
    assert_expected([case], [run_case(case)])


@pytest.mark.parametrize('field,value', [('action_id', 'other'), ('task_id', 'other'), ('proposal_id', 'other')])
def test_verified_receipt_for_another_action_cannot_complete_item(field, value):
    c = copy.deepcopy(next(c for c in cases() if c['name'] == 'verified_completion'))
    c['receipt'][field] = value
    with pytest.raises(ContractViolation):
        validate_completion(c['item'], c['receipt'], c['proposal'])


def test_proposal_cannot_cite_unrelated_evidence():
    c = copy.deepcopy(next(c for c in cases() if c['name'] == 'grounded_proposal'))
    c['proposal']['evidence_ids'] = ['e.unrelated']
    with pytest.raises(ContractViolation):
        validate_proposal(c['item'], c['proposal'])


def test_work_block_cannot_be_decoded_as_obligation_or_external_record():
    block = next(c['payload'] for c in cases() if c['name'] == 'work_block_is_not_obligation')
    for name in ('ActionableItem', 'ExternalRecord'):
        with pytest.raises(ContractViolation):
            validate(name, block)
