import hashlib
import json
from collections.abc import Sequence

from app.domain.base import DomainModel
from app.domain.candidates import CandidatePOI, CandidateStay


def _model_set_sha256(items: Sequence[DomainModel]) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in items],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_set_sha256(candidates: tuple[CandidatePOI, ...]) -> str:
    return _model_set_sha256(candidates)


def stay_candidate_set_sha256(candidates: tuple[CandidateStay, ...]) -> str:
    return _model_set_sha256(candidates)


def candidate_facts_equal(
    left: CandidatePOI | CandidateStay,
    right: CandidatePOI | CandidateStay,
) -> bool:
    """Compare normalized candidate facts while ignoring retrieval-only provenance fields."""

    if type(left) is not type(right):
        return False

    def semantic_payload(candidate: CandidatePOI | CandidateStay) -> dict[str, object]:
        payload = candidate.model_dump(mode="json")
        source = payload.get("source")
        if isinstance(source, dict):
            source.pop("retrieved_at", None)
            source.pop("raw_response_sha256", None)
        return payload

    return semantic_payload(left) == semantic_payload(right)
