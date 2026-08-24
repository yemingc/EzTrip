import json
from collections.abc import Mapping, Sequence
from urllib.parse import urlencode

from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.providers.errors import ProviderRequestError

AUTHENTICATION_INFOCODES = frozenset(
    {
        "10001",
        "10002",
        "10005",
        "10006",
        "10007",
        "10009",
        "10012",
        "10013",
        "10041",
    }
)
RATE_LIMIT_INFOCODES = frozenset(
    {
        "10003",
        "10004",
        "10010",
        "10014",
        "10019",
        "10020",
        "10021",
        "10029",
        "10044",
        "10045",
    }
)
TIMEOUT_INFOCODES = frozenset({"10015", "10016"})


def classify_amap_infocode(infocode: str) -> ProviderErrorCategory:
    if infocode in AUTHENTICATION_INFOCODES:
        return ProviderErrorCategory.AUTHENTICATION_FAILED
    if infocode in RATE_LIMIT_INFOCODES:
        return ProviderErrorCategory.RATE_LIMITED
    if infocode in TIMEOUT_INFOCODES:
        return ProviderErrorCategory.TIMEOUT
    return ProviderErrorCategory.UNRECOVERABLE


def build_amap_failure(
    *,
    operation: str,
    category: ProviderErrorCategory,
    message: str,
) -> ProviderFailure:
    return ProviderFailure(
        provider="amap",
        operation=operation,
        category=category,
        message=message,
        retryable=category in {ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.RATE_LIMITED},
    )


def _unwrap_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().casefold() not in {"```", "```json"}:
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _decode_text_payloads(content: Sequence[object]) -> tuple[dict[str, object] | None, bool]:
    saw_text = False
    saw_non_object_json = False
    for block in content:
        candidate = getattr(block, "text", None)
        if not isinstance(candidate, str):
            continue
        saw_text = True
        try:
            decoded = json.loads(_unwrap_json_fence(candidate))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, Mapping):
            return {str(key): value for key, value in decoded.items()}, saw_non_object_json
        saw_non_object_json = True
    if not saw_text:
        return None, saw_non_object_json
    return None, saw_non_object_json


def decode_mcp_json(result: object, *, operation: str) -> dict[str, object]:
    structured_content = getattr(result, "structuredContent", None)
    if structured_content is not None and not isinstance(structured_content, Mapping):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned invalid MCP structured content",
            )
        )

    content = getattr(result, "content", None)
    if not isinstance(content, Sequence):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned no MCP content blocks",
            )
        )

    payload = (
        {str(key): value for key, value in structured_content.items()}
        if isinstance(structured_content, Mapping)
        else None
    )
    saw_non_object_json = False
    if payload is None:
        payload, saw_non_object_json = _decode_text_payloads(content)
    if payload is None and bool(getattr(result, "isError", False)):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"{operation} returned an MCP tool error",
            )
        )
    if payload is None and saw_non_object_json:
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} did not return a JSON object",
            )
        )
    if payload is None:
        text_exists = any(isinstance(getattr(block, "text", None), str) for block in content)
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=(
                    ProviderErrorCategory.UNRECOVERABLE
                    if text_exists
                    else ProviderErrorCategory.MISSING_FIELD
                ),
                message=(
                    f"{operation} returned invalid JSON content"
                    if text_exists
                    else f"{operation} returned no JSON text content"
                ),
            )
        )

    infocode = str(payload.get("infocode", ""))
    if payload.get("status") == "0" or (infocode and infocode != "10000"):
        info = str(payload.get("info", "AMap request failed"))
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=classify_amap_infocode(infocode),
                message=f"{operation} failed with AMap infocode {infocode}: {info}",
            )
        )
    if bool(getattr(result, "isError", False)):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"{operation} returned an MCP tool error",
            )
        )
    return payload


def build_mcp_url(endpoint: str, key: str) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode({'key': key})}"
