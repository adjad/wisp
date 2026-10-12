"""S2a deterministic metadata transitions, not thread/scheduler or LP-W proof."""

import sys
import types
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path


# Load exactly two leaf sources under a synthetic package, without service init
# or bytecode probes. Relative imports preserve the real dependency relationship.
_DIRECTORY = Path(__file__).resolve().parents[1] / "service/inference"
_PACKAGE = "_wisp_attestation_s2a"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(_DIRECTORY)]
sys.modules[_PACKAGE] = _package
for _leaf in ("attestation_lease", "attestation_store"):
    _path = _DIRECTORY / (_leaf + ".py")
    _module = types.ModuleType(_PACKAGE + "." + _leaf)
    _module.__file__ = str(_path)
    _module.__package__ = _PACKAGE
    sys.modules[_module.__name__] = _module
    exec(compile(_path.read_bytes(), str(_path), "exec"), _module.__dict__)

lease_api = sys.modules[_PACKAGE + ".attestation_lease"]
store_api = sys.modules[_PACKAGE + ".attestation_store"]
Store = store_api.AttestationStore
Limits = store_api.StoreLimits


def key(**changes):
    values = dict(uid=501, port=8123, app_root="/synthetic/app", app_executable="/synthetic/app/bin",
                  python_root="/synthetic/python", server_entry="/synthetic/server.py",
                  manifest_path="/synthetic/manifest.json", manifest_absent=True,
                  qualification_rule_revision="rule-A", policy_revision="policy-A")
    values.update(changes)
    return lease_api.QualificationKey(**values)


def clock(now=100, previous=0, domain="synthetic-inclusive"):
    return lease_api.ClockSample(domain, now, previous)


def store(**changes):
    limits = dict(key_capacity=4, transport_capacity=4, nonce_capacity=50,
                  counter_ceiling=50, record_units=4096, fact_depth=8)
    options = dict(process_lifetime="process-A", policy_revision="policy-A",
                   clock_domain_id="synthetic-inclusive")
    for field in tuple(changes):
        if field in limits:
            limits[field] = changes.pop(field)
    options.update(changes)
    result = Store(limits=Limits(**limits), **options)
    assert result.register_transport("transport-A")
    return result


def reserve(cache, which=None, now=100, deadline=300, transport="transport-A"):
    result = cache.reserve(key() if which is None else which, transport, clock(now), build_deadline_ns=deadline)
    assert result is not None and result.started
    return result.token


def lease(job, *, start=100, expiry=200, **changes):
    fields = dict(key=job.key, qualification_id="audit-" + job.unique_nonce,
                  global_generation=job.global_generation, key_generation=job.key_generation,
                  clock_domain_id="synthetic-inclusive", process_lifetime=job.process_lifetime,
                  server_facts=(("server", 101), ("incarnation", 7)),
                  parent_facts=(("parent", 100), ("incarnation", 6)),
                  boundary_fingerprints=(("tree", b"tree-A"), ("groups", (501, 20))),
                  authority_reference="authority-" + job.unique_nonce,
                  qualification_start_ns=start, expiry_ns=expiry)
    fields.update(changes)
    return lease_api.QualificationLease(**fields)


def publish_and_finish(cache, job, observation=None, now=100):
    observation = lease(job) if observation is None else observation
    assert cache.publish(job, observation, clock(now))
    assert cache.job_finished(job)
    return observation


