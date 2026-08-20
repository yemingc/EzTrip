from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class DomainModel(BaseModel):
    """Shared validation policy for all public domain contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )
