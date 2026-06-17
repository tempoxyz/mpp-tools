from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unnamed"


@dataclass
class ConformanceCheck:
    id: str
    name: str
    description: str
    status: str
    timestamp: str
    specReferences: list[dict[str, str]]
    details: dict[str, Any]
    errorMessage: str | None = None


def make_check(
    *,
    id_parts: list[str],
    name: str,
    description: str,
    passed: bool,
    spec_ref: str | None = None,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return asdict(
        ConformanceCheck(
            id="mpp-" + "-".join(_slug(part) for part in id_parts),
            name=name,
            description=description,
            status="SUCCESS" if passed else "FAILURE",
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            specReferences=[{"id": spec_ref}] if spec_ref else [],
            details=details or {},
            errorMessage=error,
        )
    )
