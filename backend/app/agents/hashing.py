import hashlib
import json

from app.domain.candidates import CandidatePOI


def candidate_set_sha256(candidates: tuple[CandidatePOI, ...]) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
