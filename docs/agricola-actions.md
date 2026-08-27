# Agricola Actions fallback

> [!NOTE]
> Agricola now runs primarily through [Auto](./agricola-auto.md). These workflows
> are manual rollback paths; they no longer poll, audit on a schedule, or react
> to issue comments automatically.

Agricola propagation runs from [`.github/workflows/agricola.yml`](../.github/workflows/agricola.yml), and the head-to-head SDK audit runs from [`.github/workflows/agricola-audit.yml`](../.github/workflows/agricola-audit.yml). The repository-scoped `GITHUB_TOKEN` manages state, while a GitHub App supplies short-lived tokens for control-plane issue activity and cross-repository access. The official OpenAI action generates downstream patches and performs read-only semantic audits.

Both semantic analysis and downstream generation explicitly use `gpt-5.6-sol`. Downstream verification installs the pinned `uv` version before running manifest commands such as `uv sync --all-extras --dev` and `uv run pytest`.

## Triggers

| Trigger | Behavior |
| --- | --- |
| `workflow_dispatch` | Run the legacy poller manually. |

The audit workflow supports manual `workflow_dispatch`. Every run re-baselines each manifest SDK against the same pinned current `mppx` head. Each matrix job checks out the exact current head of its SDK and the pinned `mppx` head, runs an open-ended read-only Codex comparison, and requires schema-validated findings with linked code evidence. Shared vectors and conformance-adapter capabilities remain deterministic supporting signals. Agricola clusters matching `semantic:`, `vector:`, and `capability:` fingerprints, maintains one issue per finding, and updates a roll-up index. The audit itself is read-only outside `mpp-tools`; downstream publication requires a maintainer's explicit command on a finding issue.

Semantic findings are open-ended review results. If another SDK review does not report the same fingerprint, the roll-up says `not reported`; only deterministic capability and conformance checks can report an SDK as `clean`. The audit does not infer a likely origin from affected-target counts.

Issue-scoped workflow concurrency prevents overlapping fixes for the same ticket. State writers for different issues run independently: each restores only `ledger` from the current `agricola/state` commit, replays its idempotent operation on a trusted default-branch checkout, and pushes a complete snapshot with `git push --force-with-lease`. A competing update rejects the stale push, so the writer fetches the winner and replays instead of dropping or merging its change. Generation and verification for different issues remain parallel. The workflow maintains at most one state pull request. Merge it to checkpoint state on the default branch. Do not close or edit it manually. Code from the state branch is never executed.

## Required GitHub App

Create one GitHub App and install it for these repositories:

- `wevm/mppx`;
- `tempoxyz/mpp-tools`;
- every `automation: pr` target currently listed in [`sdks.yaml`](../sdks.yaml): `mpp-rs` and `pympp`.

Grant repository permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Contents | Read and write | Read canonical commits; create downstream branches. |
| Issues | Read and write | Read canonical label events and trusted PR comments; create audit issues and post App-attributed replies and revision summaries. |
| Pull requests | Read and write | Read canonical metadata; create downstream draft pull requests. |
| Actions | Read | Read failed downstream GitHub Actions logs during revision. |
| Checks | Read | Read downstream check summaries and annotations during revision. |
| Workflows | Read and write | Update downstream workflow files when a generated remediation requires it. |

The workflow requests only read access when minting the `wevm/mppx` token. It mints an issues-write token scoped to `mpp-tools` so issue activity uses the App identity and icon. PR-enabled repositories are derived from `sdks.yaml`; they must share one GitHub owner because installation tokens are owner-scoped. Revision and publication matrix jobs mint tokens for only their current target, and write access is requested only after verification. Adding a target therefore changes the manifest rather than a duplicated workflow list. Configure the App's public logo in its GitHub settings to control the displayed avatar.

Configure `tempoxyz/mpp-tools` with:

| Kind | Name | Value |
| --- | --- | --- |
| Repository variable | `AGRICOLA_APP_ID` | GitHub App ID. |
| Actions secret | `AGRICOLA_APP_PRIVATE_KEY` | Complete GitHub App private key in PEM format. |
| Actions secret | `OPENAI_API_KEY` | API key used by downstream generation and semantic SDK audits. |

Do not merge the executor until all three values are present. The poll job mints the canonical token on every run, so missing App configuration stops even a manual poll rather than silently accepting unverifiable label actors.

## Repository Actions permissions

The workflow's top-level permissions allow the control-plane `GITHUB_TOKEN` to:

- write contents for the state branch;
- maintain the state pull request.

Short-lived GitHub App tokens create and update tracking issues, audit issues, reactions, and replies.

In repository Actions settings, enable workflow write access and allow Actions to create pull requests. No direct default-branch push or branch-rule exception is required.

## Execution boundary

Propagation is split into credential boundaries:

1. `run` reads canonical state with a read-only App token, uses a control-repository App token for issue activity, and commits ledger state through a guarded, replayable Git transaction using `GITHUB_TOKEN`.
2. `generate` checks out the pinned downstream base or exact recorded PR head without persisted credentials. For revisions, a short-lived read token gathers bounded trusted-author review feedback and failed CI before Sol runs. The token is not passed to the generation action. Sol runs the reviewed manifest commands before emitting a patch.
3. `verify` checks out the same base or PR head, receives only the generated binary patch, and independently repeats the reviewed manifest commands in a separate, secretless job. An explicit initial-generation no-op skips verification.
4. `publish` checks out the same base or PR head only after verification, mints a short-lived target token, applies the verified patch, and creates or updates the stable draft pull request. Revisions fast-forward the exact recorded head and post a concise change summary. No-op targets never request write credentials.
5. `record` replays every successful pull-request reference or explicit skip against the newest ledger until its guarded push succeeds, ensures the state pull request exists, and then posts replies. Successful matrix entries remain recordable when another target fails.

