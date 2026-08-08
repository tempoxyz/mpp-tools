# Agricola

Agricola is the GitHub-native control plane for propagating reviewed changes from the canonical `wevm/mppx` SDK to downstream MPP SDKs.

It:

- validates the reviewed [`sdks.yaml`](../sdks.yaml) manifest;
- polls merged canonical pull requests with a durable Git-backed cursor;
- reconstructs authorized `agricola:*` labels as they existed at merge time;
- creates one tracking issue per actionable canonical change;
- accepts maintainer-only `plan`, `propagate`, `status`, and `skip` commands;
- generates idiomatic downstream changes, runs each SDK's declared verification, and opens draft pull requests;
- records immutable source snapshots and completed decisions under [`ledger/`](../ledger/).

Agricola never auto-merges a downstream pull request. Maintainers retain review and merge control.

## Requirements and installation

Agricola requires Python 3.12 or newer and the GitHub CLI (`gh`) for live operations.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/agricola validate
```

`validate` rejects duplicate YAML keys and unknown fields, then validates the manifest, ledger, and cursor. Pydantic models are the source of truth for validation and generated JSON Schemas:

```bash
.venv/bin/agricola schema > schemas.json
```

No checked-in schema copies need synchronization.

## Configuration

[`sdks.yaml`](../sdks.yaml) is the reviewed source of truth. It contains:

- `maintainers`: GitHub logins authorized to apply effective labels and issue commands;
- `canonical` and `spec`: repository names in `OWNER/REPO` form;
- `sdks`: immutable target definitions keyed by lowercase target name;
- `automation`: `pr` for managed propagation or `notify` for external targets;
- repository, owners, changelog convention, and verification commands for each target.

Every `automation: pr` target must declare at least one verification command. The executor runs these commands after generation and before it receives downstream write credentials.

## Labels

Apply labels to the canonical pull request before merging:

| Label | Effect |
| --- | --- |
| `agricola:all` | Queue every manifest target with `automation: pr`. |
| `agricola:<target>` | Queue the named target; target labels are additive. |
| `agricola:none` | Disable propagation. A clean `none` result is recorded without a tracking issue. |
| No Agricola label | Create a tracking issue and await a command. |

Only the last label event at or before merge counts, and its actor must be in `maintainers`. Later label edits cannot change the snapshot. Unknown authorized labels create a diagnostic plan. `agricola:none` wins over other labels, but conflicts and errors remain visible in a diagnostic tracking issue.

The workflow reads canonical label events with a GitHub App installation token. This is required because the `mpp-tools` repository token cannot reliably read cross-repository timeline events.

## Impact plans

Each tracking issue contains the stable marker `<!-- agricola:source=OWNER/REPO#NUMBER -->` and separates changed files into:

1. normative specification or conformance files;
2. canonical behavior worth matching;
3. incidental repository or TypeScript tooling files.

Agricola does not infer SDK applicability from keywords or feature tags. Authorized merge-time labels and maintainer commands are the only propagation decisions. Plans show the selected targets and the manifest's general target inventory; `automation: notify` targets remain notification-only.

The tracking issue also contains a durable downstream propagation table. It lists every target as awaiting a decision, queued, skipped, notification-only, or recorded, and links each recorded downstream pull request. Table updates are delivered only after the corresponding ledger state has been persisted.

## Commands

`@agricola` must be the first token on a line. Commands are accepted only from configured maintainers and only on Agricola tracking issues in `mpp-tools`.

```text
@agricola plan
@agricola propagate go rust
@agricola propagate all
@agricola status
@agricola skip ruby reason="TS-only tooling"
```

- `plan` regenerates the impact plan from the immutable merge-time label history.
- `propagate <targets...>` queues explicit `automation: pr` targets.
- `propagate all` queues every `automation: pr` target.
- `status` queries GitHub for downstream pull requests already recorded in the ledger.
- `skip` appends an idempotent, reason-required decision for one manifest target.

