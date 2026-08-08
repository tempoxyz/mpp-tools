# Agricola Actions setup

Agricola runs from [`.github/workflows/agricola.yml`](../.github/workflows/agricola.yml). The repository-scoped `GITHUB_TOKEN` manages control-plane issues and state; a GitHub App supplies short-lived cross-repository tokens; the official OpenAI action generates downstream patches.

## Triggers

| Trigger | Behavior |
| --- | --- |
| Ten-minute schedule | Poll merged `wevm/mppx` pull requests. GitHub may delay scheduled jobs. |
| `workflow_dispatch` | Run the poller manually. |
| New `issue_comment` containing `@agricola` | Handle a tracking-issue command. `@agricola` must be the first token on a line. |

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
| Actions secret | `OPENAI_API_KEY` | API key used by the generation action. |

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

The initial cursor starts fifteen minutes in the past. Because each poll replays a one-hour overlap, the first API read covers approximately the previous 75 minutes. To backfill a different window, update the state pull request with a reviewed `ledger/cursor.json` containing a timezone-aware ISO 8601 `merged_at`; polling begins one hour before it.

Ledger-only state pull requests skip repository CI and SDK conformance workflow triggers. Agricola implementation changes run its dedicated tests; mixed or conformance-affecting changes retain normal conformance scope.

## Labels, commands, and idempotency

The maintainer allowlist lives in [`sdks.yaml`](../sdks.yaml). Agricola reconstructs label state from canonical issue events at or before merge. A label is effective only when its final pre-merge application came from a configured maintainer. Unknown labels and conflicts create diagnostic plans. A clean `agricola:none` result is recorded without an issue. Post-merge edits do not change recorded state.

Commands use deferred issue updates and replies so the tracking table and state-changing acknowledgements appear only after state persistence. The table provides one durable view of every target, decision, and recorded downstream pull request. Skip decisions use the comment ID and line number. Propagation requests use the source SHA and target, and stable branches use `agricola/<canonical-repo>-<pr-number>`. Replaying a poll, comment, job, or state overlap therefore reuses existing work instead of opening duplicate pull requests.

## Manual poll

Run from the command line:

```bash
gh workflow run agricola.yml --repo tempoxyz/mpp-tools --ref main
gh run list --repo tempoxyz/mpp-tools --workflow agricola.yml --limit 1
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
