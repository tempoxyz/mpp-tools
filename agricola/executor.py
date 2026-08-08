from __future__ import annotations

import subprocess
from pathlib import Path

from .models import PropagationRequest


class VerificationError(RuntimeError):
    pass


def pull_request_title(request: PropagationRequest) -> str:
    return request.source_title


def pull_request_body(request: PropagationRequest) -> str:
    source = f"[{request.source.repo}#{request.source.pr}]({request.source_url})"
    tracking = (
        f"[tracking issue #{request.tracking_issue}]({request.tracking_issue_url})"
    )
    return (
        f"<!-- agricola:source={request.source.repo}#{request.source.pr} "
        f"target={request.target} -->\n"
        "## Motivation\n\n"
        f"Propagate the canonical behavior from {source} to "
        f"`{request.target_repo}`.\n\n"
        "## Summary\n\n"
        "- Ports the canonical behavior to the target SDK's idioms.\n"
        f"- Links the review context in the {tracking}.\n\n"
        "## Key design considerations\n\n"
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
