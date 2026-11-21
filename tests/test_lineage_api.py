from app.manifest import canonical


def test_lineage_endpoint_and_ledger_share_one_conclusion(client, base):
    page = client.get(base + "/lineage").json()
    assert {move["probe"] for move in page["swaps"][0]["moves"]} == {"0001", "0008"}
    ledger = client.get(base + "/lineage/ledger").json()
    segments = {item["segment"]: item for item in page["attribution"]}
    assert segments["CV-17-A03"]["direction"] == "PROBE"
    assert segments["CV-17-A01"]["direction"] == "CABINET"
    # 归卷对账与异常持续页是同一条结论
    assert {item["segment"]: item for item in ledger} == {
        item["segment"]: item for item in page["attribution"]}
    audit = client.get(base + "/audit").json()["lineage"]
    assert audit["balanced"] and audit["direction_counts"] == {"CABINET": 2, "PROBE": 1}


def test_episode_payload_carries_attribution_and_original_rows(client, base):
    detail = client.get(base + "/episodes/CV-17/CV-17-A03").json()
    assert detail["attribution"]["direction"] == "PROBE"
    assert detail["attribution"]["links"][0]["ref"] == "CV-21-A01"
    assert len(detail["rows"]) == 4


def test_ledger_is_append_only_and_old_rows_are_recoverable(client, base):
    run_id = base.rsplit("/", 1)[1]
    store = client.app.state.archives.store
    first = store.lineage_ledger(run_id)
    # 再次落账不会覆盖旧行（ON CONFLICT DO NOTHING），原结论可恢复
    store.append_lineage(run_id, [{
        "segment": "CV-17-A01", "direction": "PROBE", "status": "LINKED",
        "basis": "TAMPERED", "links": [], "cabinet": "CV-17", "start": None,
        "end": None, "parent": "CV-17-A01", "part": 1, "upper_probes": ["0001"],
        "swap_at": None}], "2026-11-21T00:00:00+00:00")
    second = store.lineage_ledger(run_id)
    assert second == first and second[0]["direction"] == "CABINET"


def test_missing_prior_shift_rejects_run_and_keeps_old_versions(client):
    reply = client.post("/api/runs", json={"sample": "shift"})
    assert (reply.status_code, reply.json()["code"]) == (409, "PRIOR_SHIFT_MISSING")
    assert client.get("/api/runs").json() == []


def test_ledger_failure_rolls_back_archive_and_preserves_prior(client):
    assert client.post("/api/runs", json={"sample": "prior"}).status_code == 201
    store = client.app.state.archives.store
    assert [item["shift"] for item in store.history()] == ["N26-1119"]
    with store.connect() as db:
        db.execute("CREATE TRIGGER badline BEFORE INSERT ON lineage_ledger"
                   " BEGIN SELECT RAISE(ABORT, 'disk'); END")
    failed = client.post("/api/runs", json={"sample": "shift"})
    assert (failed.status_code, failed.json()["code"]) == (503, "ARCHIVE_UNAVAILABLE")
    with store.connect() as db:
        db.execute("DROP TRIGGER badline")
    # 归卷与归因账同一事务：失败后只剩旧班次，无半成品，重试可成功
    assert [item["shift"] for item in store.history()] == ["N26-1119"]
    assert client.post("/api/runs", json={"sample": "shift"}).status_code == 201
    assert {item["shift"] for item in store.history()} == {"N26-1119", "N26-1120"}


def test_cabinet_scoped_lineage_and_unknown_cabinet(client, base):
    scoped = client.get(base + "/lineage/cabinet/CV-17").json()
    assert {item["cabinet"] for item in scoped} == {"CV-17"}
    assert client.get(base + "/lineage/cabinet/CV-99").status_code == 404


def test_audit_flags_drift_between_page_and_ledger(client, base):
    from app.modules.audit.reconcile import reconcile_lineage
    page = [{"segment": "CV-17-A01", "direction": "CABINET"},
            {"segment": "CV-17-A03", "direction": "PROBE"}]
    tampered = [{"segment": "CV-17-A01", "direction": "PROBE"},
                {"segment": "CV-17-A03", "direction": "PROBE"}]
    result = reconcile_lineage(page, tampered)
    assert not result["balanced"] and result["drifted"] == ["CV-17-A01"]
    assert canonical(result)  # 结论可序列化进归卷
