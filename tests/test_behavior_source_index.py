"""Initial seeding using supplied fake publications; no user/source/model reads."""
from dataclasses import replace
import json

import pytest

from service.assistant.behavior_seed import Seed, Snapshot, prepare
from service.behavior.source_index import SourceIndex, SOURCES
from service.behavior.store import Store
from tests.test_message_content import live_message_publication, v3_carrier, record as message_record

NOW = 1792051200.0


def snap(source="email", n=1, *, text="A sufficiently long arbitrary passage.", current=lambda: True):
    return Snapshot(source, tuple(dict(id=str(i), text=text+str(i), source_time=NOW-i,
                                       time_basis="source_timestamp", evidence_basis="source_statement")
                                  for i in range(n)), enabled=True, available=True,
                    current=current, publication_complete=True, omitted=17,
                    requested_range=(NOW-86400, NOW), scope="cached_publication")


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "behavior.db")
    result = SourceIndex(store)
    yield result
    store.close()


def run(index, snapshots, now=NOW, **kwargs):
    return Seed(index).run(snapshots, epoch="consent-one", now=now, analysis_enabled=True,
                           analyze=lambda *_a, **_kw: {"links": []}, admit=lambda: True, **kwargs)


def test_default_off_never_analyzes_or_retains_index(index):
    run(index, [snap()])
    def prohibited(*a, **kw):
        raise AssertionError("default-off analysis was dispatched")
    result = Seed(index).run([snap()], epoch="consent-one", now=NOW, analyze=prohibited)
    assert result["state"] == "off" and result["analyzed"] == 0
    assert index.status([])["email"]["state"] == "off"
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0


def test_native_capped_messages_seed_complete_records_with_partial_coverage(index, live_message_publication):
    from service.assistant.behavior_sources import capture
    messages, clock = live_message_publication
    rows = [message_record(guid=f"synthetic-{i}", text=f"A complete arbitrary source passage {i}.") for i in range(2000)]
    messages.cache_messages(v3_carrier(rows, coverage=dict(attempted=2001, truncated=1)), True)
    assert messages.structured_messages_snapshot()["coverage"]["status"] == "partial"
    assert messages.structured_messages_snapshot_with_receipt() is None
    batch = capture({"messages": True}, now=clock[0])
    result = run(index, batch.snapshots, now=clock[0])
    info = result["sources"]["messages"]
    assert info["indexed"] == info["eligible"] == 2000
    assert info["analyzed"] == 4 and info["pending"] == 1996
    assert info["coverage"] == "partial" and info["publication_complete"] is False
    assert info["omitted"] == 1 and "never an exhaustive" in info["completion_scope"]
    assert batch.current()
    clock[0] += 1
    next_batch = capture({"messages": True}, now=clock[0])
    restarted = SourceIndex(index.store)
    resumed = run(restarted, next_batch.snapshots, now=clock[0])["sources"]["messages"]
    assert resumed["analyzed"] == 8 and resumed["pending"] == 1992


def test_one_partial_message_cannot_suppress_complete_rows_or_ground_a_quote(index, live_message_publication):
    from service.assistant.behavior_sources import capture
    messages, clock = live_message_publication
    rows = [message_record(guid=f"synthetic-{i}", text=f"Complete arbitrary passage {i}.") for i in range(3)]
    rows[1]["text"] = "Unfinished source passage must not ground work."
    messages.cache_messages(v3_carrier(rows, partial=(1,)), True)
    assert messages.structured_messages_snapshot_with_receipt() is None
    batch = capture({"messages": True}, now=clock[0])
    result = run(index, batch.snapshots, now=clock[0])["sources"]["messages"]
    assert result["eligible"] == result["analyzed"] == 2
    assert result["coverage"] == "partial" and result["omitted"] == 1
    prepared, _, _ = prepare(batch.snapshots)
    assert len(prepared) == 2 and all("Unfinished" not in r["text"] for r in prepared)


def test_batches_advance_past_newest_eight_and_survive_restart(tmp_path):
    path = tmp_path / "behavior.db"
    publication = snap(n=13)
    store = Store(path)
    index = SourceIndex(store)
    first = run(index, [publication])
    assert first["sources"]["email"]["analyzed"] == 4
    store.close()
    store = Store(path)
    try:
        index = SourceIndex(store)
        for tick in range(3):
            result = run(index, [publication], NOW+tick+1)
        info = result["sources"]["email"]
        assert info["analyzed"] == 13 and info["pending"] == 0 and info["state"] == "seeded"
        assert info["omitted"] == 17 and info["indexed"] == 13
        assert "bounded" in info["completion_scope"] and "exhaustive" in info["completion_scope"]
        assert run(index, [publication], NOW+4)["state"] == "idle"
    finally:
        store.close()


def test_refresh_does_not_restart_unchanged_record_revisions(index):
    publication = snap(n=2)
    run(index, [publication])
    refreshed = replace(publication, current=lambda: True)
    result = run(index, [refreshed], NOW+10)
    assert result["analyzed"] == 0 and result["sources"]["email"]["analyzed"] == 2
    changed = replace(publication, records=({**publication.records[0], "text": "Changed contents, not the former passage."}, publication.records[1]))
    result = run(index, [changed], NOW+20)
    assert result["analyzed"] == 1 and result["sources"]["email"]["analyzed"] == 2