Commands in canonical or downstream repositories are not supported. Revision, retry, audit, and downstream issue commands remain manual operations. A failed unpublished propagation can be retried by rerunning its workflow; a published stable branch is updated idempotently.

## Downstream execution

Every source-target pair uses a stable branch such as `agricola/mppx-412`. The source SHA and target define a stable idempotency key, so overlapping polls and repeated commands do not create duplicate pull requests.

The executor pins the target repository's default-branch commit when it creates a
propagation request. Generation, verification, and publication all use that exact
target tree, even if the default branch advances while the workflow is running.

The executor:

1. checks out the downstream repository, exact canonical merge commit, specification, reviewed plan, and target conventions;
2. generates the smallest idiomatic downstream patch without repository credentials;
3. transfers only that patch to a separate job and runs the manifest verification commands without secrets;
4. mints a target-scoped GitHub App token only after verification succeeds;
5. creates or updates the stable branch and opens a draft pull request;
6. records the propagation decision only after GitHub returns the pull-request reference.

Generation and verification failure leave no recorded propagate decision, so the same request remains retryable. Successful publications are recorded even when another target in the same matrix fails. A closed stable pull request must be reopened before retrying. An existing ready-for-review pull request is returned to draft before its branch is updated, and a merged pull request is treated as the completed result.

## State and recovery

The poller creates `ledger/cursor.json` on its first run. GitHub Actions restores ledger data from `agricola/state` and updates one stable state pull request, following the changelog release-PR pattern. Merge that pull request to checkpoint the ledger on the default branch. Do not close or edit it manually. The next state change creates or updates its successor, so at most one state pull request remains open.

Executable control-plane code always comes from the protected default branch. The initial cursor starts fifteen minutes behind the current time; combined with the one-hour replay overlap, the first API read covers approximately the previous 75 minutes. Later polls retain the overlap and deduplicate using source snapshots and decisions.

Ledger filenames identify the canonical repository and pull request. Entries contain immutable source metadata, the authorized merge-time label snapshot, and discriminated decisions:

- `skip` requires a reason and forbids a pull-request reference;
- `propagate` requires a downstream pull-request reference and is written only after publication.

Tracking issue deduplication scans the control repository's issue API directly for the stable marker; it does not depend on search indexing.

State-changing replies are deferred until the state pull request has been updated. A failed state update therefore cannot leave a misleading acknowledgement. Reruns reuse deterministic idempotency keys.

## CLI reference

| Command | Purpose |
| --- | --- |
| `agricola validate` | Validate the manifest, ledger, and cursor. |
| `agricola schema` | Print generated manifest, ledger, and cursor JSON Schemas. |
| `agricola poll` | Process merged canonical pull requests. |
| `agricola handle-comment [event]` | Parse an `issue_comment` event; defaults to `GITHUB_EVENT_PATH`. |
| `agricola deliver-reply <file>` | Deliver a reply deferred until after Git persistence. |
| `agricola deliver-issue-update <file>` | Deliver a tracking issue body update deferred until after Git persistence. |
| `agricola record-propagations <results>` | Record published pull requests and render deferred replies. |
| `agricola verify-propagation <request>` | Run the target's reviewed verification commands. |
| `agricola render-propagation <request>` | Render deterministic pull-request metadata. |
| `agricola parse-command --author <login>` | Parse commands from standard input for diagnostics. |

Live control-plane commands authenticate through `GH_TOKEN`; canonical polling can use a separate repository token:

```bash
GH_TOKEN=... AGRICOLA_CANONICAL_TOKEN=... \
  .venv/bin/agricola poll --control-repo tempoxyz/mpp-tools
```

See [Agricola Actions setup](../docs/agricola-actions.md) for GitHub App, secret, and deployment configuration.

## Development verification

With Ruff, `ty`, `uv`, Go, and Actionlint available:

```bash
.venv/bin/python -m unittest discover -s agricola/tests -v
.venv/bin/ruff format --check agricola
.venv/bin/ruff check agricola
uvx ty check agricola
go run github.com/rhysd/actionlint/cmd/actionlint@latest .github/workflows/agricola.yml
```
