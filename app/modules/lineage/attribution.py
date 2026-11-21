"""跨换装归因：识别上层探头在展柜间的换装上，并为每个异常段判定唯一主处置方向。

归因只用已经固化的证据（部署窗、柜时判级、异常段、原行），不修改任何旧探头原行。
主处置方向只有一条：

- ``PROBE`` 跟探头：异常由迁移探头带入，沿同一探头接旧班次的旧账；
- ``CABINET`` 跟柜位：同柜换装前已存在的旧账，由新探头在同一柜承接；
- ``NEW`` 新故障：换装后出现，柜位与探头两侧都没有可接续的异常历史；
- ``UNLINKED`` 证据失败：换装证据未成立时保留旧版结论，不强行接线。
"""
from collections import defaultdict

from app.timebase import seconds, utc

PROBE = "PROBE"
CABINET = "CABINET"
NEW = "NEW"
UNLINKED = "UNLINKED"

DIRECTIONS = (PROBE, CABINET, NEW, UNLINKED)


def _row_index(rows):
    return {row["id"]: row for row in rows if row.get("disposition") == "accepted"}


def _upper_probe(evidence_row, lookup):
    row = lookup.get(evidence_row["upper"])
    deployment = row.get("deployment") if row else None
    return deployment["probe"] if deployment and deployment["level"] == "upper" else None


def swaps(spec):
    """按测量时刻整理上层换装点：同一时刻两柜互换上层探头。

    左闭右开，换装点本身属于换装后的部署窗。只登记 upper 层，且要求在两个不同
    登记柜之间成对交换；证据不完整的换装不产生换装点（由调用方保留旧版）。
    """
    begin, end = seconds(spec.start), seconds(spec.end)
    upper = [window for window in spec.deployments if window.level == "upper"]
    by_moment = defaultdict(list)
    for window in upper:
        start = seconds(window.start)
        if not (begin <= start < end):
            continue
        by_moment[start].append(window)
    events = []
    for moment in sorted(by_moment):
        windows = by_moment[moment]
        # 同一探头在该时刻只允许一条新窗；同一柜在该时刻只允许一条新窗。
        if len({item.probe for item in windows}) != len(windows):
            continue
        if len({item.cabinet for item in windows}) != len(windows):
            continue
        prior = []
        for window in windows:
            candidates = [item for item in upper
                          if item.probe == window.probe and seconds(item.end) == moment
                          and item.cabinet != window.cabinet]
            if len(candidates) != 1:
                prior = []
                break
            prior.append(candidates[0])
        if len(prior) < 2:
            continue
        # 必须成对：新窗的来源柜集合等于旧窗柜集合，且至少涉及两个柜。
        new_cabinets = {item.cabinet for item in windows}
        old_cabinets = {item.cabinet for item in prior}
        if len(new_cabinets) >= 2 and new_cabinets == old_cabinets:
            events.append({"at": utc(moment), "at_epoch": moment,
                           "moves": [{"probe": item.probe, "from_cabinet": old.cabinet,
                                      "to_cabinet": item.cabinet}
                                     for item, old in sorted(zip(windows, prior),
                                                             key=lambda pair: pair[0].probe)]})
    return events


def split_at_swaps(episodes, swap_events):
    """把跨换装点的异常段在换装时刻切开，段号沿用原序号加小写序，原段不覆盖。

    切开后的每段保留父段号 ``parent`` 与片段序 ``part``，证据各自独立，便于恢复。
    """
    if not swap_events:
        return [{**episode, "parent": episode["id"], "part": 1} for episode in episodes]
    moments = sorted({event["at_epoch"] for event in swap_events})
    result = []
    for episode in episodes:
        start = seconds(episode["evidence"][0]["start"])
        end = seconds(episode["evidence"][-1]["start"]) + 3600
        cuts = [moment for moment in moments if start < moment < end]
        if not cuts:
            result.append({**episode, "parent": episode["id"], "part": 1})
            continue
        boundaries = [start, *cuts, end]
        for part, (left, right) in enumerate(zip(boundaries, boundaries[1:]), 1):
            members = [proof for proof in episode["evidence"]
                       if left <= seconds(proof["start"]) < right]
            if not members:
                continue
            result.append({"id": f'{episode["id"]}{chr(96 + part)}', "parent": episode["id"],
                           "part": part, "cabinet": episode["cabinet"],
                           "start": members[0]["start"], "end": utc(seconds(members[-1]["start"]) + 3600),
                           "hours": len(members), "kind": episode["kind"], "axes": episode["axes"],
                           "evidence": members})
    result.sort(key=lambda item: (item["cabinet"], seconds(item["start"])))
    return result