def test_long_records_advance_passages_instead_of_marking_unread_tail_analyzed(index):
    text = "Beginning of the record. " + "x"*5000 + " A later relevant statement."
    publication = snap(text=text)
    result = run(index, [publication])
    assert result["sources"]["email"]["analyzed"] == 0
    assert 0 < result["sources"]["email"]["analyzed_characters"] < len(text)
    result = run(index, [publication], NOW+1)
    assert result["sources"]["email"]["analyzed"] == 0
    for tick in range(2, 10):
        result = run(index, [publication], NOW+tick)
    assert result["sources"]["email"]["analyzed"] == 1
    assert result["sources"]["email"]["analyzed_characters"] == len(text)+1


def test_all_five_sources_get_turns_with_four_anchor_budget(index):
    snapshots = [snap(source=s, n=8, text="Disjoint long passage "+s) for s in SOURCES]
    result = run(index, snapshots)
    result = run(index, snapshots, NOW+1)
    assert all(result["sources"][s]["analyzed"] > 0 for s in SOURCES)


def test_recent_and_old_backlog_share_turns_across_accounts(index):
    raw = []
    for account in ("one","two"):
        raw += [dict(id=f"{account}-{i}",text=f"Arbitrary complete passage {account} {i}.",
            source_time=None,time_basis="unknown",ordering_at=NOW-10000+i,
            reference={"provider":"gmail","account_id":account}) for i in range(8)]
    old_ids = {r["id"] for r in prepare([Snapshot("email",tuple(raw),enabled=True,available=True,
                        current=lambda:True)])[0]}
    selected = []
    for turn in range(8):
        for account in ("one","two"):
            raw.append(dict(id=f"recent-{account}-{turn}",text=f"A newly arrived complete passage {account} {turn}.",
                source_time=None,time_basis="unknown",ordering_at=NOW+turn,
                reference={"provider":"gmail","account_id":account}))
        def analyze(context,**kwargs):
            selected.append(context)
            return {"links":[]}
        Seed(index).run([Snapshot("email",tuple(raw),enabled=True,available=True,current=lambda:True)],
            epoch="consent-one",now=NOW+turn*30,analysis_enabled=True,analyze=analyze,admit=lambda:True)
        assert len(selected[-1]) <= 4
        assert {r["reference"]["account_id"] for r in prepare([Snapshot("email",tuple(raw),
            enabled=True,available=True,current=lambda:True)])[0] if r["id"] in {c["id"] for c in selected[-1]}} == {"one","two"}
        if turn == 0:
            assert sum("newly arrived" in r["text"] for r in selected[-1]) == 2
        assert all(r["source_time"] is None for r in selected[-1])
        assert all("ordering_at" not in r for r in selected[-1])
    assert old_ids <= {r["id"] for batch in selected for r in batch}


def test_byte_capped_seed_moves_past_already_analyzed_input_prefix(index,monkeypatch):
    from service.assistant import behavior_seed
    monkeypatch.setattr(behavior_seed,"MAX_BYTES",500)
    publication = snap(n=8,text="A bounded complete passage with enough text to fill the small test budget. ")
    all_records = []
    for raw in publication.records:
        all_records += prepare([replace(publication,records=(raw,))])[0]
    seen = set()
    def analyze(context,**kwargs):
        seen.update(r["id"] for r in context)
        return {"links":[]}
    for turn in range(8):
        Seed(index).run([publication],epoch="consent-one",now=NOW+turn*30,analysis_enabled=True,
            analyze=analyze,admit=lambda:True)
    assert seen == {r["id"] for r in all_records}


@pytest.mark.parametrize("bad", [None, {}, {"links": {}}, {"actions": []}, {"links": [{"action": "send"}]}])
def test_invalid_analysis_is_deferred_not_counted_as_analyzed(index, bad):
    seed = Seed(index)
    result = seed.run([snap()], epoch="consent-one", now=NOW, analysis_enabled=True,
                      analyze=lambda *a, **k: bad, admit=lambda: True)
    info = result["sources"]["email"]
    assert result["state"] == "deferred" and info["analyzed"] == 0 and info["deferred"] == 1
    assert run(index, [snap()], NOW+10)["analyzed"] == 0
    assert run(index, [snap()], NOW+301)["analyzed"] == 1


def test_epoch_change_and_explicit_forget_cannot_reuse_checkpoints(index):
    run(index, [snap()])
    assert index.status(prepare([snap()])[0])["email"]["analyzed"] == 1
    index.reset("consent-two")
    assert index.status([])["email"]["indexed"] == 0
    assert index.edges() == []
    result = run(index, [snap()], NOW+1)
    assert result["analyzed"] == 1