class PublicationTests(unittest.TestCase):
    def test_original_start_and_expiry_equality(self):
        for now, accepted in ((100, True), (199, True), (200, False), (201, False)):
            with self.subTest(now=now):
                cache = store()
                job = reserve(cache)
                self.assertIs(cache.publish(job, lease(job), clock(now)), accepted)
                self.assertEqual(len(cache.status().outstanding), 1)
                self.assertTrue(cache.job_finished(job))

    def test_expiry_is_not_renewed_by_lookup_or_refresh(self):
        cache = store()
        old_job = reserve(cache)
        old = publish_and_finish(cache, old_job)
        refresh = reserve(cache, now=150)
        new = publish_and_finish(cache, refresh, lease(refresh, start=150, expiry=250), now=150)
        self.assertEqual(cache.snapshot(key()).replacement_revision, 2)
        self.assertEqual(cache.snapshot(key()).key_generation, 0)
        self.assertEqual((old.qualification_start_ns, old.expiry_ns), (100, 200))
        self.assertTrue(lease_api.lease_usable(old, cache.snapshot(key()), clock(199)))
        self.assertFalse(lease_api.lease_usable(old, cache.snapshot(key()), clock(200)))
        self.assertIs(cache.lookup(key(), clock(220)), new)
        self.assertIsNone(cache.lookup(key(), clock(250)))

    def test_changed_facts_revoke_before_fresh_generation(self):
        for field in ("server_facts", "parent_facts", "boundary_fingerprints"):
            with self.subTest(field=field):
                cache = store()
                old = publish_and_finish(cache, reserve(cache))
                refresh = reserve(cache, now=150)
                changed = lease(refresh, start=150, expiry=250, **{field: (("changed", b"B"),)})
                self.assertFalse(cache.publish(refresh, changed, clock(150)))
                self.assertEqual(cache.snapshot(key()).key_generation, 1)
                self.assertIsNone(cache.snapshot(key()).published_lease)
                self.assertFalse(cache.revoke_lease(old))
                self.assertTrue(cache.job_finished(refresh))
                fresh = reserve(cache, now=150)
                accepted = replace(changed, key_generation=fresh.key_generation,
                                   qualification_id="audit-fresh")
                self.assertTrue(cache.publish(fresh, accepted, clock(150)))

    def test_publication_binds_lease_key_policy_lifetime_domain_and_generations(self):
        for change in ({"key": key(port=8124)}, {"process_lifetime": "other-process"},
                       {"clock_domain_id": "wall-clock"}, {"global_generation": 1},
                       {"key_generation": 1}, {"key": key(policy_revision="policy-B")}):
            with self.subTest(change=change):
                cache = store()
                job = reserve(cache)
                self.assertFalse(cache.publish(job, lease(job, **change), clock()))
                self.assertIsNone(cache.snapshot(key()).published_lease)
                self.assertEqual(cache.snapshot(key()).key_generation, 1)

    def test_each_full_key_dimension_is_isolated(self):
        changes = dict(uid=502, port=8124, app_root="/synthetic/./app", app_executable="other-bin",
                       python_root="other-python", server_entry="other-server", manifest_path="other-manifest",
                       qualification_rule_revision="rule-B")
        for field, value in changes.items():
            with self.subTest(field=field):
                cache = store()
                first = reserve(cache)
                second = reserve(cache, key(**{field: value}))
                self.assertNotEqual(first.key, second.key)
                self.assertTrue(cache.fail(first))
                self.assertEqual(cache.snapshot(second.key).key_generation, 0)
                self.assertTrue(cache.publish(second, lease(second), clock()))
        cache = store()
        self.assertIsNone(cache.reserve(key(policy_revision="policy-B"), "transport-A", clock(), build_deadline_ns=300))
        self.assertIsNone(cache.reserve(key(manifest_absent=False), "transport-A", clock(), build_deadline_ns=300))


