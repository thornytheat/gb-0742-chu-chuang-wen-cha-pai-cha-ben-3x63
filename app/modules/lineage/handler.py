from fastapi import APIRouter

from app.dependencies import Archive, Service, cabinet_scope

router = APIRouter(prefix="/api/runs/{run_id}/lineage")


@router.get("")
def lineage(archive: Archive):
    """跨换装归因总账：异常持续页与归卷对账共用同一条结论。"""
    _, snapshot, _ = archive
    return {"swaps": snapshot["swap_events"], "attribution": snapshot["lineage"],
            "segments": snapshot["lineage_segments"]}


@router.get("/cabinet/{cabinet}")
def cabinet_lineage(archive: Archive, cabinet: str):
    cabinet_scope(archive[0], cabinet)
    _, snapshot, _ = archive
    return [item for item in snapshot["lineage"] if item["cabinet"] == cabinet]


@router.get("/ledger")
def ledger(run_id: str, service: Service):
    """追加式归因账，只增不改；旧探头原行与旧版归因均可据此恢复。"""
    service.store.fetch(run_id)
    return service.store.lineage_ledger(run_id)