def test_revoke_during_analysis_never_resurrects_checkpoint(index):
    def analyze(*a, **k):
        index.revoke("email")
        return {"links": []}
    result = Seed(index).run([snap()], epoch="consent-one", now=NOW, analysis_enabled=True,
                            analyze=analyze, admit=lambda: True)
    assert result["state"] == "cancelled"
    assert index.status([])["email"]["indexed"] == 0


def test_receipt_replaced_during_analysis_never_advances(index):
    live = [True]
    publication = snap(current=lambda: live[0])
    def analyze(*a, **k):
        live[0] = False
        return {"links": []}
    result = Seed(index).run([publication], epoch="consent-one", now=NOW, analysis_enabled=True,
                            analyze=analyze, admit=lambda: True)
    assert result["state"] == "cancelled" and result["sources"] == {}
    assert index.store._conn.execute("SELECT sum(covered) FROM behavior_seed_records").fetchone()[0] == 0


def test_unavailable_or_off_source_has_no_currently_analyzed_claim(index):
    run(index, [snap()])
    result = run(index, [replace(snap(), available=False)], NOW+1)
    assert result["sources"]["email"]["state"] == "unavailable"
    assert result["sources"]["email"]["indexed"] == 0
    result = run(index, [Snapshot("email")], NOW+2)
    assert result["sources"]["email"]["state"] == "off"
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0


def test_bounded_publication_is_not_exhaustive_and_preserves_declared_vs_observed(index, monkeypatch):
    import service.assistant.behavior_seed as module
    monkeypatch.setattr(module, "MAX_SOURCE_RECORDS", 2)
    publication = replace(snap(n=5), publication_complete=False, declared_range=(NOW-180*86400, NOW+86400))
    result = run(index, [publication])
    info = result["sources"]["email"]
    assert (info["seen"], info["eligible"], info["omitted"], info["publication_complete"]) == (5,2,20,False)
    assert info["requested_range"] == [NOW-86400, NOW]
    assert info["observed_range"] == [NOW-1, NOW]
    assert info["declared_range"] == list(publication.declared_range)
    assert info["scope"] == "cached_publication"


def test_conflicting_duplicate_ids_are_withheld_and_not_chosen_by_order():
    p = snap()
    duplicate = replace(p, records=(p.records[0], {**p.records[0], "text": "A conflicting second version."}))
    records, detail, _ = prepare([duplicate])
    assert records == [] and detail["email"]["skipped"] == 2 and detail["email"]["eligible"] == 0


def test_note_modification_time_does_not_anchor_relative_prose():
    records, detail, _ = prepare([snap("notes", text="Finish this tomorrow.")])
    assert records[0]["source_time"] is None and records[0]["time_basis"] == "unknown"
    assert detail["notes"]["observed_range"] is None


def test_source_bytes_cannot_starve_other_sources(monkeypatch):
    import service.assistant.behavior_seed as module
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 100)
    records, detail, _ = prepare([snap("email", 10, text="a"*60), snap("notes", text="An independent note passage.")])
    assert detail["email"]["omitted"] == 26
    assert any(r["source"] == "notes" for r in records)


def test_no_source_text_titles_or_quotes_are_persisted(index):
    secret = "NEVER_DUPLICATE_THIS_PRIVATE_PASSAGE"
    run(index, [snap(text=secret)])
    dump = "\n".join(index.store._conn.iterdump())
    assert secret not in dump
    assert "Subject" not in dump
    assert index.store._conn.execute("SELECT chars FROM behavior_seed_records").fetchone()[0] == len(secret)+1


