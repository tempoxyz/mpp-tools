from __future__ import annotations

from datetime import UTC, datetime

from agricola.models import (
    SDK,
    Automation,
    CanonicalChange,
    Changelog,
    Manifest,
    PullRequestFile,
    Repository,
)


def manifest() -> Manifest:
    return Manifest(
        version=1,
        maintainers=frozenset({"brendanjryan", "maintainer"}),
        canonical=Repository(repo="wevm/mppx"),
        spec=Repository(repo="tempoxyz/mpp-specs"),
        sdks={
            "go": SDK(
                repo="tempoxyz/mpp-go",
                automation=Automation.PR,
                owners=("brendanjryan",),
                changelog=Changelog.KEEP_A_CHANGELOG,
                verify=("make test",),
            ),
            "rust": SDK(
                repo="tempoxyz/mpp-rs",
                automation=Automation.PR,
                owners=(),
                changelog=Changelog.KEEP_A_CHANGELOG,
                verify=("cargo test",),
            ),
            "ruby": SDK(
                repo="stripe/mpp-rb",
                automation=Automation.NOTIFY,
                owners=(),
                changelog=Changelog.NONE,
                verify=(),
            ),
        },
    )


def change(number: int = 412) -> CanonicalChange:
    return CanonicalChange(
        repo="wevm/mppx",
        number=number,
        sha="abc1234567",
        title="Add refunds",
        url=f"https://github.com/wevm/mppx/pull/{number}",
        body="Adds refund behavior.",
        merged_at=datetime(2026, 8, 7, 14, 0, tzinfo=UTC),
        labels=("agricola:go",),
        files=(
            PullRequestFile("src/refunds.ts", additions=20, deletions=2),
            PullRequestFile(
                "conformance/vectors/refunds.json", status="added", additions=30
            ),
            PullRequestFile("package-lock.json", additions=4, deletions=4),
        ),
    )