class StaleAndAccountingTests(unittest.TestCase):
    def test_same_key_join_starts_no_additional_job(self):
        cache = store()
        job = reserve(cache)
        joined = cache.reserve(key(), "transport-A", clock(110), build_deadline_ns=400)
        self.assertFalse(joined.started)
        self.assertIs(joined.token, job)
        self.assertEqual(len(cache.status().outstanding), 1)
        self.assertEqual(cache.status().issued_nonces, 1)

    def test_stale_success_failure_timeout_cancel_leave_successor_unchanged(self):
        cache = store()
        old = reserve(cache)
        self.assertTrue(cache.fail(old))
        fresh = reserve(cache, now=150)
        new_lease = publish_and_finish(cache, fresh, lease(fresh, start=150, expiry=300), now=150)
        snapshot = cache.snapshot(key())
        status = cache.status()
        self.assertFalse(cache.publish(old, lease(old), clock(100)))
        self.assertFalse(cache.fail(old))
        self.assertFalse(cache.cancel(old))
        self.assertFalse(cache.expire(old, clock(1000)))
        self.assertEqual(cache.snapshot(key()), snapshot)
        self.assertEqual(cache.status(), status)
        self.assertIs(cache.lookup(key(), clock(160)), new_lease)
        self.assertTrue(cache.job_finished(old))
        self.assertFalse(cache.job_finished(old))
        self.assertEqual(cache.snapshot(key()), snapshot)

    def test_old_stream_generation_cannot_revoke_new_publication(self):
        cache = store()
        old = publish_and_finish(cache, reserve(cache))
        self.assertTrue(cache.revoke_lease(old))
        new_job = reserve(cache, now=120)
        new = publish_and_finish(cache, new_job, lease(new_job, start=120, expiry=250), now=120)
        self.assertFalse(cache.revoke_lease(old))
        self.assertIs(cache.lookup(key(), clock(130)), new)

    def test_same_generation_old_stream_can_revoke_replacement(self):
        cache = store()
        old = publish_and_finish(cache, reserve(cache))
        refresh = reserve(cache, now=150)
        publish_and_finish(cache, refresh, lease(refresh, start=150, expiry=250), now=150)
        self.assertTrue(cache.revoke_lease(old))
        self.assertEqual(cache.snapshot(key()).replacement_revision, 2)
        self.assertEqual(cache.snapshot(key()).key_generation, 1)
        self.assertIsNone(cache.lookup(key(), clock(160)))

    def test_join_requires_live_identity_and_does_not_transfer_initiator(self):
        cache = store()
        job = reserve(cache)
        self.assertIsNone(cache.reserve(key(), "unknown", clock(), build_deadline_ns=300))
        self.assertTrue(cache.register_transport("transport-B"))
        joined = cache.reserve(key(), "transport-B", clock(), build_deadline_ns=300)
        self.assertFalse(joined.started)
        self.assertIs(joined.token, job)
        self.assertTrue(cache.close_transport("transport-B"))
        self.assertIs(cache.snapshot(key()).current_token, job)
        self.assertTrue(cache.publish(job, lease(job), clock()))

    def test_expired_published_lease_is_foreground_and_close_keeps_published_value(self):
        cache = store()
        old = publish_and_finish(cache, reserve(cache))
        self.assertTrue(cache.close_transport("transport-A"))
        self.assertIs(cache.lookup(key(), clock(150)), old)
        self.assertTrue(cache.register_transport("transport-B"))
        job = reserve(cache, now=200, transport="transport-B")
        self.assertEqual(cache.status().outstanding_refreshes, 0)
        self.assertTrue(cache.publish(job, lease(job, start=200, expiry=300), clock(200)))

    def test_timeout_retains_two_hung_jobs_and_third_refuses(self):
        cache = store()
        first = reserve(cache, deadline=120)
        self.assertTrue(cache.expire(first, clock(120)))
        second = reserve(cache, now=120, deadline=140)
        self.assertTrue(cache.expire(second, clock(140)))
        self.assertEqual(len(cache.status().outstanding), 2)
        self.assertIsNone(cache.reserve(key(), "transport-A", clock(140), build_deadline_ns=200))
        self.assertTrue(cache.job_finished(first))
        third = reserve(cache, now=140, deadline=200)
        self.assertNotEqual(third.unique_nonce, first.unique_nonce)
        self.assertEqual(len(cache.status().outstanding), 2)

    def test_reserve_retires_expired_current_token_without_forgiving_job(self):
        cache = store()
        first = reserve(cache, deadline=120)
        second = reserve(cache, now=120, deadline=200)
        self.assertEqual(second.key_generation, 1)
        self.assertEqual(len(cache.status().outstanding), 2)
        self.assertFalse(cache.publish(first, lease(first), clock(120)))

    def test_timeout_is_strict_at_deadline(self):
        cache = store()
        job = reserve(cache, deadline=120)
        self.assertFalse(cache.expire(job, clock(119)))
        self.assertTrue(cache.expire(job, clock(120)))
        self.assertFalse(cache.expire(job, clock(121)))
        self.assertEqual(len(cache.status().outstanding), 1)

    def test_close_and_cancel_do_not_free_running_jobs(self):
        cache = store()
        first = reserve(cache)
        self.assertTrue(cache.close_transport("transport-A"))
        self.assertFalse(cache.publish(first, lease(first), clock()))
        self.assertTrue(cache.register_transport("transport-B"))
        second = reserve(cache, key(port=8124), transport="transport-B")
        self.assertTrue(cache.cancel(second))
        self.assertEqual(len(cache.status().outstanding), 2)
        self.assertIsNone(cache.reserve(key(port=8125), "transport-B", clock(), build_deadline_ns=300))
        self.assertTrue(cache.job_finished(first))
        self.assertEqual(len(cache.status().outstanding), 1)

    def test_duplicate_completion_or_wrong_token_cannot_free_another_job(self):
        cache = store()
        first = reserve(cache)
        second = reserve(cache, key(port=8124))
        for changed in (replace(first, unique_nonce=second.unique_nonce),
                        replace(first, expected_revision=1), replace(first, absolute_build_deadline_ns=301)):
            with self.subTest(token=changed):
                self.assertFalse(cache.job_finished(changed))
        self.assertTrue(cache.job_finished(first))
        self.assertFalse(cache.job_finished(first))
        self.assertEqual(cache.status().outstanding, (second,))
        self.assertIs(cache.snapshot(second.key).current_token, second)

    def test_successful_publish_still_needs_explicit_finished_accounting(self):
        cache = store()
        job = reserve(cache)
        self.assertTrue(cache.publish(job, lease(job), clock()))
        self.assertFalse(cache.publish(job, lease(job), clock()))
        self.assertEqual(len(cache.status().outstanding), 1)
        self.assertTrue(cache.job_finished(job))
        self.assertFalse(cache.job_finished(job))
        self.assertEqual(cache.status().outstanding, ())

    def test_finishing_current_without_result_retires_authority(self):
        cache = store()
        job = reserve(cache)
        self.assertTrue(cache.job_finished(job))
        self.assertFalse(cache.publish(job, lease(job), clock()))
        self.assertIsNone(cache.snapshot(key()).current_token)
        self.assertEqual(cache.snapshot(key()).key_generation, 1)

    def test_refresh_uses_only_one_slot_and_foreground_recovery_remains(self):
        cache = store()
        publish_and_finish(cache, reserve(cache))
        other_key = key(port=8124)
        publish_and_finish(cache, reserve(cache, other_key))
        refresh = reserve(cache, now=150, deadline=160)
        self.assertEqual(cache.status().outstanding_refreshes, 1)
        self.assertIsNone(cache.reserve(other_key, "transport-A", clock(150), build_deadline_ns=300))
        foreground = reserve(cache, key(port=8125), now=150)
        self.assertEqual(len(cache.status().outstanding), 2)
        self.assertTrue(cache.expire(refresh, clock(160)))
        self.assertEqual(cache.status().outstanding_refreshes, 1)
        self.assertTrue(cache.job_finished(foreground))
        self.assertIsNone(cache.reserve(other_key, "transport-A", clock(170), build_deadline_ns=300))
        self.assertTrue(cache.job_finished(refresh))
        self.assertEqual(cache.status().outstanding_refreshes, 0)


