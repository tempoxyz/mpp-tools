"""SemVer constraints for conformance scenarios."""

from semantic_version import NpmSpec, Version


def matches_constraint(version: str, expression: str) -> bool:
    """Return whether a version satisfies an npm-style SemVer constraint."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("SemVer constraint must be a non-empty string")

    try:
        actual = Version(version.removeprefix("v"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid SemVer version: {version!r}") from exc

    try:
        constraint = NpmSpec(expression)
    # semantic_version 2.10 raises AttributeError on some hyphen ranges such as
    # ">=0.4 - 0.6"; report every parse failure as an invalid constraint.
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid SemVer constraint: {expression!r}") from exc

    return constraint.match(actual)
