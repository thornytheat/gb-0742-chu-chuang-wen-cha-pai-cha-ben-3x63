from fastapi import APIRouter

from app.dependencies import Archive, Service
from app.modules.audit.reconcile import reconcile_lineage

router = APIRouter(prefix="/api/runs/{run_id}")


@router.get("/audit")
def reconciliation(archive: Archive, service: Service):
    snapshot = archive[1]
    ledger = service.store.lineage_ledger(archive[0]["id"])
    audit = {**snapshot["audit"], "isolation": snapshot["isolation"]}
    # 归卷对账实时比对追加式归因账，保证与异常持续页同一条结论。
    audit["lineage"] = reconcile_lineage(snapshot["lineage"], ledger)
    return audit
