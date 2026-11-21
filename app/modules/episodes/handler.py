from fastapi import APIRouter

from app.dependencies import Archive, cabinet_scope
from app.errors import FieldError

router = APIRouter(prefix="/api/runs/{run_id}/episodes")


def _attribution_map(snapshot):
    return {item["segment"]: item for item in snapshot["lineage"]}


@router.get("/{cabinet}")
def episodes(archive: Archive, cabinet: str):
    cabinet_scope(archive[0], cabinet)
    snapshot = archive[1]
    attribution = _attribution_map(snapshot)
    result = []
    for item in snapshot["lineage_segments"]:
        if item["cabinet"] != cabinet:
            continue
        result.append({**item, "attribution": attribution.get(item["id"], {
            "direction": "UNLINKED", "status": "PRESERVED", "basis": "SWAP_EVIDENCE_INCOMPLETE",
            "links": []})})
    return result


@router.get("/{cabinet}/{episode_id}")
def episode(archive: Archive, cabinet: str, episode_id: str):
    snapshot = archive[1]
    for item in episodes(archive, cabinet):
        if item["id"] == episode_id or (item["parent"] == episode_id and item["id"].startswith(episode_id)):
            ids = {row[level] for row in item["evidence"] for level in ("upper", "lower")}
            return {**item, "rows": [row for row in snapshot["rows"] if row["id"] in ids]}
    raise FieldError("UNKNOWN_EPISODE", "异常段未登记", 404)
