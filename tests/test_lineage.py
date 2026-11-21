import pytest

from app.modules.intake.catalog import load_sample
from app.modules.lineage.attribution import (CABINET, NEW, PROBE, UNLINKED,
                                              split_at_swaps, swaps)
from app.modules.archive.pipeline import evaluate


@pytest.fixture(scope="module")
def prior_snapshot():
    return evaluate(load_sample("prior"))


@pytest.fixture(scope="module")
def prior_archive(prior_snapshot):
    return ({"id": "run-prior", "shift": "N26-1119"}, prior_snapshot, None)


def test_swap_event_detects_pairwise_upper_exchange(package):
    events = swaps(package.spec)
    assert len(events) == 1 and events[0]["at"] == "2026-11-20T05:00:00Z"
    moves = {move["probe"]: (move["from_cabinet"], move["to_cabinet"]) for move in events[0]["moves"]}
    assert moves == {"0001": ("CV-17", "CV-21"), "0008": ("CV-21", "CV-17")}


def test_each_episode_gets_one_primary_direction_and_keeps_original_rows(package, prior_archive):
    evidence = evaluate(package, prior_archive)
    assert len(evidence["lineage"]) == len(evidence["episodes"])
    for item in evidence["lineage"]:
        assert item["direction"] in (PROBE, CABINET, NEW, UNLINKED)
        assert item["status"] in ("LINKED", "PRESERVED")
        # 原行不被归因覆盖：每段仍能在 rows 中按证据 id 找回原行
        segment = next(s for s in evidence["lineage_segments"] if s["id"] == item["segment"])
        ids = {row_id for proof in segment["evidence"] for row_id in (proof["upper"], proof["lower"])}
        assert ids <= {row["id"] for row in evidence["rows"]}


def test_post_swap_episode_follows_moved_probe_to_prior_ledger(package, prior_archive):
    evidence = evaluate(package, prior_archive)
    by_segment = {item["segment"]: item for item in evidence["lineage"]}
    moved = by_segment["CV-17-A03"]
    assert moved["direction"] == PROBE and moved["basis"] == "PROBE_HISTORY"
    assert moved["upper_probes"] == ["0008"] and moved["swap_at"] == "2026-11-20T05:00:00Z"
    link = moved["links"][0]
    assert (link["run"], link["ref"], link["cabinet"], link["probe"]) == (
        "run-prior", "CV-21-A01", "CV-21", "0008")


def test_pre_swap_episodes_follow_cabinet_and_chain_older_ledger(package, prior_archive):
    evidence = evaluate(package, prior_archive)
    by_segment = {item["segment"]: item for item in evidence["lineage"]}
    first = by_segment["CV-17-A01"]
    assert first["direction"] == CABINET and first["basis"] == "CABINET_ORIGIN"
    second = by_segment["CV-17-A02"]
    assert second["direction"] == CABINET and second["links"][0]["ref"] == "CV-17-A01"


def test_moved_probe_without_prior_history_is_new_fault_not_reused(package):
    evidence = evaluate(package, None)
    by_segment = {item["segment"]: item for item in evidence["lineage"]}
    assert by_segment["CV-17-A03"]["direction"] == NEW
    assert by_segment["CV-17-A03"]["basis"] == "NO_PRIOR_LEDGER"


def test_prior_probe_history_is_compatible_after_migration(prior_snapshot):
    # 旧班次 0008 在 CV-21 的异常段被索引到探头，迁移后仍可按探头追溯
    from app.modules.lineage.attribution import _prior_history
    by_probe, by_cabinet = _prior_history(({"id": "x"}, prior_snapshot, None))
    assert [item["episode"] for item in by_probe["0008"]] == ["CV-21-A01"]
    assert [item["episode"] for item in by_cabinet["CV-21"]] == ["CV-21-A01"]


def test_split_at_swap_keeps_parent_and_parts_recoverable():
    episodes = [{"id": "CV-17-A01", "cabinet": "CV-17", "kind": "SUSTAINED", "axes": ["temperature"],
                 "start": "2026-11-20T03:00:00Z", "end": "2026-11-20T07:00:00Z", "hours": 4,
                 "evidence": [{"start": f"2026-11-20T{hour:02d}:00:00Z", "upper": "u", "lower": "l"}
                              for hour in range(3, 7)]}]
    from app.timebase import seconds
    events = [{"at_epoch": seconds("2026-11-20T05:00:00Z"),
               "at": "2026-11-20T05:00:00Z", "moves": []}]
    parts = split_at_swaps(episodes, events)
    assert [(p["id"], p["parent"], p["part"], p["hours"]) for p in parts] == [
        ("CV-17-A01a", "CV-17-A01", 1, 2), ("CV-17-A01b", "CV-17-A01", 2, 2)]
    # 原段仍可由父段号与片段序拼回
    recovered = [proof for p in parts for proof in p["evidence"]]
    assert recovered == episodes[0]["evidence"]


def test_no_swap_leaves_segments_identical_to_episodes():
    episodes = [{"id": "CV-17-A01", "cabinet": "CV-17", "kind": "TRANSIENT", "axes": [],
                 "start": "2026-11-20T03:00:00Z", "end": "2026-11-20T04:00:00Z", "hours": 1,
                 "evidence": [{"start": "2026-11-20T03:00:00Z", "upper": "u", "lower": "l"}]}]
    parts = split_at_swaps(episodes, [])
    assert parts[0]["parent"] == "CV-17-A01" and parts[0]["part"] == 1
    assert parts[0]["evidence"] == episodes[0]["evidence"]


def test_incomplete_swap_evidence_preserves_old_conclusion_unlinked(package, prior_archive):
    # 抹掉 0001 的换装后新窗：换装证据不成立，段不被强行接线
    spec = package.spec.model_copy(update={
        "deployments": [d for d in package.spec.deployments
                        if not (d.probe == "0001" and d.cabinet == "CV-21")]})
    broken = package.__class__(spec, package.blob)
    evidence = evaluate(broken, prior_archive)
    assert evidence["swap_events"] == []
    for item in evidence["lineage"]:
        assert item["direction"] in (CABINET, NEW, UNLINKED)
