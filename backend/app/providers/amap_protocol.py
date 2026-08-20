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


def decode_mcp_json(result: object, *, operation: str) -> dict[str, object]:
    content = getattr(result, "content", None)
    if not isinstance(content, Sequence):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned no MCP content blocks",
            )
        )

    text: str | None = None
    for block in content:
        candidate = getattr(block, "text", None)
        if isinstance(candidate, str):
            text = candidate
            break
    if text is None:
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned no JSON text content",
            )
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"{operation} returned invalid JSON content",
            )
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ProviderRequestError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} did not return a JSON object",
            )
        )

    payload = {str(key): value for key, value in decoded.items()}
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
