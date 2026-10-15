"""Initial seeding using supplied fake publications; no user/source/model reads."""
from dataclasses import replace
import json

import pytest

from service.assistant.behavior_seed import Seed, Snapshot, prepare
from service.behavior.source_index import SourceIndex, SOURCES
from service.behavior.store import Store

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
    assert result["sources"]["email"]["analyzed_characters"] == 2400
    result = run(index, [publication], NOW+1)
    assert result["sources"]["email"]["analyzed"] == 0
    result = run(index, [publication], NOW+2)
    assert result["sources"]["email"]["analyzed"] == 1
    assert result["sources"]["email"]["analyzed_characters"] == len(text)+1


def test_all_five_sources_get_turns_with_four_anchor_budget(index):
    snapshots = [snap(source=s, n=8, text="Disjoint long passage "+s) for s in SOURCES]
    result = run(index, snapshots)
    result = run(index, snapshots, NOW+1)
    assert all(result["sources"][s]["analyzed"] > 0 for s in SOURCES)


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


def test_new_extractor_version_reanalyzes_without_reusing_prior_checkpoint(index, monkeypatch):
    import service.behavior.source_index as module
    run(index, [snap()])
    monkeypatch.setattr(module, "VERSION", 2)
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
