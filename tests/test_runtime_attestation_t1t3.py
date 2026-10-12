"""Synchronous synthetic S1 controls; never import the service package.

Standalone unittest execution deliberately avoids pytest/conftest and production
dependencies. Repository CI may also collect these unittest cases through pytest.
Injected integers and symbolic facts qualify no real clock or native authority.
"""

import importlib.util
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path


_SOURCE = Path(__file__).resolve().parents[1] / "service/inference/attestation_lease.py"
_SPEC = importlib.util.spec_from_file_location("_wisp_attestation_lease_s1", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
attestation = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = attestation
# Compile the one source directly; do not probe a product __pycache__ path.
exec(compile(_SOURCE.read_bytes(), str(_SOURCE), "exec"), attestation.__dict__)

QualificationKey = attestation.QualificationKey
ClockSample = attestation.ClockSample
QualificationLease = attestation.QualificationLease
BuildToken = attestation.BuildToken
PublicationSnapshot = attestation.PublicationSnapshot


def key(**changes):
    fields = dict(
        uid=501, port=8123, app_root="/synthetic/Wisp.app",
        app_executable="/synthetic/Wisp.app/Wisp", python_root="/synthetic/python",
        server_entry="/synthetic/server.py", manifest_path="/synthetic/runtime.json",
        manifest_absent=True, qualification_rule_revision="rule-1", policy_revision="policy-1",
    )
    fields.update(changes)
    return QualificationKey(**fields)


def lease(**changes):
    fields = dict(
        key=key(), qualification_id="audit-A", global_generation=2, key_generation=3,
        clock_domain_id="synthetic-inclusive", process_lifetime="process-A",
        server_facts=(("pid", 101), ("incarnation", 8), ("exec", b"server")),
        parent_facts=(("pid", 100), ("incarnation", 7), ("exec", b"parent")),
        boundary_fingerprints=(("tree", b"audit-tree-A"), ("groups", (501, 20))),
        authority_reference="authority-A", qualification_start_ns=100, expiry_ns=200,
    )
    fields.update(changes)
    return QualificationLease(**fields)


def token(**changes):
    fields = dict(
        process_lifetime="process-A", key=key(), global_generation=2, key_generation=3,
        policy_revision="policy-1", expected_revision=4, unique_nonce="job-A",
        initiating_transport_lifetime="transport-A", absolute_build_deadline_ns=180,
    )
    fields.update(changes)
    return BuildToken(**fields)


def snapshot(**changes):
    fields = dict(
        key=key(), global_generation=2, key_generation=3, process_lifetime="process-A",
        policy_revision="policy-1", replacement_revision=4,
        clock_domain_id="synthetic-inclusive", current_token=token(), published_lease=None,
        open_transport_lifetimes=("transport-A",), policy_admitted=True,
        manifest_absent=True, quarantined=False,
    )
    fields.update(changes)
    return PublicationSnapshot(**fields)


def sample(now=150, previous=100, domain="synthetic-inclusive"):
    return ClockSample(domain, now, previous)


class LeaseTimeTests(unittest.TestCase):
    def test_original_start_and_strict_expiry_boundaries(self):
        original = lease()
        for now, expected in ((99, False), (100, True), (199, True), (200, False), (201, False)):
            with self.subTest(now=now):
                self.assertIs(attestation.lease_time_valid(original, sample(now, 0)), expected)

    def test_completion_and_successful_use_cannot_extend_audit_age(self):
        original = lease()
        self.assertTrue(attestation.lease_time_valid(original, sample(170)))
        self.assertTrue(attestation.lease_time_valid(original, sample(199, 170)))
        self.assertFalse(attestation.lease_time_valid(original, sample(200, 199)))
        self.assertEqual((original.qualification_start_ns, original.expiry_ns), (100, 200))

    def test_build_finishing_at_expiry_is_not_publishable(self):
        job = token(absolute_build_deadline_ns=300)
        state = snapshot(current_token=job)
        self.assertTrue(attestation.publication_eligible(job, lease(), state, sample(199)))
        self.assertFalse(attestation.publication_eligible(job, lease(), state, sample(200)))

    def test_old_lease_cannot_borrow_refresh_freshness(self):
        old = lease()
        fresh = lease(qualification_id="audit-B", qualification_start_ns=190, expiry_ns=290)
        state = snapshot(replacement_revision=5, current_token=None, published_lease=fresh)
        self.assertFalse(attestation.lease_usable(old, state, sample(220, 190)))
        self.assertTrue(attestation.lease_usable(fresh, state, sample(220, 190)))
        self.assertTrue(attestation.lease_usable(old, state, sample(199, 190)))

    def test_wrong_clock_domain_refuses(self):
        self.assertFalse(attestation.lease_time_valid(lease(), sample(domain="wall-clock")))
        self.assertFalse(attestation.publication_eligible(
            token(), lease(), snapshot(clock_domain_id="other-inclusive"), sample()))

    def test_clock_regression_and_invalid_time_inputs_refuse(self):
        with self.assertRaises(ValueError):
            sample(149, 150)
        for invalid in (-1, True, 1.0, float("nan"), float("inf"), float("-inf"), "150", None):
            with self.subTest(invalid=repr(invalid)):
                for field in ("now_ns", "previous_ns"):
                    with self.assertRaises(ValueError):
                        replace(sample(), **{field: invalid})
                for field in ("qualification_start_ns", "expiry_ns"):
                    with self.assertRaises(ValueError):
                        replace(lease(), **{field: invalid})

    def test_empty_or_reversed_lease_interval_refuses(self):
        for expiry in (99, 100):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                lease(expiry_ns=expiry)

    def test_arbitrary_precision_integer_times_have_no_float_overflow(self):
        start = 10 ** 400
        original = lease(qualification_start_ns=start, expiry_ns=start + 10)
        self.assertTrue(attestation.lease_time_valid(original, sample(start + 9, start)))
        self.assertFalse(attestation.lease_time_valid(original, sample(start + 10, start)))


class TokenPublicationTests(unittest.TestCase):
    def test_valid_initial_and_same_incarnation_refresh(self):
        job, fresh = token(), lease()
        self.assertTrue(attestation.token_owns_slot(job, snapshot()))
        self.assertTrue(attestation.publication_eligible(job, fresh, snapshot(), sample()))
        older = lease(qualification_id="older", qualification_start_ns=50, expiry_ns=120)
        self.assertTrue(attestation.publication_eligible(
            job, fresh, snapshot(published_lease=older), sample()))

    def test_each_token_dimension_is_compared_not_only_nonce(self):
        variants = {
            "process_lifetime": token(process_lifetime="process-B"),
            "key": token(key=key(port=8124)),
            "global_generation": token(global_generation=9),
            "key_generation": token(key_generation=9),
            "policy_revision": token(key=key(policy_revision="policy-2"), policy_revision="policy-2"),
            "expected_revision": token(expected_revision=5),
            "unique_nonce": token(unique_nonce="job-B"),
            "initiating_transport_lifetime": token(initiating_transport_lifetime="transport-B"),
            "absolute_build_deadline_ns": token(absolute_build_deadline_ns=181),
        }
        for dimension, changed in variants.items():
            with self.subTest(dimension=dimension):
                self.assertFalse(attestation.token_owns_slot(changed, snapshot()))
                self.assertFalse(attestation.publication_eligible(changed, lease(), snapshot(), sample()))

    def test_current_token_alone_does_not_override_store_dimensions(self):
        changes = (
            {"key": key(port=8124)}, {"global_generation": 4}, {"key_generation": 4},
            {"process_lifetime": "process-B"}, {"policy_revision": "policy-2"},
            {"replacement_revision": 5}, {"open_transport_lifetimes": ()},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(attestation.token_owns_slot(token(), snapshot(**change)))
                self.assertFalse(attestation.publication_eligible(token(), lease(), snapshot(**change), sample()))

    def test_absolute_build_deadline_is_strict_and_retirement_still_possible(self):
        state, job = snapshot(), token()
        for now, expected in ((179, True), (180, False), (181, False)):
            with self.subTest(now=now):
                self.assertIs(attestation.publication_eligible(job, lease(), state, sample(now)), expected)
                self.assertTrue(attestation.token_owns_slot(job, state))

    def test_retired_token_or_closed_initiator_cannot_publish_or_revoke(self):
        for state in (snapshot(current_token=None), snapshot(open_transport_lifetimes=())):
            with self.subTest(state=state):
                self.assertFalse(attestation.token_owns_slot(token(), state))
                self.assertFalse(attestation.publication_eligible(token(), lease(), state, sample()))

    def test_old_success_and_failure_do_not_touch_successor(self):
        old = token()
        successor_job = token(key_generation=4, unique_nonce="job-B")
        successor_lease = lease(key_generation=4, qualification_id="audit-B")
        state = snapshot(key_generation=4, current_token=successor_job, published_lease=successor_lease)
        before = state
        self.assertFalse(attestation.publication_eligible(old, lease(), state, sample()))
        # A late error/timeout uses this same exact-owner predicate, not a key-only eviction.
        self.assertFalse(attestation.token_owns_slot(old, state))
        self.assertIs(state.published_lease, successor_lease)
        self.assertEqual(state, before)
        self.assertTrue(attestation.publication_eligible(successor_job, successor_lease, state, sample()))

    def test_same_generation_new_nonce_also_defeats_old_success_and_failure(self):
        newer = token(unique_nonce="job-B")
        state = snapshot(current_token=newer)
        self.assertFalse(attestation.token_owns_slot(token(), state))
        self.assertFalse(attestation.publication_eligible(token(), lease(), state, sample()))
        self.assertTrue(attestation.token_owns_slot(newer, state))

    def test_candidate_lease_must_match_current_generations_and_context(self):
        changes = (
            {"key": key(port=8124)}, {"global_generation": 4}, {"key_generation": 4},
            {"process_lifetime": "process-B"}, {"clock_domain_id": "other-clock"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(attestation.publication_eligible(token(), lease(**change), snapshot(), sample()))

    def test_changed_facts_require_prior_revocation_not_same_generation_refresh(self):
        for field in ("server_facts", "parent_facts", "boundary_fingerprints"):
            with self.subTest(field=field):
                changed = lease(**{field: (("changed", b"B"),)})
                self.assertFalse(attestation.publication_eligible(
                    token(), changed, snapshot(published_lease=lease()), sample()))
                new_job = token(key_generation=4, unique_nonce="job-B")
                new_lease = replace(changed, key_generation=4)
                new_state = snapshot(key_generation=4, current_token=new_job, published_lease=None)
                self.assertTrue(attestation.publication_eligible(new_job, new_lease, new_state, sample()))

    def test_stale_published_lease_cannot_be_kept_in_new_generation(self):
        job = token(key_generation=4)
        state = snapshot(key_generation=4, current_token=job, published_lease=lease())
        self.assertFalse(attestation.publication_eligible(job, lease(key_generation=4), state, sample()))

    def test_typed_facts_do_not_alias_boolean_and_integer(self):
        old = lease(server_facts=(("typed", True),))
        changed = lease(server_facts=(("typed", 1),))
        self.assertEqual(old.server_facts, changed.server_facts)
        self.assertFalse(attestation.publication_eligible(
            token(), changed, snapshot(published_lease=old), sample()))


class RevocationTests(unittest.TestCase):
    def test_each_captured_generation_context_is_required(self):
        for change in (
            {"global_generation": 4}, {"key_generation": 4}, {"key": key(port=8124)},
            {"process_lifetime": "process-B"}, {"policy_revision": "policy-2"},
            {"clock_domain_id": "other-clock"},
        ):
            with self.subTest(change=change):
                self.assertFalse(attestation.lease_generation_current(lease(), snapshot(**change)))

    def test_replacement_revision_is_not_revocation_generation(self):
        self.assertTrue(attestation.lease_generation_current(lease(), snapshot(replacement_revision=99)))
        self.assertTrue(attestation.lease_usable(lease(), snapshot(replacement_revision=99), sample()))
        self.assertFalse(attestation.token_owns_slot(token(), snapshot(replacement_revision=99)))

    def test_expired_stream_may_only_revoke_its_current_generation(self):
        original = lease()
        self.assertFalse(attestation.lease_usable(original, snapshot(), sample(210)))
        self.assertTrue(attestation.lease_generation_current(original, snapshot()))
        self.assertFalse(attestation.lease_generation_current(original, snapshot(key_generation=4)))

    def test_policy_manifest_and_quarantine_block_use_and_publication(self):
        for change in ({"policy_admitted": False}, {"manifest_absent": False}, {"quarantined": True}):
            with self.subTest(change=change):
                state = snapshot(**change)
                self.assertFalse(attestation.lease_usable(lease(), state, sample()))
                self.assertFalse(attestation.publication_eligible(token(), lease(), state, sample()))
        absent_key = key(manifest_absent=False)
        job = token(key=absent_key)
        state = snapshot(key=absent_key, current_token=job)
        self.assertFalse(attestation.publication_eligible(job, lease(key=absent_key), state, sample()))


class ImmutableInputTests(unittest.TestCase):
    def test_records_are_frozen_without_instance_dict_aliases(self):
        for record, field in ((key(), "port"), (lease(), "expiry_ns"), (token(), "unique_nonce"),
                              (snapshot(), "replacement_revision"), (sample(), "now_ns")):
            with self.subTest(record=type(record).__name__):
                self.assertFalse(hasattr(record, "__dict__"))
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, field, None)

    def test_mutable_nested_facts_and_custom_equality_are_rejected(self):
        class PretendImmutable(tuple):
            pass

        class PretendScalar(str):
            pass

        invalid = ([], {}, bytearray(b"x"), (("nested", []),), (("nested", {}),),
                   (object(),), PretendImmutable((1,)), (PretendScalar("x"),), ())
        for value in invalid:
            for field in ("server_facts", "parent_facts", "boundary_fingerprints"):
                with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                    lease(**{field: value})

    def test_key_dimensions_are_exact_and_paths_are_not_normalized(self):
        changes = dict(uid=502, port=8124, app_root="/synthetic/./Wisp.app",
                       app_executable="/synthetic/other", python_root="/synthetic/other-python",
                       server_entry="/synthetic/other-server", manifest_path="/synthetic/other-manifest",
                       manifest_absent=False, qualification_rule_revision="rule-2", policy_revision="policy-2")
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(key(), key(**{field: value}))

    def test_invalid_key_token_snapshot_and_identifier_values(self):
        for change in ({"uid": True}, {"uid": -1}, {"port": 0}, {"port": 65536},
                       {"manifest_absent": 1}, {"app_root": ""}, {"policy_revision": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                key(**change)
        for change in ({"unique_nonce": ""}, {"policy_revision": "policy-2"},
                       {"absolute_build_deadline_ns": 0}, {"expected_revision": True},
                       {"key_generation": -1}, {"absolute_build_deadline_ns": float("inf")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                token(**change)
        for change in ({"open_transport_lifetimes": ["transport-A"]},
                       {"open_transport_lifetimes": ("transport-A", "transport-A")},
                       {"policy_admitted": 1}, {"quarantined": None}, {"current_token": object()},
                       {"published_lease": object()}, {"key": ()}, {"clock_domain_id": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                snapshot(**change)
        with self.assertRaises(ValueError):
            lease(authority_reference=object())
        with self.assertRaises(ValueError):
            sample(domain="")

    def test_wrong_record_types_cannot_gain_eligibility(self):
        self.assertFalse(attestation.lease_time_valid(None, sample()))
        self.assertFalse(attestation.lease_time_valid(lease(), None))
        self.assertFalse(attestation.token_owns_slot(None, snapshot()))
        self.assertFalse(attestation.token_owns_slot(token(), None))
        self.assertFalse(attestation.lease_generation_current(None, snapshot()))
        self.assertFalse(attestation.lease_generation_current(lease(), None))
        self.assertFalse(attestation.publication_eligible(None, lease(), snapshot(), sample()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