def test_checkpoint_retention_is_bounded(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_RECORDS", 3)
    result = run(index, [snap(n=5)])
    assert result["sources"]["email"]["not_indexed"] == 2
    assert result["sources"]["email"]["coverage"] == "partial"
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 3
    index.prune(now=NOW+181*86400)
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0


def _work(record, deadline, ident=None, ownership="user"):
    from service.behavior.associations import digest
    quote = record["text"][:800]
    return dict(id=ident or "work:"+record["id"],
                endpoint=dict(record_id=record["id"], source=record["source"], revision=record["revision"],
                              start=0, end=len(quote), sha256=digest(quote)),
                kind="task", ownership=ownership, due_ts=None,
                due_range=[deadline, deadline+86400] if deadline is not None else None,
                deadline_precision="date" if deadline is not None else "unknown", analyzed_at=NOW)


def _finish(index, records, *, now=NOW, work=()):
    anchors = index.select(records, now=now)
    assert index.finish(anchors, records, [], epoch="consent-one", now=now,
                        admit=lambda: True, work=work)
    return anchors


def _prepared_case(index):
    from service.publication_authority import Authority, Clock, Witness
    records, manifests, _ = prepare([snap(), snap("messages"), snap("calendar")])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    anchors = index.select(records, now=NOW)
    authority = Authority("synthetic-prepared-source")
    authority.publish()
    clock = Clock(live=False)
    clock.observe(NOW,100)
    witness = Witness.combine(authority.token(clock=clock,expires_wall=NOW+10,expires_monotonic=110))
    a,b,c = records
    links = [dict(relation="possibly_related", left_id=a["id"], right_id=r["id"],
                  left_quote=a["text"], right_quote=r["text"]) for r in (b,c)]
    return records,anchors,links,authority,clock,witness


def _seed_rows(index):
    return {table:[tuple(row) for row in index.store._conn.execute("SELECT * FROM "+table+" ORDER BY 1")]
            for table in ("meta","behavior_seed_sources","behavior_seed_records","behavior_seed_links",
                          "behavior_seed_work","behavior_seed_capacity")}


def test_prepared_finish_commits_progress_and_derivations_visible_to_another_connection(index,monkeypatch):
    import sqlite3
    from service import config
    from service.publication_authority import publication
    records,anchors,links,_,_,witness = _prepared_case(index)
    traced = []
    index.store._conn.set_trace_callback(traced.append)
    def forbidden(*args,**kwargs):
        raise AssertionError("Full admission/config was called inside prepared publication")
    monkeypatch.setattr(config,"models_config",forbidden)
    with publication(witness) as lease:
        assert index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,
                                     work=[_work(records[0],NOW+86400)],lease=lease)
        assert lease.current() and not index.store._conn.in_transaction
    assert any(sql == "BEGIN IMMEDIATE" for sql in traced) and "COMMIT" in traced
    # The preexisting trace observer survives unchanged; no handler is installed.
    count = len(traced)
    path = index.store._conn.execute("PRAGMA database_list").fetchone()[2]
    assert len(traced) == count+1
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT count(*) FROM behavior_seed_work").fetchone()[0] == 1
        assert reader.execute("SELECT count(*) FROM behavior_seed_links").fetchone()[0] == 2
        assert reader.execute("SELECT count(*) FROM behavior_seed_records WHERE state='analyzed'").fetchone()[0] == 3


def test_prepared_finish_refuses_outer_transaction_without_committing_or_rolling_it_back(index):
    import sqlite3
    from service.publication_authority import publication
    from service.behavior.source_index import CallerTransactionActive
    records,anchors,links,_,_,witness = _prepared_case(index)
    conn = index.store._conn
    conn.execute("INSERT INTO meta VALUES ('unrelated-outer-row','true')")
    before = _seed_rows(index)
    trace = []
    conn.set_trace_callback(trace.append)
    with publication(witness) as lease:
        with pytest.raises(CallerTransactionActive,match="not applied"):
            index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,lease=lease)
    assert trace == [] and conn.in_transaction and _seed_rows(index) == before
    conn.commit()  # Only the actual caller can commit its unrelated work.
    with sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2]) as reader:
        assert reader.execute("SELECT value FROM meta WHERE key='unrelated-outer-row'").fetchone()[0] == "true"
        assert reader.execute("SELECT count(*) FROM behavior_seed_links").fetchone()[0] == 0


def test_prepared_finish_rolls_back_progress_links_work_loss_trim_and_capacity_on_expiry(index,monkeypatch):
    from service.publication_authority import publication
    import service.behavior.source_index as module
    records,anchors,links,_,clock,witness = _prepared_case(index)
    monkeypatch.setattr(module,"MAX_EDGES",1)
    # Retained expired work must be restored too, along with its loss counter.
    old = _work(records[0],NOW-86400,ident="prior-expired-work")
    with index.store._conn:
        index.store._conn.execute("INSERT INTO behavior_seed_work VALUES (?,?,?)",(old["id"],json.dumps(old),NOW-2*86400))
    before = _seed_rows(index)
    trims = []
    original = index._trim
    def trim(kind,now):
        original(kind,now)
        trims.append(kind)
        if kind == "links":
            assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_capacity").fetchone()[0] > 0
            assert index.store.meta_get("behavior_seed_loss_counts") != {}
            clock.observe(NOW+10,110)
    monkeypatch.setattr(index,"_trim",trim)
    with publication(witness) as lease:
        assert not index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,
                    lease=lease,work=[_work(r,NOW+(i+1)*86400) for i,r in enumerate(records)])
    assert trims == ["work","links"] and not index.store._conn.in_transaction
    assert _seed_rows(index) == before


def test_prepared_finish_rolls_back_own_transaction_after_sql_failure(index):
    import sqlite3
    from service.publication_authority import publication
    records,anchors,links,_,_,witness = _prepared_case(index)
    before = _seed_rows(index)
    index.store._conn.execute("CREATE TEMP TRIGGER reject_work BEFORE INSERT ON behavior_seed_work "
                             "BEGIN SELECT RAISE(ABORT,'synthetic-write-failure'); END")
    with publication(witness) as lease:
        with pytest.raises(sqlite3.IntegrityError,match="synthetic-write-failure"):
            index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,
                                 work=[_work(records[0],NOW+86400)],lease=lease)
    assert not index.store._conn.in_transaction and _seed_rows(index) == before


def test_prepared_finish_rejects_missing_stale_or_closed_publication_lease(index):
    from service.publication_authority import publication
    records,anchors,links,authority,_,witness = _prepared_case(index)
    before = _seed_rows(index)
    assert not index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,lease=None)
    with publication(witness) as lease:
        pass
    assert not index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,lease=lease)
    authority.withdraw()
    assert not index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,lease=lease)
    assert _seed_rows(index) == before and not index.store._conn.in_transaction