def _prior_history(prior):
    """从旧班次归卷抽取 探头/柜位 -> 异常段 索引，历史归因随探头迁移保留。"""
    if not prior:
        return None, None
    _, snapshot, _ = prior
    lookup = _row_index(snapshot["rows"])
    by_probe, by_cabinet = defaultdict(list), defaultdict(list)
    for episode in snapshot["episodes"]:
        entry = {"episode": episode["id"], "cabinet": episode["cabinet"],
                 "start": episode["start"], "end": episode["end"], "kind": episode["kind"],
                 "probes": sorted({probe for proof in episode["evidence"]
                                   if (probe := _upper_probe(proof, lookup))})}
        for probe in entry["probes"]:
            by_probe[probe].append(entry)
        by_cabinet[episode["cabinet"]].append(entry)
    return by_probe, by_cabinet


def attribute(spec, rows, episodes, swap_events, prior=None):
    """为每段异常给出跨换装归因，返回归因列表与换装证据，同一次计算固化。"""
    lookup = _row_index(rows)
    segments = split_at_swaps(episodes, swap_events)
    prior_by_probe, prior_by_cabinet = _prior_history(prior)
    swap_at = {event["at_epoch"]: event for event in swap_events}
    results = []
    for segment in segments:
        probes = sorted({probe for proof in segment["evidence"]
                         if (probe := _upper_probe(proof, lookup))})
        start = seconds(segment["evidence"][0]["start"])
        anchor = next((moment for moment in sorted(swap_at) if start >= moment), None)
        basis = {"segment": segment["id"], "parent": segment["parent"], "part": segment["part"],
                 "cabinet": segment["cabinet"], "start": segment["start"], "end": segment["end"],
                 "upper_probes": probes, "swap_at": utc(anchor) if anchor else None}
        if anchor is None:
            direction, links, reason = _before_swap(segment, episodes)
        else:
            event = swap_at[anchor]
            direction, links, reason = _after_swap(segment, probes, event, prior_by_probe,
                                                   prior_by_cabinet, prior)
        results.append({**basis, "direction": direction, "links": links,
                        "basis": reason, "status": "LINKED" if direction != UNLINKED else "PRESERVED"})
    return results, segments


def _before_swap(segment, episodes):
    """换装前的段：与同柜更早异常连续则归柜位旧账，否则记本柜首段。"""
    segment_start = seconds(segment["evidence"][0]["start"])
    earlier = [other for other in episodes
               if other["cabinet"] == segment["cabinet"]
               and seconds(other["evidence"][-1]["start"]) < segment_start
               and other["id"] != segment["parent"]]
    if earlier:
        origin = sorted(earlier, key=lambda item: seconds(item["evidence"][-1]["start"]))[-1]
        return CABINET, [{"kind": "EPISODE", "ref": origin["id"], "cabinet": origin["cabinet"],
                          "note": "同柜换装前异常旧账"}], "CABINET_CONTINUITY"
    return CABINET, [], "CABINET_ORIGIN"


def _after_swap(segment, probes, event, prior_by_probe, prior_by_cabinet, prior):
    moves = {move["probe"]: move for move in event["moves"]}
    cabinet = segment["cabinet"]
    incoming = [probe for probe in probes if probe in moves and moves[probe]["to_cabinet"] == cabinet]
    if not incoming:
        # 换装证据无法解释该段上层来源：保留旧版，不覆盖原行结论。
        return UNLINKED, [], "SWAP_EVIDENCE_INCOMPLETE"
    probe = incoming[0]
    move = moves[probe]
    if not prior:
        return NEW, [], "NO_PRIOR_LEDGER"
    probe_history = [item for item in prior_by_probe.get(probe, [])
                     if item["cabinet"] == move["from_cabinet"]]
    if probe_history:
        old = sorted(probe_history, key=lambda item: item["end"])[-1]
        return PROBE, [{"kind": "PRIOR_EPISODE", "ref": old["episode"],
                        "run": prior[0]["id"], "cabinet": old["cabinet"],
                        "probe": probe, "note": "同探头旧班次异常，随探头迁移接续"}], "PROBE_HISTORY"
    cabinet_history = [item for item in prior_by_cabinet.get(cabinet, [])]
    if cabinet_history:
        old = sorted(cabinet_history, key=lambda item: item["end"])[-1]
        return CABINET, [{"kind": "PRIOR_EPISODE", "ref": old["episode"],
                          "run": prior[0]["id"], "cabinet": cabinet,
                          "note": "同柜旧班次异常旧账，新探头承接"}], "CABINET_HISTORY"
    return NEW, [], "NO_MATCHING_HISTORY"