class LifetimeAndBoundTests(unittest.TestCase):
    def test_close_is_synchronous_tombstone_and_cannot_revive(self):
        cache = store()
        job = reserve(cache)
        self.assertTrue(cache.close_transport("transport-A"))
        self.assertFalse(cache.close_transport("transport-A"))
        self.assertFalse(cache.register_transport("transport-A"))
        self.assertFalse(cache.publish(job, lease(job), clock()))
        self.assertIsNone(cache.reserve(key(), "transport-A", clock(), build_deadline_ns=300))
        self.assertEqual(cache.status().transport_identities, 1)

    def test_key_capacity_never_evicts_generation_history(self):
        cache = store(key_capacity=1)
        job = reserve(cache)
        self.assertTrue(cache.fail(job))
        self.assertTrue(cache.job_finished(job))
        self.assertIsNone(cache.reserve(key(port=8124), "transport-A", clock(), build_deadline_ns=300))
        self.assertIsNone(cache.snapshot(key(port=8124)))
        replacement = reserve(cache)
        self.assertEqual(replacement.key_generation, 1)
        self.assertEqual(cache.status().keys, 1)

    def test_transport_capacity_counts_closed_identities(self):
        cache = store(transport_capacity=1)
        self.assertTrue(cache.close_transport("transport-A"))
        self.assertFalse(cache.register_transport("transport-B"))
        self.assertEqual(cache.status().transport_identities, 1)
        self.assertEqual(cache.status().open_transports, 0)

    def test_nonce_exhaustion_never_reuses_settled_nonce(self):
        cache = store(nonce_capacity=1)
        job = reserve(cache)
        self.assertTrue(cache.fail(job))
        self.assertTrue(cache.job_finished(job))
        self.assertIsNone(cache.reserve(key(), "transport-A", clock(), build_deadline_ns=300))
        self.assertEqual(cache.status().issued_nonces, 1)
        self.assertEqual(cache.status().outstanding, ())

    def test_counter_exhaustion_disables_without_wrap(self):
        cache = store(counter_ceiling=1)
        first = reserve(cache)
        self.assertTrue(cache.fail(first))
        self.assertTrue(cache.job_finished(first))
        second = reserve(cache)
        self.assertTrue(cache.fail(second))
        self.assertFalse(cache.status().admitted)
        self.assertEqual(cache.status().disabled_reason, "counter-exhaustion")
        self.assertEqual(cache.status().global_generation, 1)
        self.assertEqual(cache.snapshot(key()).key_generation, 1)
        self.assertEqual(cache.status().outstanding, (second,))

    def test_replacement_revision_exhaustion_is_terminal(self):
        cache = store(counter_ceiling=1)
        publish_and_finish(cache, reserve(cache))
        job = reserve(cache, now=150)
        self.assertFalse(cache.publish(job, lease(job, start=150, expiry=250), clock(150)))
        self.assertFalse(cache.status().admitted)
        self.assertIsNone(cache.lookup(key(), clock(150)))

    def test_rollback_quarantine_manifest_disable_one_way(self):
        for reason in ("rollback", "quarantine", "manifest-transition"):
            with self.subTest(reason=reason):
                cache = store()
                old = publish_and_finish(cache, reserve(cache))
                job = reserve(cache, now=150)
                cache.disable(reason)
                status = cache.status()
                self.assertFalse(status.admitted)
                self.assertEqual(status.global_generation, 1)
                self.assertEqual(status.outstanding, (job,))
                self.assertIsNone(cache.lookup(key(), clock(150)))
                self.assertFalse(cache.publish(job, lease(job), clock(150)))
                self.assertFalse(cache.revoke_lease(old))
                self.assertFalse(cache.register_transport("transport-B"))
                self.assertIsNone(cache.reserve(key(), "transport-A", clock(150), build_deadline_ns=300))
                cache.disable(reason)
                self.assertEqual(cache.status(), status)
                self.assertTrue(cache.job_finished(job))
                self.assertFalse(hasattr(cache, "enable"))

    def test_fresh_injected_lifetime_is_independent_of_old_store(self):
        old_cache = store()
        old_job = reserve(old_cache)
        new_cache = store(process_lifetime="process-B")
        new_job = reserve(new_cache)
        self.assertFalse(new_cache.publish(old_job, lease(old_job), clock()))
        self.assertFalse(new_cache.job_finished(old_job))
        self.assertEqual(new_cache.status().outstanding, (new_job,))
        self.assertTrue(new_cache.publish(new_job, lease(new_job), clock()))


