# Agricola Actions setup

Agricola propagation runs from [`.github/workflows/agricola.yml`](../.github/workflows/agricola.yml), and the head-to-head SDK audit runs from [`.github/workflows/agricola-audit.yml`](../.github/workflows/agricola-audit.yml). The repository-scoped `GITHUB_TOKEN` manages control-plane issues and state; a GitHub App supplies short-lived cross-repository tokens; the official OpenAI action generates downstream patches and performs read-only semantic audits.

## Triggers

| Trigger | Behavior |
| --- | --- |
| Ten-minute schedule | Poll merged `wevm/mppx` pull requests. GitHub may delay scheduled jobs. |
| `workflow_dispatch` | Run the poller manually. |
| New `issue_comment` containing `@agricola` | Handle a tracking-issue command. `@agricola` must be the first token on a line. |

The audit workflow runs every Monday at 09:00 UTC and supports `workflow_dispatch`. For each manifest SDK, it checks out the exact current heads of that repository and `mppx`, runs an open-ended read-only Codex comparison, and requires schema-validated findings with linked code evidence. Shared vectors and conformance-adapter capabilities remain deterministic supporting signals. Agricola clusters matching `semantic:`, `vector:`, and `capability:` fingerprints, maintains one issue per finding, and updates a roll-up index. The audit itself is read-only outside `mpp-tools`; downstream publication requires a maintainer's explicit command on a finding issue.

One concurrency group serializes all triggers. Runs execute trusted code from the current default branch. Ledger state is restored from `agricola/state`; the changelog-style updater creates or updates at most one state pull request. Merge it to checkpoint state on the default branch. Do not close or edit it manually. Code from the state branch is never executed.

## Required GitHub App

Create one GitHub App and install it for these repositories:

- `wevm/mppx`;
- `tempoxyz/mpp-tools`;
- every `automation: pr` target currently listed in [`sdks.yaml`](../sdks.yaml): `mpp-rs` and `pympp`.

Grant repository permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Contents | Read and write | Read canonical commits; create downstream branches. |
| Issues | Read | Read canonical label timeline events. |
| Pull requests | Read and write | Read canonical metadata; create downstream draft pull requests. |

The workflow requests only read access when minting the `wevm/mppx` token and only contents/pull-request write access when minting the `tempoxyz` token. Adding a new target requires updating both `sdks.yaml` and the downstream repository list in the workflow.

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
- create and update tracking issues and replies;
- maintain the state pull request.

In repository Actions settings, enable workflow write access and allow Actions to create pull requests. No direct default-branch push or branch-rule exception is required.

## Execution boundary

Propagation is split into credential boundaries:

1. `run` reads canonical state with a read-only App token and persists the cursor/tracking plan with `GITHUB_TOKEN`.
2. `generate` checks out the request's pinned downstream base commit without persisted credentials. The OpenAI API key is passed only to the official generation action, whose proxy keeps it out of the generated process environment.
3. `verify` checks out the same base commit, receives only the generated binary patch, and runs reviewed manifest commands in a separate, secretless job. An explicit no-op skips verification.
4. `publish` checks out the same base commit only after verification, mints a short-lived target token, applies the verified patch, and creates or updates the stable draft pull request. No-op targets never request write credentials.
5. `record` persists every successful pull-request reference or explicit skip, including successful matrix entries when another target fails, updates the state pull request, and then posts replies.

Downstream code never runs in a job containing downstream write credentials. Generated changes remain drafts and are never auto-merged.

## Deployment checklist

1. Review [`sdks.yaml`](../sdks.yaml), especially maintainers, repositories, automation modes, and verification commands.
2. Create canonical labels `agricola:all`, `agricola:none`, and `agricola:<target>` for each desired target. Agricola validates labels but does not create them.
3. Create and install the GitHub App with the repositories and permissions above.
4. Add `AGRICOLA_APP_ID`, `AGRICOLA_APP_PRIVATE_KEY`, and `OPENAI_API_KEY` to `mpp-tools`.
5. Enable workflow write access and pull-request creation.
6. Run the workflow manually and confirm the poll succeeds and the state pull request contains `ledger/cursor.json`.
7. On a tracking issue, run `@agricola propagate <target>` and confirm generation, verification, a downstream draft pull request, and a recorded ledger decision.
8. Run the SDK audit manually and confirm the `[Agricola] SDK drift audit` index links one issue per finding and records every exact audited commit.
9. On an affected PR-enabled finding, run `@agricola propagate <target>` and confirm the finding links the resulting draft remediation pull request.

The initial cursor starts fifteen minutes in the past. Because each poll replays a one-hour overlap, the first API read covers approximately the previous 75 minutes. To backfill a different window, update the state pull request with a reviewed `ledger/cursor.json` containing a timezone-aware ISO 8601 `merged_at`; polling begins one hour before it.

Ledger-only state pull requests skip repository CI and SDK conformance workflow triggers. Agricola implementation changes run its dedicated tests; mixed or conformance-affecting changes retain normal conformance scope.

## Labels, commands, and idempotency

The maintainer allowlist lives in [`sdks.yaml`](../sdks.yaml). Agricola reconstructs label state from canonical issue events at or before merge. A label is effective only when its final pre-merge application came from a configured maintainer. Unknown labels and conflicts create diagnostic plans. A clean `agricola:none` result is recorded without an issue. Post-merge edits do not change recorded state.

Commands use deferred issue updates and replies so acknowledgements follow their corresponding state change. Canonical-change tables reflect the durable decision ledger. Audit-finding tables retain linked remediation pull requests across later audit runs. Skip decisions use the comment ID and line number. Canonical propagation branches use `agricola/<canonical-repo>-<pr-number>`; audit remediation branches use `agricola/<finding-id>`. Replaying a poll, comment, job, or state overlap therefore reuses existing work instead of opening duplicate pull requests.

On an audit-finding issue, `@agricola propagate <target>` generates, verifies, and opens or updates a draft fix for a named affected PR-enabled SDK. `@agricola propagate all` selects all PR-enabled SDKs affected by that finding. `@agricola status` reports the linked remediation pull requests. The issue embeds the exact audited canonical and target commits used by the request; `plan` and `skip` are not accepted on finding issues.

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
- The state pull request is not updated: confirm workflow write access and permission to create pull requests.
- An audit target is incomplete: inspect its SDK-tagged audit job. Missing snapshots are reported in the roll-up and make the workflow fail after the issue is updated.
- A semantic audit is incomplete: confirm `OPENAI_API_KEY` is configured and inspect the SDK-tagged `Compare SDK implementation to canonical mppx` step. The deterministic snapshot is still reported, but the audit remains unhealthy until the open-ended comparison succeeds.
- A resolved finding remains open: confirm the latest audit was healthy. Incomplete audits deliberately preserve issue state; a healthy run closes findings that no longer appear.