@pytest.mark.parametrize("boundary",["INSERT INTO behavior_seed_work","COMMIT"])
def test_prepared_finish_reentrant_source_withdrawal_is_explicitly_not_applied(index,boundary):
    from service.publication_authority import PublicationBusy,publication
    records,anchors,links,authority,_,witness = _prepared_case(index)
    attempts = []
    def observe(sql):
        if sql.startswith(boundary) and not attempts:
            try:
                authority.withdraw()
            except PublicationBusy as error:
                attempts.append(error)
    index.store._conn.set_trace_callback(observe)
    with publication(witness) as lease:
        assert index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,
                                     work=[_work(records[0],NOW+86400)],lease=lease)
        assert len(attempts) == 1 and witness.current() and lease.current()
    assert "not applied" in str(attempts[0]) and len(index.work()) == 1
    authority.withdraw()  # A real retry outside publication is applied.
    assert not witness.current()


def test_prepared_finish_commits_before_concurrent_source_withdrawal_acknowledgement(index):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from service.publication_authority import publication
    records,anchors,links,authority,_,witness = _prepared_case(index)
    attempted,acknowledged = threading.Event(),threading.Event()
    tasks = []
    def revoke():
        attempted.set()
        authority.withdraw()
        acknowledged.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def observe(sql):
            if sql.startswith("INSERT INTO behavior_seed_work") and not tasks:
                tasks.append(pool.submit(revoke))
                assert attempted.wait(3) and not acknowledged.is_set()
        index.store._conn.set_trace_callback(observe)
        with publication(witness) as lease:
            assert index.finish_prepared(anchors,records,links,epoch="consent-one",now=NOW,
                                        work=[_work(records[0],NOW+86400)],lease=lease)
            assert len(tasks) == 1 and attempted.is_set() and not acknowledged.is_set()
            assert not index.store._conn.in_transaction and lease.current()
        tasks[0].result(timeout=3)
    assert acknowledged.is_set() and not witness.current() and len(index.work()) == 1


def test_seed_prepared_path_checks_full_admission_before_application_lease_and_runtime_gate(index):
    from contextlib import contextmanager
    from service.publication_authority import Authority,Witness,PublicationBusy,revocation
    authority = Authority("synthetic-seed-source")
    authority.publish()
    witness = Witness.combine(authority.token())
    held = []
    prepared = []
    @contextmanager
    def gate():
        # Acquiring the Runtime gate follows the outer publication lease.
        with pytest.raises(PublicationBusy):
            with revocation():
                raise AssertionError("Outer application lease was missing")
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    def full_check():
        assert not held, "Full admission was called inside the Runtime publication gate"
        # A cooperating mutation is allowed during preparation, proving this
        # full check is not being disguised as a pure memory witness.
        with revocation():
            return witness.current()
    def prepare_witness():
        assert not held
        with revocation():
            prepared.append(True)
        return witness
    result = Seed(index).run([snap(current=full_check)],epoch="consent-one",now=NOW,
        analysis_enabled=True,analyze=lambda *args,**kwargs:{"links":[]},admit=full_check,
        publication=gate,prepare_publication=prepare_witness)
    assert result["state"] == "advanced" and result["analyzed"] == 1 and len(prepared) == 2
    assert index.status(prepare([snap()])[0])["email"]["analyzed"] == 1


def test_seed_prepared_path_cannot_publish_after_acknowledged_withdrawal_during_analysis(index):
    from service.publication_authority import Authority,Witness
    authority = Authority("synthetic-seed-source")
    authority.publish()
    witness = Witness.combine(authority.token())
    def analyze(*args,**kwargs):
        authority.withdraw()
        return {"links":[]}
    result = Seed(index).run([snap(current=witness.current)],epoch="consent-one",now=NOW,
        analysis_enabled=True,analyze=analyze,admit=lambda:True,prepare_publication=lambda:witness)
    assert result["state"] == "cancelled" and not witness.current()
    assert index.store._conn.execute("SELECT sum(covered),sum(attempts) FROM behavior_seed_records").fetchone()[:] == (0,0)
    assert index.work() == index.edges() == [] and not index.store._conn.in_transaction


def test_seed_prepared_path_missing_stamp_refuses_before_any_indexing(index):
    result = Seed(index).run([snap()],epoch="consent-one",now=NOW,analysis_enabled=True,
        analyze=lambda *args,**kwargs:{"links":[]},admit=lambda:True,prepare_publication=lambda:None)
    assert result["state"] == "unavailable"
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0