class InputAndClockTests(unittest.TestCase):
    def test_clock_domain_and_store_level_regression(self):
        cache = store()
        self.assertIsNone(cache.reserve(key(), "transport-A", clock(domain="wall"), build_deadline_ns=300))
        job = reserve(cache, now=150)
        self.assertFalse(cache.publish(job, lease(job), clock(149)))
        self.assertFalse(cache.status().admitted)
        self.assertEqual(cache.status().disabled_reason, "clock-regression")
        self.assertEqual(cache.status().outstanding, (job,))

    def test_full_token_changes_cannot_publish_retire_or_settle(self):
        cache = store()
        job = reserve(cache)
        changes = dict(process_lifetime="process-B", key=key(port=8124), global_generation=1,
                       key_generation=1, expected_revision=1, unique_nonce="forged",
                       initiating_transport_lifetime="transport-B", absolute_build_deadline_ns=301)
        before = cache.snapshot(key())
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = replace(job, **{field: value})
                self.assertFalse(cache.publish(changed, lease(job), clock()))
                self.assertFalse(cache.fail(changed))
                self.assertFalse(cache.cancel(changed))
                self.assertFalse(cache.expire(changed, clock(500)))
                self.assertFalse(cache.job_finished(changed))
                self.assertEqual(cache.snapshot(key()), before)
        policy_job = replace(job, key=key(policy_revision="policy-B"), policy_revision="policy-B")
        self.assertFalse(cache.fail(policy_job))

    def test_invalid_constructor_and_method_inputs(self):
        for field in Limits.__slots__:
            for invalid in (0, -1, True, 1.5, None):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    store(**{field: invalid})
        for options in ({"process_lifetime": []}, {"policy_revision": ""}, {"clock_domain_id": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                store(**options)
        cache = store()
        for bad in ([], {}, object(), None):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    cache.register_transport(bad)
                with self.assertRaises(ValueError):
                    cache.snapshot(bad)
                with self.assertRaises(ValueError):
                    cache.job_finished(bad)
        for deadline in (100, 99, True, 100.0, float("nan"), float("inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                cache.reserve(key(), "transport-A", clock(), build_deadline_ns=deadline)
        with self.assertRaises(ValueError):
            cache.disable("enable")

    def test_size_depth_and_alias_limits_refuse_without_reservation(self):
        cache = store(record_units=512, fact_depth=4)
        before = cache.status()
        with self.assertRaises(ValueError):
            cache.reserve(key(app_root="x" * 512), "transport-A", clock(), build_deadline_ns=300)
        self.assertEqual(cache.status(), before)
        job = reserve(cache)
        with self.assertRaises(ValueError):
            cache.publish(job, lease(job, server_facts=((((("deep",),),),),)), clock())
        with self.assertRaises(ValueError):
            lease(job, server_facts=(("mutable", []),))
        with self.assertRaises(ValueError):
            cache.publish(job, lease(job, server_facts=(b"x" * 512,)), clock())
        self.assertIs(cache.snapshot(key()).current_token, job)

    def test_public_snapshots_and_status_have_no_mutable_alias(self):
        cache = store()
        job = reserve(cache)
        snapshot = cache.snapshot(key())
        status = cache.status()
        with self.assertRaises(FrozenInstanceError):
            snapshot.key_generation = 7
        with self.assertRaises(FrozenInstanceError):
            status.global_generation = 7
        self.assertEqual(type(status.outstanding), tuple)
        self.assertTrue(cache.fail(job))
        self.assertEqual(snapshot.key_generation, 0)
        self.assertEqual(cache.snapshot(key()).key_generation, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
