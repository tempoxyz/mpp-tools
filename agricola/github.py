from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import CanonicalChange, Cursor, LabelAction, LabelEvent, PullRequestFile

_SOURCE_MARKER = re.compile(
    r"<!--\s*agricola:source=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\s*-->"
)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, control_repo: str, *, token: str | None = None) -> None:
        self.control_repo = control_repo
        self.environment = os.environ.copy()
        if token:
            self.environment["GH_TOKEN"] = token

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
        process = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=self.environment,
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
        files = self.pull_request_files(repo, number)
        return CanonicalChange(
            repo=repo,
            number=number,
            sha=item["merge_commit_sha"] or item["head"]["sha"],
            title=item["title"],
            url=item["html_url"],
            body=item.get("body") or "",
            merged_at=_time(item.get("merged_at") or item["updated_at"]),
            labels=tuple(label["name"] for label in item.get("labels", [])),
            files=files,
        )

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
        pages = self.api(
            f"repos/{self.control_repo}/issues?state=all&per_page=100",
            paginate=True,
        )
        return next(
            (
                item
                for page in pages
                for item in page
                if marker in (item.get("body") or "")
            ),
            None,
        )

    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> dict[str, object]:
        fields: dict[str, object] = {"title": title, "body": body}
        if labels:
            fields["labels"] = list(labels)
        return self.api(
            f"repos/{self.control_repo}/issues", method="POST", fields=fields
        )

    def update_issue(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, object]:
        fields: dict[str, object] = {
            name: value
            for name, value in (("title", title), ("body", body))
            if value is not None
        }
        return self.api(
            f"repos/{self.control_repo}/issues/{number}", method="PATCH", fields=fields
        )

    def comment_issue(self, number: int, body: str) -> dict[str, object]:
        return self.api(
            f"repos/{self.control_repo}/issues/{number}/comments",
            method="POST",
            fields={"body": body},
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


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result.astimezone(UTC)