Downstream code never runs in a job containing downstream write credentials. Generated changes remain drafts and are never auto-merged.

## Deployment checklist

1. Review [`sdks.yaml`](../sdks.yaml), especially maintainers, repositories, automation modes, and verification commands.
2. Create canonical labels `agricola:all`, `agricola:none`, and `agricola:<target>` for each desired target. Agricola validates labels but does not create them.
3. Create and install the GitHub App with the repositories and permissions above.
4. Add `AGRICOLA_APP_ID`, `AGRICOLA_APP_PRIVATE_KEY`, and `OPENAI_API_KEY` to `mpp-tools`.
5. Enable workflow write access and pull-request creation.
6. Run the workflow manually and confirm the poll succeeds and the state pull request contains `ledger/cursor.json`.
7. On a tracking issue, run `/ag fix <target>` and confirm the eyes reaction, generation, verification, a downstream draft pull request, and a recorded ledger decision.
8. Run the SDK audit manually and confirm the `[Agricola] SDK drift audit` index links one issue per finding and records every exact audited commit.
9. On an affected PR-enabled finding, copy and post `/ag fix` and confirm the finding links the resulting draft remediation pull request.
10. Add a review comment or failing check to that draft, post `/ag fix "address the review and CI"` on its tracking issue, and confirm the same PR receives an incremental commit and summary comment.

The initial cursor starts fifteen minutes in the past. Because each poll replays a one-hour overlap, the first API read covers approximately the previous 75 minutes. To backfill a different window, update the state pull request with a reviewed `ledger/cursor.json` containing a timezone-aware ISO 8601 `merged_at`; polling begins one hour before it.

Ledger-only state pull requests skip repository CI and SDK conformance workflow triggers. Agricola implementation changes run its dedicated tests; mixed or conformance-affecting changes retain normal conformance scope.

## Labels, commands, and idempotency

The maintainer allowlist lives in [`sdks.yaml`](../sdks.yaml). Agricola reconstructs label state from canonical issue events at or before merge. A label is effective only when its final pre-merge application came from a configured maintainer. An unlabeled change is recorded without an issue or propagation. Unknown labels and conflicts create diagnostic plans. A clean `agricola:none` result is also recorded without an issue. Post-merge edits do not change recorded state.

Commands use deferred issue updates and replies so acknowledgements follow their corresponding state change. Canonical-change tables reflect the durable decision ledger. Audit-finding tables retain linked remediation pull requests across later audit runs. Skip decisions use the comment ID and line number. Canonical propagation branches use `agricola/<canonical-repo>-<pr-number>`; audit remediation branches use `agricola/<finding-id>`. Replaying a poll, comment, job, or state overlap therefore reuses existing work instead of opening duplicate pull requests.

On an audit-finding issue, `/ag fix` generates, verifies, and opens draft fixes for all affected PR-enabled SDKs. Optional target names narrow the request. Once a PR is recorded, `/ag fix "instruction"` ingests unresolved owner/member/collaborator review feedback plus failed checks and Actions logs, revises the exact current head, verifies it, fast-forwards the same branch, and posts a summary. Accepted fixes reply with a direct link to the exact Actions run. `/ag status` reports linked remediation pull requests. Every actionable finding includes a copy-ready quick command, and valid maintainer commands receive an eyes reaction when processing begins. `/agricola` remains a compatibility alias for the command prefix.

## Manual poll

Run from the command line:

```bash
# Trigger canonical-change polling on the current default branch.
gh workflow run agricola.yml --repo tempoxyz/mpp-tools --ref main

# Show the newest polling run and its status.
gh run list --repo tempoxyz/mpp-tools --workflow agricola.yml --limit 1

# Trigger a complete SDK drift audit against current repository heads.
gh workflow run agricola-audit.yml --repo tempoxyz/mpp-tools --ref main

# Show the newest audit run and its status.
gh run list --repo tempoxyz/mpp-tools --workflow agricola-audit.yml --limit 1
```

The Actions page also exposes **Run workflow** through `workflow_dispatch`.

## Troubleshooting

- Canonical token creation fails: confirm the App ID, private key, installation on `wevm/mppx`, and requested permissions.
- A label is ignored: confirm its final application occurred before merge, was performed by a configured maintainer, and canonical timeline events were returned.
- Generation fails: inspect the generation action output and confirm `OPENAI_API_KEY` is configured. An empty patch succeeds only when the generator provides the required explicit skip reason.
- Verification fails: reproduce the listed `sdks.yaml` commands in the target repository; no downstream branch is published.
- Publication fails: confirm the App installation covers the target and grants contents/pull-request write access. Reopen a closed stable pull request before retrying.
- A command has no reply: inspect state persistence. Replies are intentionally skipped after failed ledger updates.
- A state transaction exhausts its retries: inspect competing writer runs and rerun the failed job; its event or publication artifacts remain idempotent.
- The state pull request is not created: confirm workflow write access and permission to create pull requests.
- An audit target is incomplete: inspect its SDK-tagged audit job. Missing snapshots are reported in the roll-up and make the workflow fail after the issue is updated.
- A semantic audit is incomplete: confirm `OPENAI_API_KEY` is configured and inspect the SDK-tagged `Compare SDK implementation to canonical mppx` step. The deterministic snapshot is still reported, but the audit remains unhealthy until the open-ended comparison succeeds.
- A resolved finding remains open: confirm the latest audit was healthy. Incomplete audits deliberately preserve issue state; a healthy run closes findings that no longer appear.
