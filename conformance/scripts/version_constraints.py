"""SemVer parsing and comparator constraints for conformance scenarios."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering


_SEMVER_TEXT = (
    r"v?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_COMPARATOR_RE = re.compile(rf"(<=|>=|==|=|<|>)\s*({_SEMVER_TEXT})")


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ValueError(f"Invalid SemVer version: {value!r}")

        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        if any(
            identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
            for identifier in prerelease
        ):
            raise ValueError(f"Invalid SemVer version: {value!r}")

        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented

        own_core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if own_core != other_core:
            return own_core < other_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True

        for own_identifier, other_identifier in zip(self.prerelease, other.prerelease):
            if own_identifier == other_identifier:
                continue
            own_numeric = own_identifier.isdigit()
            other_numeric = other_identifier.isdigit()
            if own_numeric and other_numeric:
                return int(own_identifier) < int(other_identifier)
            if own_numeric != other_numeric:
                return own_numeric
            return own_identifier < other_identifier
        return len(self.prerelease) < len(other.prerelease)


def _comparators(expression: str) -> list[tuple[str, SemVer]]:
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("SemVer constraint must be a non-empty string")

    comparators: list[tuple[str, SemVer]] = []
    position = 0
    while position < len(expression):
        separator = re.match(r"[\s,]*", expression[position:]).group(0)
        if comparators and not separator:
            raise ValueError(f"Invalid SemVer constraint: {expression!r}")
        if not comparators and "," in separator:
            raise ValueError(f"Invalid SemVer constraint: {expression!r}")
        position += len(separator)
        if position == len(expression):
            if "," in separator:
                raise ValueError(f"Invalid SemVer constraint: {expression!r}")
            break

        match = _COMPARATOR_RE.match(expression, position)
        if not match:
            raise ValueError(f"Invalid SemVer constraint: {expression!r}")
        comparators.append((match.group(1), SemVer.parse(match.group(2))))
        position = match.end()

    if not comparators:
        raise ValueError(f"Invalid SemVer constraint: {expression!r}")
    return comparators


def matches_constraint(version: str, expression: str) -> bool:
    """Return whether a SemVer version satisfies every comparator."""
    actual = SemVer.parse(version)
    operations = {
        "=": lambda expected: actual == expected,
        "==": lambda expected: actual == expected,
        "<": lambda expected: actual < expected,
        "<=": lambda expected: actual <= expected,
        ">": lambda expected: actual > expected,
        ">=": lambda expected: actual >= expected,
    }
    return all(
        operations[operator](expected)
        for operator, expected in _comparators(expression)
    )
