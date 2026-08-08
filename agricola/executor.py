from __future__ import annotations

import subprocess
from pathlib import Path

from .models import AuditSource, PropagationRequest


class VerificationError(RuntimeError):
    pass


def pull_request_title(request: PropagationRequest) -> str:
    return request.source_title


def pull_request_body(request: PropagationRequest) -> str:
    ticket = (
        f"[Agricola ticket #{request.tracking_issue}]({request.tracking_issue_url})"
    )
    canonical_commit = (
        f"[{request.source.repo}@{request.source.sha[:12]}]"
        f"(https://github.com/{request.source.repo}/commit/{request.source.sha})"
    )
    target_commit = (
        f"[{request.target_repo}@{request.target_base_sha[:12]}]"
        f"(https://github.com/{request.target_repo}/commit/{request.target_base_sha})"
    )
    if isinstance(request.source, AuditSource):
        return (
            f"<!-- agricola:audit-finding={request.source.finding} "
            f"target={request.target} -->\n"
            "## Motivation\n\n"
            f"Agricola found that `{request.target_repo}` diverges from the canonical "
            f"implementation: **{request.source_title}**\n\n"
            f"The {ticket} contains the audit evidence, affected SDKs, and remediation "
            "lifecycle.\n\n"
            "## Summary\n\n"
            f"- Reconciles `{request.source.fingerprint}` in the target SDK's idioms.\n"
            f"- Adds implementation and regression coverage for {request.source.finding}.\n"
            f"- Links the change to the {ticket}.\n\n"
            "## Key design considerations\n\n"
            f"- Limits scope to the audited delta between {canonical_commit} and "
            f"{target_commit}.\n"
            "- Favors the target SDK's public API and conventions over a literal port.\n"
            f"- Uses the stable `{request.branch}` automation branch.\n"
            "- Remains a draft until a maintainer reviews the generated changes.\n"
        )
    source = f"[{request.source.repo}#{request.source.pr}]({request.source_url})"
    return (
        f"<!-- agricola:source={request.source.repo}#{request.source.pr} "
        f"target={request.target} -->\n"
        "## Motivation\n\n"
        f"Propagate **{request.source_title}** from {source} to "
        f"`{request.target_repo}`. The {ticket} records the target decision and "
        "remediation lifecycle.\n\n"
        "## Summary\n\n"
        f"- Ports the behavior introduced by {source}.\n"
        "- Adds target-native implementation and regression coverage.\n"
        f"- Links the change to the {ticket}.\n\n"
        "## Key design considerations\n\n"
        f"- Pins the port to {canonical_commit} and {target_commit}.\n"
        "- Favors the target SDK's public API and conventions over source-language structure.\n"
        f"- Uses the stable `{request.branch}` automation branch.\n"
        "- Remains a draft until a maintainer reviews the generated changes.\n"
    )


def verify(request: PropagationRequest, root: str | Path = ".") -> None:
    for command in request.verify:
        process = subprocess.run(
            ["bash", "-lc", command],
            cwd=root,
            check=False,
        )
        if process.returncode:
            raise VerificationError(
                f"verification command failed ({process.returncode}): {command}"
            )
