#!/usr/bin/env python3
"""Open, update, or close one issue per SDK from the daily SDK conformance run.

The SDK Conformance Monitor workflow tests the head of each SDK repository's
default branch and passes its jobs' results here as NEEDS (toJSON(needs)). A
failing SDK gets one open issue labeled conformance-failure, with a comment
whenever a new SDK commit still fails; the issue closes once the SDK passes.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

LABEL = "conformance-failure"
LABEL_COLOR = "B60205"
LABEL_DESCRIPTION = "Daily conformance run failing"


@dataclass(frozen=True)
class Sdk:
    """An SDK repository the daily run tests, keyed by its monitor job ID."""

    adapter: str
    repository: str
    language_label: str

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[1]


SDKS: dict[str, Sdk] = {
    "sdk-rust": Sdk("rust", "tempoxyz/mpp-rs", "rust"),
    "sdk-python": Sdk("python", "tempoxyz/pympp", "python"),
    "sdk-go": Sdk("go", "tempoxyz/mpp-go", "go"),
    "sdk-typescript": Sdk("typescript", "wevm/mppx", "javascript"),
    "sdk-ruby": Sdk("ruby", "stripe/mpp-rb", "ruby"),
}


def issue_title(sdk: Sdk) -> str:
    """Title of the one open issue for a failing SDK."""
    return f"Conformance failing: {sdk.repository} ({sdk.adapter})"


def action_for(result: str) -> str:
    """Map a reusable workflow job result to fail, pass, or skip."""
    if result == "success":
        return "pass"
    if result == "failure":
        return "fail"
    # cancelled or skipped runs say nothing about the SDK.
    return "skip"


def commit_ref(repository: str, sha: str) -> str:
    if not sha:
        return "an unknown commit"
    return f"[`{sha[:12]}`](https://github.com/{repository}/commit/{sha})"


def failed_step_lines(failed: list[tuple[str, str, str]]) -> str:
    if not failed:
        return "- See the run for the failing step."
    return "\n".join(f"- [{job}]({url}): {step}" for job, step, url in failed)


# Commands the reusable workflow runs around use_local_sdk.py for an adapter, so
# the reproduction exercises the SDK checkout rather than the pinned release.
SDK_PREPARE: dict[str, str] = {
    "typescript": "(cd ../../sdk && corepack enable && pnpm install --frozen-lockfile && pnpm build)\n",
}
ADAPTER_INSTALL: dict[str, str] = {
    "typescript": "npm install\n",
    "go": "(cd adapters/go && GOTOOLCHAIN=auto go mod download)\n",
    "python": "(cd adapters/python && uv sync --python 3.12)\n",
    "rust": "(cd adapters/rust && cargo fetch)\n",
}


def failure_body(
    sdk: Sdk,
    sha: str,
    run_url: str,
    tools_repository: str,
    tools_sha: str,
    failed: list[tuple[str, str, str]],
) -> str:
    """Body of a new issue for a failing SDK."""
    return f"""The daily SDK conformance run tested `{sdk.repository}` at {commit_ref(sdk.repository, sha)} against {tools_repository} at {commit_ref(tools_repository, tools_sha)}, and it failed.

**Run:** {run_url}

### Failing steps

{failed_step_lines(failed)}

### What to do

Fix the SDK, or the vector or adapter here if the conformance suite is wrong. This issue gets a comment when a newer SDK commit still fails, and closes itself once the daily run passes.

### Reproduce

```bash
git clone https://github.com/{sdk.repository} sdk
git clone https://github.com/{tools_repository} mpp-tools
cd mpp-tools/conformance
make install-runner
{SDK_PREPARE.get(sdk.adapter, "")}make use-local-sdk ADAPTER={sdk.adapter} SDK_PATH="$(cd ../../sdk && pwd)"
{ADAPTER_INSTALL.get(sdk.adapter, "")}make test-sdk ADAPTER={sdk.adapter}
make flow-sdk ADAPTER={sdk.adapter}
```
"""


def failure_comment(sdk: Sdk, sha: str, run_url: str, failed: list[tuple[str, str, str]]) -> str:
    """Comment on an open issue when a newer SDK commit still fails."""
    return f"""Still failing at {commit_ref(sdk.repository, sha)}.

**Run:** {run_url}

{failed_step_lines(failed)}
"""


def gh(*args: str, token: str | None = None) -> str:
    env = None
    if token:
        env = {**os.environ, "GH_TOKEN": token}
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, env=env).stdout


def failed_steps(repository: str, run_id: str, sdk: Sdk) -> list[tuple[str, str, str]]:
    """Failed steps of the SDK's jobs in this run, as (job, step, job URL)."""
    try:
        output = gh(
            "api",
            "--paginate",
            f"repos/{repository}/actions/runs/{run_id}/jobs",
            "--jq",
            ".jobs[] | @json",
            token=os.environ.get("ACTIONS_TOKEN"),
        )
    except subprocess.CalledProcessError:
        return []
    failed = []
    prefix = f"{sdk.name} / "
    for line in output.splitlines():
        job = json.loads(line)
        if not job.get("name", "").startswith(prefix):
            continue
        for step in job.get("steps") or []:
            if step.get("conclusion") == "failure":
                failed.append((job["name"], step["name"], job["html_url"]))
    return failed


def main() -> None:
    needs = json.loads(os.environ["NEEDS"])
    repository = os.environ["GH_REPO"]
    run_id = os.environ["RUN_ID"]
    tools_sha = os.environ.get("TOOLS_SHA", "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"

    open_issues = json.loads(
        gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "200", "--json", "number,title")
    )
    by_title = {issue["title"]: issue["number"] for issue in open_issues}
    label_ready = False

    for job_id, sdk in SDKS.items():
        need = needs.get(job_id) or {}
        sha = (need.get("outputs") or {}).get("sdk_sha", "")
        action = action_for(need.get("result", ""))
        existing = by_title.get(issue_title(sdk))
        print(f"{sdk.repository}: {need.get('result', 'missing')} at {sha or 'unknown'}")

        if action == "pass" and existing:
            gh(
                "issue",
                "close",
                str(existing),
                "--comment",
                f"Passing again at {commit_ref(sdk.repository, sha)}. Run: {run_url}",
            )
        elif action == "fail":
            failed = failed_steps(repository, run_id, sdk)
            if existing:
                seen = gh("issue", "view", str(existing), "--json", "body,comments", "--jq", ".body, .comments[].body")
                # Without a recorded commit, comment on every failing run.
                if sha and sha in seen:
                    continue
                gh("issue", "comment", str(existing), "--body", failure_comment(sdk, sha, run_url, failed))
            else:
                if not label_ready:
                    gh("label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force")
                    label_ready = True
                gh(
                    "issue",
                    "create",
                    "--title",
                    issue_title(sdk),
                    "--body",
                    failure_body(sdk, sha, run_url, repository, tools_sha, failed),
                    "--label",
                    LABEL,
                    "--label",
                    sdk.language_label,
                )


if __name__ == "__main__":
    main()
