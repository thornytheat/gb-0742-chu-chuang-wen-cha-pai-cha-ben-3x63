from datetime import datetime, timezone
from time import perf_counter

from app.config import POLICY
from app.errors import FieldError
from app.manifest import identify, json_sha, preserve
from app.modules.archive.pipeline import evaluate
from app.modules.archive.proof import verify
from app.modules.intake.catalog import load_sample
from app.timebase import seconds, utc


class ArchiveService:
    def __init__(self, store, loader=load_sample):
        self.store = store
        self.loader = loader

    def run(self, sample):
        start = perf_counter()
        package = self.loader(sample)
        prior = self._load_prior(package.spec.prior_shift)
        evidence = evaluate(package, prior)
        spec = package.spec
        key, hashes = identify(package)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest = {"id": key, "sample": spec.sample, "label": spec.label, "shift": spec.shift, "policy": POLICY,
                    "created": created,
                    "window": [utc(seconds(spec.start)), utc(seconds(spec.end))],
                    "cabinets": [cabinet.model_dump(mode="json") for cabinet in spec.cabinet_hours],
                    "tolerance_seconds": spec.tolerance_seconds, "sustained_hours": spec.sustained_hours,
                    "prior_shift": spec.prior_shift,
                    "prior_run": prior[0]["id"] if prior else None,
                    "file": spec.csv, "hashes": hashes, "result_sha256": json_sha(evidence),
                    "elapsed_ms": round(1000 * (perf_counter() - start)), "summary": evidence["audit"]["hours"]}
        record, created_run = self.store.insert_run_with_lineage(
            manifest, evidence, preserve(package), evidence["lineage"], created)
        if record["result_sha256"] != manifest["result_sha256"]:
            raise FieldError("RULE_DRIFT", "同版结果摘要不一致", 409)
        return {"manifest": record, "reused": not created_run}

    def _load_prior(self, prior_shift):
        if not prior_shift:
            return None
        archive = self.store.fetch_by_shift(prior_shift)
        if archive is None:
            raise FieldError("PRIOR_SHIFT_MISSING", "依据旧班次未归卷，换装证据不足，保留旧版结论", 409)
        verify(archive)
        return archive

    def replay(self, run_id):
        archive = self.store.fetch(run_id)
        manifest = archive[0]
        if manifest["policy"] != POLICY:
            raise FieldError("OLD_POLICY", "当前规则无法复算此归卷", 409)
        pinned = verify(archive)
        prior = self._load_prior(manifest.get("prior_shift"))
        if json_sha(evaluate(pinned, prior)) != manifest["result_sha256"]:
            raise FieldError("RULE_DRIFT", "复算结果摘要不一致", 409)
        return {"manifest": manifest, "verified": True, "reused": True}
