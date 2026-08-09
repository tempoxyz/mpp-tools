from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import PropagationRequest, PropagationResult

_REVISION_TRAILER = "Agricola-Request"
_REVISION_SUMMARY_TRAILER = "Agricola-Summary"


class PublicationError(RuntimeError):
    pass


class Git(Protocol):
    def fetch_branch(self, branch: str) -> str: ...
    def remote_branch_sha(self, branch: str) -> str | None: ...
    def push(self, branch: str, *, expected_remote: str | None = None) -> None: ...
    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...
    def commits_between(self, ancestor: str, descendant: str) -> tuple[str, ...]: ...
    def trailer(self, commit: str, key: str) -> str: ...
    def stat(self, commit: str) -> str: ...


class GitHub(Protocol):
    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, object] | None = None,
        paginate: bool = False,
    ) -> Any: ...

    def graphql(self, query: str, variables: dict[str, object]) -> Any: ...


class SubprocessGit:
    def __init__(self, root: str = ".") -> None:
        self.root = root

    def _run(
        self, arguments: list[str], *, allowed_exit_codes: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode not in allowed_exit_codes:
            message = process.stderr.strip() or process.stdout.strip()
            raise PublicationError(
                f"git {' '.join(arguments)} failed ({process.returncode}): {message}"
            )
        return process

    def fetch_branch(self, branch: str) -> str:
        remote_ref = f"refs/remotes/origin/{branch}"
        self._run(
            [
                "fetch",
                "--force",
                "origin",
                f"refs/heads/{branch}:{remote_ref}",
            ]
        )
        return self._run(["rev-parse", remote_ref]).stdout.strip()

    def remote_branch_sha(self, branch: str) -> str | None:
        output = self._run(
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
        ).stdout.strip()
        return output.split(maxsplit=1)[0] if output else None

    def push(self, branch: str, *, expected_remote: str | None = None) -> None:
        arguments = ["push"]
        if expected_remote is not None:
            arguments.append(
                f"--force-with-lease=refs/heads/{branch}:{expected_remote}"
            )
        arguments.extend(["origin", f"HEAD:refs/heads/{branch}"])
        self._run(arguments)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        process = self._run(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            allowed_exit_codes=(0, 1),
        )
        return process.returncode == 0

    def commits_between(self, ancestor: str, descendant: str) -> tuple[str, ...]:
        output = self._run(["rev-list", f"{ancestor}..{descendant}"]).stdout
        return tuple(line for line in output.splitlines() if line)

    def trailer(self, commit: str, key: str) -> str:
        return self._run(
            ["show", "-s", f"--format=%(trailers:key={key},valueonly)", commit]
        ).stdout.strip()

    def stat(self, commit: str) -> str:
        return self._run(["show", "--stat", "--format=", "--no-renames", commit]).stdout


def publish(
    request: PropagationRequest,
    *,
    title: str,
    body: str,
    git: Git,
    github: GitHub,
    revision_summary: str | None = None,
    revision_stat: str | None = None,
    at: datetime | None = None,
) -> PropagationResult:
    """Publish one verified commit to its stable branch and draft pull request."""
    existing = _find_existing_pull(github, request.target_repo, request.branch)
    state = _pull_state(existing)
    expected_pr = request.revision.pr if request.revision is not None else None
    if expected_pr is not None:
        actual_pr = (
            f"{request.target_repo}#{existing['number']}"
            if existing is not None
            else ""
        )
        if expected_pr != actual_pr:
            raise PublicationError(
                "recorded revision pull request does not match the stable branch"
            )
        if state != "OPEN":
            raise PublicationError("recorded revision pull request is no longer open")

    if state == "MERGED":
        assert existing is not None
        return _result(request, existing, at)
    if state == "CLOSED":
        raise PublicationError(
            "existing Agricola pull request is closed; reopen it before retrying"
        )

    if existing is not None and not bool(existing.get("draft")):
        _convert_to_draft(github, existing)

    summary = revision_summary
    stat = revision_stat
    if request.revision is not None:
        if not summary or stat is None:
            raise PublicationError("revision publication requires a summary and stat")
        remote = git.fetch_branch(request.branch)
        if remote == request.revision.head_sha:
            git.push(request.branch)
        else:
            if not git.is_ancestor(request.revision.head_sha, remote):
                raise PublicationError(
                    "revision branch no longer descends from the recorded pull request head"
                )
            published = _published_revision(git, request, remote)
            if published is None:
                raise PublicationError("revision branch changed before publication")
            summary = git.trailer(published, _REVISION_SUMMARY_TRAILER)
            if not summary:
                raise PublicationError("published revision has no stored summary")
            stat = git.stat(published)
    else:
        remote = git.remote_branch_sha(request.branch)
        if remote is None:
            git.push(request.branch)
        else:
            git.fetch_branch(request.branch)
            git.push(request.branch, expected_remote=remote)

    pull = existing or _create_draft_pull(github, request, title, body)
    if request.revision is not None:
        assert summary is not None and stat is not None
        _post_revision_summary(github, request, pull, summary, stat)
    return _result(request, pull, at)


def _find_existing_pull(
    github: GitHub, repository: str, branch: str
) -> dict[str, object] | None:
    owner = repository.split("/", 1)[0]
    pulls = github.api(
        f"repos/{repository}/pulls",
        fields={"state": "all", "head": f"{owner}:{branch}", "per_page": 100},
    )
    if not isinstance(pulls, list):
        raise PublicationError("GitHub pull request listing returned a non-list")
    return next(
        (
            pull
            for pull in pulls
            if ((pull.get("head") or {}).get("repo") or {}).get("full_name")
            == repository
        ),
        None,
    )


def _pull_state(pull: dict[str, object] | None) -> str:
    if pull is None:
        return ""
    if pull.get("merged_at"):
        return "MERGED"
    return str(pull.get("state") or "").upper()


def _convert_to_draft(github: GitHub, pull: dict[str, object]) -> None:
    node_id = pull.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise PublicationError("existing pull request has no GraphQL node ID")
    github.graphql(
        """
        mutation AgricolaConvertToDraft($id: ID!) {
          convertPullRequestToDraft(input: {pullRequestId: $id}) {
            pullRequest { isDraft }
          }
        }
        """,
        {"id": node_id},
    )


def _published_revision(
    git: Git, request: PropagationRequest, remote: str
) -> str | None:
    assert request.revision is not None
    return next(
        (
            commit
            for commit in git.commits_between(request.revision.head_sha, remote)
            if git.trailer(commit, _REVISION_TRAILER) == request.idempotency_key
        ),
        None,
    )


def _create_draft_pull(
    github: GitHub,
    request: PropagationRequest,
    title: str,
    body: str,
) -> dict[str, object]:
    repository = github.api(f"repos/{request.target_repo}")
    if not isinstance(repository, dict) or not repository.get("default_branch"):
        raise PublicationError("target repository has no default branch")
    pull = github.api(
        f"repos/{request.target_repo}/pulls",
        method="POST",
        fields={
            "title": title,
            "body": body,
            "head": request.branch,
            "base": str(repository["default_branch"]),
            "draft": True,
        },
    )
    if not isinstance(pull, dict):
        raise PublicationError("GitHub pull request creation returned a non-object")
    return pull


def _post_revision_summary(
    github: GitHub,
    request: PropagationRequest,
    pull: dict[str, object],
    summary: str,
    stat: str,
) -> None:
    number = int(str(pull["number"]))
    marker = f"<!-- agricola:revision-summary:{request.idempotency_key} -->"
    pages = github.api(
        f"repos/{request.target_repo}/issues/{number}/comments?per_page=100",
        paginate=True,
    )
    comments = [comment for page in pages for comment in page]
    if any(marker in str(comment.get("body") or "") for comment in comments):
        return
    comment = (
        f"{marker}\n"
        "Agricola updated this pull request from the latest review feedback and "
        "failing CI.\n\n"
        "### Summary of changes\n\n"
        + "\n".join(f"- {line}" for line in summary.splitlines())
        + "\n\n### Changed files\n\n```text\n"
        + stat.rstrip()
        + "\n```\n\n"
        f"Requested from {request.tracking_issue_url}."
    )
    github.api(
        f"repos/{request.target_repo}/issues/{number}/comments",
        method="POST",
        fields={"body": comment},
    )


def _result(
    request: PropagationRequest,
    pull: dict[str, object],
    at: datetime | None,
) -> PropagationResult:
    number = int(str(pull["number"]))
    url = str(pull.get("html_url") or "")
    if not url:
        raise PublicationError("GitHub pull request has no URL")
    return PropagationResult(
        request=request,
        pr=f"{request.target_repo}#{number}",
        url=url,
        at=(at or datetime.now(UTC)),
    )
