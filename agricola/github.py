from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    CanonicalChange,
    Cursor,
    LabelAction,
    LabelEvent,
    PropagationRevision,
    PullRequestComment,
    PullRequestFile,
)

_SOURCE_MARKER = re.compile(
    r"<!--\s*agricola:source=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\s*-->"
)
_AGRICOLA_LABEL_COLOR = "6f42c1"
_SDK_LABEL_COLOR = "bfdadc"


def agricola_issue_labels(targets: Iterable[str] = ()) -> tuple[str, ...]:
    return ("agricola", *sorted(set(targets)))


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        control_repo: str,
        *,
        token: str | None = None,
        repo_tokens: dict[str, str] | None = None,
    ) -> None:
        self.control_repo = control_repo
        self.environment = os.environ.copy()
        if token:
            self.environment["GH_TOKEN"] = token
        self.repo_tokens = repo_tokens or {}
        self._known_issue_labels: set[str] | None = None

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, object] | None = None,
        paginate: bool = False,
    ) -> Any:
        command = ["gh", "api", endpoint, "--method", method]
        if paginate:
            command.extend(["--paginate", "--slurp"])
        input_text = None
        if fields is not None:
            if method == "GET":
                for name, value in fields.items():
                    command.extend(["-f", f"{name}={value}"])
            else:
                command.extend(["--input", "-"])
                input_text = json.dumps(fields)
        environment = self.environment.copy()
        match = re.match(r"repos/([^/]+/[^/]+)(?:/|$)", endpoint)
        if match and (repo_token := self.repo_tokens.get(match.group(1))):
            environment["GH_TOKEN"] = repo_token
        process = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        if process.returncode:
            message = (
                process.stderr.strip()
                or process.stdout.strip()
                or f"gh exited {process.returncode}"
            )
            raise GitHubError(f"GitHub API {method} {endpoint} failed: {message}")
        try:
            return json.loads(process.stdout) if process.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"GitHub API returned invalid JSON for {endpoint}"
            ) from exc

    def graphql(self, query: str, variables: dict[str, object]) -> Any:
        return self.api(
            "graphql",
            method="POST",
            fields={"query": query, "variables": variables},
        )

    def failed_run_log(self, repo: str, run_id: int) -> str:
        environment = self.environment.copy()
        if repo_token := self.repo_tokens.get(repo):
            environment["GH_TOKEN"] = repo_token
        process = subprocess.run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--log-failed",
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        if process.returncode:
            message = process.stderr.strip() or f"gh exited {process.returncode}"
            raise GitHubError(
                f"could not read failed logs for {repo} run {run_id}: {message}"
            )
        return process.stdout

    def merged_changes(
        self,
        repo: str,
        cursor: Cursor,
        *,
        overlap: timedelta = timedelta(hours=1),
    ) -> list[CanonicalChange]:
        cutoff = cursor.merged_at - overlap
        changes: list[CanonicalChange] = []
        page = 1
        while True:
            items = self.api(
                f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}"
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub pull request listing returned a non-list")
            for item in items:
                if not item.get("merged_at"):
                    continue
                merged_at = _time(item["merged_at"])
                if merged_at < cutoff:
                    continue
                changes.append(
                    CanonicalChange(
                        repo=repo,
                        number=int(item["number"]),
                        sha=item["merge_commit_sha"],
                        title=item["title"],
                        url=item["html_url"],
                        body=item.get("body") or "",
                        merged_at=merged_at,
                        labels=tuple(label["name"] for label in item.get("labels", [])),
                    )
                )
            if len(items) < 100 or not items or _time(items[-1]["updated_at"]) < cutoff:
                break
            page += 1
        return sorted(changes, key=lambda change: (change.merged_at, change.number))

    def pull_request(self, repo: str, number: int) -> CanonicalChange:
        item = self.api(f"repos/{repo}/pulls/{number}")
        if not item.get("merged_at"):
            raise GitHubError(f"{repo}#{number} is not merged")
        files = self.pull_request_files(repo, number)
        return CanonicalChange(
            repo=repo,
            number=number,
            sha=item["merge_commit_sha"] or item["head"]["sha"],
            title=item["title"],
            url=item["html_url"],
            body=item.get("body") or "",
            merged_at=_time(item["merged_at"]),
            labels=tuple(label["name"] for label in item.get("labels", [])),
            files=files,
        )

    def repository_head(self, repo: str) -> str:
        repository = self.api(f"repos/{repo}")
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GitHubError(f"{repo} has no default branch")
        commit = self.api(f"repos/{repo}/commits/{branch}")
        sha = commit.get("sha")
        if not isinstance(sha, str) or not sha:
            raise GitHubError(f"could not resolve the default branch head for {repo}")
        return sha

    def pull_request_files(self, repo: str, number: int) -> tuple[PullRequestFile, ...]:
        pages = self.api(
            f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True
        )
        return tuple(
            PullRequestFile(
                item["filename"], item["status"], item["additions"], item["deletions"]
            )
            for page in pages
            for item in page
        )

    def label_events(self, repo: str, number: int) -> tuple[LabelEvent, ...]:
        pages = self.api(
            f"repos/{repo}/issues/{number}/events?per_page=100", paginate=True
        )
        events: list[LabelEvent] = []
        for page in pages:
            for item in page:
                if item.get("event") not in {"labeled", "unlabeled"} or not item.get(
                    "label"
                ):
                    continue
                events.append(
                    LabelEvent(
                        LabelAction(item["event"]),
                        item["label"]["name"],
                        item["actor"]["login"],
                        _time(item["created_at"]),
                    )
                )
        return tuple(events)

    def find_tracking_issue(self, marker: str) -> dict[str, object] | None:
        return next(iter(self.find_tracking_issues(marker)), None)

    def find_tracking_issues(self, marker: str) -> tuple[dict[str, object], ...]:
        pages = self.api(
            f"repos/{self.control_repo}/issues?state=all&per_page=100",
            paginate=True,
        )
        return tuple(
            item
            for page in pages
            for item in page
            if marker in (item.get("body") or "")
        )

    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> dict[str, object]:
        self._ensure_issue_labels(labels)
        fields: dict[str, object] = {"title": title, "body": body}
        if labels:
            fields["labels"] = list(labels)
        return self.api(
            f"repos/{self.control_repo}/issues", method="POST", fields=fields
        )

    def _ensure_issue_labels(self, labels: Sequence[str]) -> None:
        if not labels:
            return
        if self._known_issue_labels is None:
            pages = self.api(
                f"repos/{self.control_repo}/labels?per_page=100", paginate=True
            )
            self._known_issue_labels = {
                str(item["name"]).casefold() for page in pages for item in page
            }
        for label in labels:
            normalized = label.casefold()
            if normalized in self._known_issue_labels:
                continue
            fields: dict[str, object] = {
                "name": label,
                "color": (
                    _AGRICOLA_LABEL_COLOR
                    if normalized == "agricola"
                    else _SDK_LABEL_COLOR
                ),
                "description": (
                    "Issues managed by Agricola"
                    if normalized == "agricola"
                    else f"Issues affecting the {label} SDK"
                ),
            }
            self.api(f"repos/{self.control_repo}/labels", method="POST", fields=fields)
            self._known_issue_labels.add(normalized)

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, object]:
        fields: dict[str, object] = {
            name: value
            for name, value in (("title", title), ("body", body), ("state", state))
            if value is not None
        }
        return self.api(
            f"repos/{self.control_repo}/issues/{number}", method="PATCH", fields=fields
        )

    def comment_issue(self, number: int, body: str) -> dict[str, object]:
        return self.comment_repository_issue(self.control_repo, number, body)

    def comment_repository_issue(
        self, repository: str, number: int, body: str
    ) -> dict[str, object]:
        return self.api(
            f"repos/{repository}/issues/{number}/comments",
            method="POST",
            fields={"body": body},
        )

    def pull_request_comments(self, reference: str) -> tuple[PullRequestComment, ...]:
        repository, number = reference.rsplit("#", 1)
        pages = self.api(
            f"repos/{repository}/issues/{int(number)}/comments?per_page=100",
            paginate=True,
        )
        return tuple(
            PullRequestComment(
                id=int(item["id"]),
                body=str(item.get("body") or ""),
                author=str((item.get("user") or {}).get("login") or ""),
                created_at=_time(item["created_at"]),
                has_eyes=int((item.get("reactions") or {}).get("eyes") or 0) > 0,
            )
            for page in pages
            for item in page
        )

    def react_to_issue_comment(
        self, repository: str, comment_id: int, content: str = "eyes"
    ) -> None:
        self.api(
            f"repos/{repository}/issues/comments/{comment_id}/reactions",
            method="POST",
            fields={"content": content},
        )

    def source_from_body(self, body: str) -> tuple[str, int]:
        match = _SOURCE_MARKER.search(body)
        if not match:
            raise GitHubError("issue is not an Agricola tracking issue")
        return match.group(1), int(match.group(2))

    def pull_status(self, reference: str) -> str:
        repo, number = reference.rsplit("#", 1)
        pull = self.api(f"repos/{repo}/pulls/{int(number)}")
        draft = "draft" if pull.get("draft") else "ready"
        return f"{reference}: {pull['state']} ({draft}) — {pull['html_url']}"

    def pull_revision(
        self, reference: str, expected_branch: str
    ) -> PropagationRevision:
        repo, number = reference.rsplit("#", 1)
        pull = self.api(f"repos/{repo}/pulls/{int(number)}")
        if pull.get("state") != "open":
            raise GitHubError(f"{reference} is not open")
        head = pull.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        if head_repo != repo or head.get("ref") != expected_branch:
            raise GitHubError(f"{reference} does not use {repo}:{expected_branch}")
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise GitHubError(f"{reference} has no head commit")
        return PropagationRevision(
            pr=reference,
            url=pull["html_url"],
            head_sha=head_sha,
        )


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result.astimezone(UTC)
