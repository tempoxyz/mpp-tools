# Agricola

Agricola is the GitHub-native control plane for reviewing changes from the canonical `wevm/mppx` SDK before they reach downstream MPP SDKs.

The current control plane:

- validate the reviewed [`sdks.yaml`](../sdks.yaml) manifest;
- poll merged canonical pull requests with a durable Git-backed cursor;
- create one dry-run tracking plan per actionable canonical change;
- reconstruct authorized `agricola:*` labels as they existed at merge time;
- accept maintainer-only `plan`, `status`, and `skip` commands on tracking issues;
- record source snapshots and human decisions under [`ledger/`](../ledger/).

Agricola never creates downstream branches or pull requests, runs SDK verification commands, or invokes code generation.

## Requirements and installation

Agricola requires Python 3.12 or newer and the GitHub CLI (`gh`) for live operations.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/agricola validate
```

`validate` loads the manifest, rejects duplicate YAML keys and unknown fields, and validates every ledger entry plus an existing cursor. Pydantic models are the source of truth for validation and generated JSON Schemas:

```bash
.venv/bin/agricola schema > schemas.json
```

No checked-in schema copies need to be synchronized.

## Configuration

[`sdks.yaml`](../sdks.yaml) is the reviewed source of truth. It contains:

- `maintainers`: GitHub logins authorized to apply effective labels and issue commands;
- `canonical` and `spec`: repository names in `OWNER/REPO` form;
- `sdks`: immutable target definitions keyed by lowercase target name;
- `automation`: `pr` for future managed propagation or `notify` for external targets;
- `owners`, changelog convention, verification commands, and declared capabilities.

An `automation: pr` target must declare at least one verification command. Verification commands describe the target contract but are not executed by the current review-only control plane.

## Labels

Labels are applied to the canonical pull request:

| Label | Effect |
| --- | --- |
| `agricola:all` | Select every manifest target with `automation: pr`. |
| `agricola:<target>` | Select the named target; target labels are additive. |
| `agricola:none` | Disable propagation. A clean `none` result is recorded without a tracking issue. |
| No Agricola label | Create a tracking issue and await a command. |

Only the last label event at or before merge counts, and its actor must be in `maintainers`. Later label edits cannot change the plan or ledger snapshot. Unknown authorized labels create a diagnostic plan. `agricola:none` wins over other labels, but conflicts and errors still create a diagnostic plan rather than being silently suppressed.

Labels select targets for human review; they do not create downstream pull requests.

## Impact plans

Each tracking issue contains the stable marker `<!-- agricola:source=OWNER/REPO#NUMBER -->` and separates changed files into:

1. normative specification or conformance files;
2. canonical behavior worth matching;
3. incidental repository or TypeScript tooling files.

Applicability is deterministic:

- normative or conformance changes apply to every `automation: pr` target;
- otherwise Agricola extracts declared capability signals from the PR title, body, and file paths;
- an SDK is applicable when it declares every detected capability;
- an SDK is not applicable when detected capabilities are missing;
- with no recognized capability signal, applicability is reported as unknown rather than guessed;
- `automation: notify` targets remain notification-only.

## Commands

`@agricola` must be the first token on a line. Commands are accepted only from configured maintainers and only on Agricola tracking issues in `mpp-tools`.

```text
@agricola plan
@agricola status
@agricola skip ruby reason="TS-only tooling"
```

- `plan` regenerates the dry-run plan from the immutable merge-time label history.
- `status` queries GitHub live for downstream references already recorded in the ledger.
- `skip` appends an idempotent, reason-required decision for one manifest target.

Commands in canonical or downstream repositories require cross-repository event delivery and authentication that are not configured. Propagation, revision, retry, audit, and downstream issue commands are intentionally unavailable.

## State and recovery

The poller creates `ledger/cursor.json` on its first run. In GitHub Actions, ledger data is restored from `agricola/state`, and a stable state pull request is updated in place like a changelogs release PR. This automation-owned PR remains open and is not merged or edited manually. Executable code always comes from the protected default branch. The initial cursor starts fifteen minutes behind the current time; combined with the one-hour replay overlap, the first API read covers approximately the previous 75 minutes. This prevents both a deployment race and an unbounded historical issue flood. Later polls keep the one-hour overlap and deduplicate against ledger snapshots.

Ledger filenames identify the canonical repository and pull request. Entries contain immutable source metadata, the authorized merge-time label snapshot, and discriminated decisions:

- `skip` requires `reason` and forbids a pull-request reference;
- `propagate` requires a downstream pull-request reference; the schema can validate imported propagation records, but no current command creates them.

Tracking issue deduplication scans the control repository's issue API directly for the stable marker; it does not depend on search indexing.

The Actions workflow processes state-changing commands in three stages:

1. prepare the ledger mutation and pending reply;
2. commit the ledger to the stable state branch and create or update its pull request;
3. deliver the GitHub reply only after persistence succeeds.

A failed state PR update therefore cannot leave a misleading “Recorded” acknowledgement. Rerunning the failed job reuses the comment-and-line idempotency key.

## CLI reference

| Command | Purpose |
| --- | --- |
| `agricola validate` | Validate the manifest, ledger, and cursor. |
| `agricola schema` | Print generated manifest, ledger, and cursor JSON Schemas. |
| `agricola poll` | Process merged canonical pull requests. |
| `agricola handle-comment [event]` | Parse an `issue_comment` event; defaults to `GITHUB_EVENT_PATH`. |
| `agricola deliver-reply <file>` | Deliver a reply deferred until after Git persistence. Used internally by Actions. |
| `agricola parse-command --author <login>` | Parse commands from standard input for diagnostics. |

Live commands authenticate through `GH_TOKEN` or the existing `gh` login:

```bash
GH_TOKEN=... .venv/bin/agricola poll --control-repo tempoxyz/mpp-tools
```

See [Agricola Actions setup](../docs/agricola-actions.md) for deployment and permissions.

## Development verification

With Ruff, `ty`, `uv`, Go, and Actionlint available:

```bash
.venv/bin/python -m unittest discover -s agricola/tests -v
.venv/bin/ruff format --check agricola
.venv/bin/ruff check agricola
uvx ty check agricola
go run github.com/rhysd/actionlint/cmd/actionlint@latest .github/workflows/agricola.yml
```