def test_aged_derivations_reopen_still_current_exact_revision(index):
    records, manifests, _ = prepare([snap(n=1)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    saved = _work(records[0], NOW+400*86400)
    _finish(index, records, work=[saved])
    later = NOW+181*86400
    index.sync(records, manifests, epoch="consent-one", now=later)
    index.prune(now=later)
    assert index.work() == []
    anchors = index.select(records, now=later)
    assert len(anchors) == 1 and anchors[0]["text_start"] == 0
    assert index.status(records)["email"]["expired_derivations"] == 1
    assert index.finish(anchors, records, [], epoch="consent-one", now=later,
                        admit=lambda: True, work=[dict(saved, analyzed_at=later)])
    index.prune(now=later+1)
    assert len(index.work()) == 1 and index.select(records, now=later+1) == []


def test_capacity_keeps_nearest_grounded_ranges_and_reports_loss_without_churn(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 2)
    records, manifests, _ = prepare([snap(n=4)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    deadlines = [NOW+86400, NOW+2*86400, NOW+90*86400, None]
    # Earlier urgent work must survive later analysis of remote or unknown dates.
    for n, (record, deadline) in enumerate(zip(records, deadlines)):
        anchor = dict(record, text_start=0)
        assert index.finish([anchor], records, [], epoch="consent-one", now=NOW+n,
                            admit=lambda: True, work=[_work(record, deadline)])
    assert {w["endpoint"]["record_id"] for w in index.work()} == {r["id"] for r in records[:2]}
    info = index.status(records)["email"]
    assert info["state"] == "capacity_limited" and info["coverage"] == "partial"
    assert info["capacity_deferred"] == 2 and info["derived_omitted_records"] == 2
    for n in range(5):
        index.sync(records, manifests, epoch="consent-one", now=NOW+10+n)
        assert index.select(records, now=NOW+10+n) == []


def test_capacity_freeing_retries_a_bounded_record(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap(n=2)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    for n, record in enumerate(records):
        assert index.finish([dict(record, text_start=0)], records, [], epoch="consent-one", now=NOW+n,
                            admit=lambda: True, work=[_work(record, NOW+(n+1)*86400)])
    assert index.select(records, now=NOW+2) == []
    index.store._conn.execute("DELETE FROM behavior_seed_work")
    retry = index.select(records, now=NOW+3)
    assert len(retry) == 1 and retry[0]["id"] == records[1]["id"] and retry[0]["text_start"] == 0
    assert index.finish(retry, records, [], epoch="consent-one", now=NOW+3,
                        admit=lambda: True, work=[_work(records[1], NOW+2*86400)])
    assert index.select(records, now=NOW+4) == []


def test_initial_two_thousand_record_seed_keeps_imminent_work(index):
    records, manifests, _ = prepare([replace(snap(n=2000), omitted=0)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    for n in range(0, len(records), 4):
        chunk = records[n:n+4]
        work = [_work(r, NOW+(i+1)*86400 if i < 1000 else None)
                for i, r in enumerate(records[n:n+4], start=n)]
        assert index.finish([dict(r, text_start=0) for r in chunk], records, [],
                            epoch="consent-one", now=NOW+n/10000, admit=lambda: True, work=work)
    assert len(index.work()) == 1000
    assert {w["endpoint"]["record_id"] for w in index.work()} == {r["id"] for r in records[:1000]}
    assert index.status(records)["email"]["capacity_deferred"] == 1000
    assert index.select(records, now=NOW+1) == []


def test_capacity_eviction_preserves_unfinished_long_record_passage(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap(), snap("messages", text="Long statement. "+"x"*5000)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    short, long = records
    assert index.finish([dict(short, text_start=0)], records, [], epoch="consent-one", now=NOW,
                        admit=lambda: True, work=[_work(short, NOW+86400)])
    passage = index.select(records, now=NOW+1)
    assert len(passage) == 1 and passage[0]["id"] == long["id"]
    assert index.finish(passage, records, [], epoch="consent-one", now=NOW+1,
                        admit=lambda: True, work=[_work(long, NOW+2*86400)])
    cursor = index.store._conn.execute("SELECT cursor FROM behavior_seed_records WHERE id=?", (long["id"],)).fetchone()[0]
    following = index.select(records, now=NOW+2)
    assert len(following) == 1 and following[0]["text_start"] == cursor > 0


def test_passed_range_releases_capacity_without_claiming_completion(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap(n=3)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    for n, record in enumerate(records):
        assert index.finish([dict(record, text_start=0)], records, [], epoch="consent-one", now=NOW+n,
                            admit=lambda: True, work=[_work(record, NOW+(n+1)*86400)])
    assert index.select(records, now=NOW+10) == []
    # The first date range's exclusive end has passed. Only one free slot
    # releases one old capacity-deferred record, never all victims at once.
    later = NOW+2*86400
    anchors = index.select(records, now=later)
    assert len(anchors) == 1
    assert index.status(records)["email"]["capacity_deferred"] == 1
    assert anchors[0]["source_time"] == next(r["source_time"] for r in records if r["id"] == anchors[0]["id"])
    index.reset("other-consent")
    assert not index.finish(anchors, records, [], epoch="consent-one", now=later,
                            admit=lambda: True, work=[_work(anchors[0], later+86400)])
    assert index.work() == []


def test_capacity_retry_needs_current_bytes_and_revocation_purges_markers(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap(n=2)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    for n, record in enumerate(records):
        assert index.finish([dict(record, text_start=0)], records, [], epoch="consent-one", now=NOW+n,
                            admit=lambda: True, work=[_work(record, NOW+(n+1)*86400)])
    index.store._conn.execute("DELETE FROM behavior_seed_work")
    assert index.select([], now=NOW+2) == []
    assert index.status(records)["email"]["capacity_deferred"] == 1
    index.revoke("email")
    assert index.select(records, now=NOW+3) == []
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_capacity").fetchone()[0] == 0


def test_multiple_expired_demands_never_generate_capacity_retry_churn(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap()])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    work = [_work(records[0], NOW-3*86400, "old:1"), _work(records[0], NOW-2*86400, "old:2")]
    _finish(index, records, work=work)
    assert index.work() == []
    for offset in range(1, 6):
        index.sync(records, manifests, epoch="consent-one", now=NOW+offset)
        assert index.select(records, now=NOW+offset) == []
    assert index.status(records)["email"]["capacity_deferred"] == 0


def test_full_link_capacity_does_not_block_independent_work_recovery(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    records, manifests, _ = prepare([snap(), snap("messages"), snap("calendar")])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    a, b, c = records
    proposals = [dict(relation="possibly_related", left_id=a["id"], right_id=r["id"],
                      left_quote=a["text"], right_quote=r["text"]) for r in (b, c)]
    assert index.finish([dict(r, text_start=0) for r in records], records, proposals,
                        epoch="consent-one", now=NOW, admit=lambda: True,
                        work=[_work(a, NOW+2*86400), _work(b, NOW+86400)])
    assert {r[0] for r in index.store._conn.execute("SELECT kind FROM behavior_seed_capacity WHERE record_id=?", (a["id"],))} == {"work", "links"}
    index.store._conn.execute("DELETE FROM behavior_seed_work")
    retry = index.select(records, now=NOW+1)
    assert len(retry) == 1 and retry[0]["id"] == a["id"]
    assert index.finish(retry, records, [], epoch="consent-one", now=NOW+1,
                        admit=lambda: True, work=[_work(a, NOW+2*86400)])
    assert len(index.edges()) == 1 and index.work()[0]["endpoint"]["record_id"] == a["id"]
    assert index.select(records, now=NOW+2) == []


def test_retry_slot_is_reserved_across_long_passages_and_released_if_bytes_disappear(index, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    publication = replace(snap(n=4), records=tuple(dict(r, text=r["text"]+("x"*5000 if n else ""))
                                                 for n, r in enumerate(snap(n=4).records)))
    records, manifests, _ = prepare([publication])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    by_id = {r["id"]: r for r in records}
    for tick in range(12):
        anchors = index.select(records, now=NOW+tick)
        if not anchors:
            break
        work = [_work(by_id[r["id"]], NOW+(records.index(by_id[r["id"]])+1)*86400)
                for r in anchors if r["text_start"] == 0]
        assert index.finish(anchors, records, [], epoch="consent-one", now=NOW+tick,
                            admit=lambda: True, work=work)
    assert index.status(records)["email"]["capacity_deferred"] == 3
    index.store._conn.execute("DELETE FROM behavior_seed_work")
    first = index.select(records, now=NOW+20, limit=1)
    assert len(first) == 1 and first[0]["text_start"] == 0
    assert index.finish(first, records, [], epoch="consent-one", now=NOW+20, admit=lambda: True)
    next_passage = index.select(records, now=NOW+21)
    assert len(next_passage) == 1 and next_passage[0]["id"] == first[0]["id"]
    assert next_passage[0]["text_start"] > 0
    assert index.status(records)["email"]["capacity_retrying"] == 1
    remaining = [r for r in records if r["id"] != first[0]["id"]]
    replacement = index.select(remaining, now=NOW+22)
    assert len(replacement) == 1 and replacement[0]["id"] != first[0]["id"]
    # When the omitted publication returns, it cannot consume the other retry's
    # reservation. Its preserved cursor waits for a fresh free slot.
    again = index.select(records, now=NOW+23)
    assert len(again) == 1 and again[0]["id"] == replacement[0]["id"]


def test_old_index_version_restarts_once_and_capacity_markers_survive_reopen(tmp_path, monkeypatch):
    import service.behavior.source_index as module
    monkeypatch.setattr(module, "MAX_EDGES", 1)
    path = tmp_path / "old-index.db"
    store = Store(path)
    index = SourceIndex(store)
    records, manifests, _ = prepare([snap(n=2)])
    index.sync(records, manifests, epoch="consent-one", now=NOW)
    for n, record in enumerate(records):
        assert index.finish([dict(record, text_start=0)], records, [], epoch="consent-one", now=NOW+n,
                            admit=lambda: True, work=[_work(record, NOW+(n+1)*86400)])
    store.close()
    store = Store(path)
    try:
        index = SourceIndex(store)
        index.sync(records, manifests, epoch="consent-one", now=NOW+2)
        assert index.status(records)["email"]["state"] == "capacity_limited"
        assert index.select(records, now=NOW+2) == []
        store.meta_set("behavior_seed_version", module.VERSION-1)
        index.sync(records, manifests, epoch="consent-one", now=NOW+3)
        assert len(index.select(records, now=NOW+3)) == 2
        assert index.work() == [] and index.status(records)["email"]["capacity_deferred"] == 0
    finally:
        store.close()


def test_new_extractor_version_reanalyzes_without_reusing_prior_checkpoint(index, monkeypatch):
    import service.behavior.source_index as module
    run(index, [snap()])
    monkeypatch.setattr(module, "VERSION", module.VERSION+1)
    assert run(index, [snap()], NOW+1)["analyzed"] == 1


@pytest.mark.parametrize("source,expected", [("calendar", "scheduled"), ("email", "source_statement"),
                                            ("messages", "source_statement"), ("notes", "source_statement")])
def test_source_content_cannot_promote_itself_to_observed_completion(source, expected):
    publication = snap(source)
    publication = replace(publication, records=({**publication.records[0], "evidence_basis": "recorded_completion"},))
    records, _, _ = prepare([publication])
    assert records[0]["evidence_basis"] == expected


def test_empty_fresh_publication_differs_from_unavailable(index):
    result = run(index, [replace(snap(n=0), omitted=0)])
    info = result["sources"]["email"]
    assert info["state"] == "seeded" and info["coverage"] == "bounded_publication"
    assert info["indexed"] == 0 and info["observed_range"] is None


def test_admission_is_rechecked_inside_publication_gate_before_sync(index):
    from contextlib import contextmanager
    live = [True]
    @contextmanager
    def publication():
        live[0] = False  # simulate revocation before this pass gets the host gate
        index.reset("new-consent")
        yield
    result = Seed(index).run([snap()], epoch="old-consent", now=NOW, analysis_enabled=True,
        analyze=lambda *a, **k: {"links": []}, admit=lambda: live[0], publication=publication)
    assert result["state"] == "unavailable" and index._epoch() == "new-consent"
    assert index.store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0


def test_only_records_actually_included_in_interpreter_input_advance(index):
    def analyze(rows, **_):
        return dict(links=[], processed_ids=[rows[0]["id"]])
    result = Seed(index).run([snap(n=4)], epoch="one", now=NOW, analysis_enabled=True,
                            analyze=analyze, admit=lambda: True)
    assert result["analyzed"] == 1 and result["sources"]["email"]["analyzed"] == 1
    assert result["sources"]["email"]["pending"] == 3


def test_malformed_metadata_and_unicode_do_not_create_false_time_or_crash_seed():
    publication = replace(snap(), records=(dict(id="valid", text="A safely readable passage.", source_time=[],
        time_basis={}, direction=[], evidence_basis={}, status=[]), dict(id="bad", text="\ud800")))
    records, detail, _ = prepare([publication])
    assert len(records) == 1 and records[0]["source_time"] is None and records[0]["direction"] == "unknown"
    assert detail["email"]["skipped"] == 1


@pytest.mark.parametrize("text,window", [
    ("The arbitrary work is due 2026-10-20 at 4pm UTC.", [1792512000.0, 1792512060.0]),
    ("The arbitrary work is due tomorrow at 4pm UTC.", None),
])
def test_seed_regrounds_absolute_time_without_an_utterance_anchor(index, text, window):
    from service.assistant.behavior_interpreter import Interpreter
    raw = dict(id="opaque-email", text=text, source_time=None, time_basis="unknown",
               evidence_basis="source_statement", reference={"source": "email", "message_id": "opaque"})
    snapshot = Snapshot("email", (raw,), enabled=True, available=True, current=lambda: True,
                        publication_complete=False, published_at=NOW)
    def complete(system, prompt, **kwargs):
        row = json.loads(prompt)["evidence"][0]
        return json.dumps(dict(patterns=[], links=[], demands=[dict(source_id=row["id"],
            quote=text, kind="preparation", title="Prepare", ownership="user", importance="unknown",
            temporal_id=row["temporal"][0]["id"] if row["temporal"] else None)]))
    def analyze(rows, *, admit):
        result = Interpreter(complete).interpret(rows, [], now=NOW, timezone="UTC", admit=admit, associations=True)
        assert result["state"] == "tentative"
        return dict(links=[], demands=[{k:d.get(k) for k in
            ("source_id", "quote", "kind", "ownership", "due_range", "importance_hypothesis")}
            for d in result["demands"]], processed_ids=result["record_ids"])
    seed = Seed(index)
    result = seed.run([snapshot], epoch="source-consent", now=NOW, analysis_enabled=True,
                      analyze=analyze, admit=lambda: True)
    assert result["state"] == "advanced"
    work = seed.demands([snapshot], epoch="source-consent", now=NOW, admit=lambda: True)
    assert len(work) == 1 and work[0]["due_range"] == window
    assert work[0]["observed_at"] is None and work[0]["action_authority"] == "none"
    assert work[0]["status"] == ("tentative" if window else "unresolved")
    persisted = "\n".join(index.store._conn.iterdump())
    assert text not in persisted and "Prepare" not in persisted
