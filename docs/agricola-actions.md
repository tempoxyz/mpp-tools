# Agricola Actions setup

Agricola runs from [`.github/workflows/agricola.yml`](../.github/workflows/agricola.yml) using the repository-scoped `GITHUB_TOKEN`. It needs no separate secret, database, machine user, or GitHub App.

## Triggers

| Trigger | Behavior |
| --- | --- |
| Ten-minute schedule | Poll merged `wevm/mppx` pull requests. GitHub may delay scheduled jobs. |
| `workflow_dispatch` | Run the poller manually. |
| New `issue_comment` containing `@agricola` | Validate and handle a tracking-issue command. The parser still requires `@agricola` as the first token on a line. |

One concurrency group serializes all triggers. Runs check out the repository's current default branch rather than assuming `main`.

## Required permissions

The workflow declares:

- `contents: write` to persist the cursor and decision ledger;
- `issues: write` to create tracking issues, update plans, and deliver command replies;
- `pull-requests: read` to inspect canonical changes and query live status.

The repository's Actions settings must allow `GITHUB_TOKEN` write access. Branch rules must permit the bot-authored ledger commit or provide an approved equivalent update path. If the default branch rejects the push, the workflow fails before delivering a state-changing command acknowledgement.

The canonical repository must be publicly readable by this token. Private or cross-organization repositories require an appropriately scoped GitHub App or token and are outside the secret-free setup.

## Deployment checklist

1. Review [`sdks.yaml`](../sdks.yaml), especially `maintainers`, repository names, automation modes, verification commands, and capabilities.
2. Create canonical labels `agricola:all`, `agricola:none`, and `agricola:<target>` for each desired manifest target. Agricola validates labels but does not create them.
3. Enable GitHub Actions and workflow write permissions in `mpp-tools`.
4. Ensure branch rules allow the `agricola[bot]` ledger commit.
5. Run the workflow manually once and confirm that `ledger/cursor.json` is committed.
6. Run `agricola validate` locally or inspect the workflow's validation step.

The initial cursor starts fifteen minutes in the past. Because every poll also replays a one-hour overlap, the first API read covers approximately the previous 75 minutes. To intentionally backfill a different window, commit a reviewed `ledger/cursor.json` containing a timezone-aware ISO 8601 `merged_at` timestamp; polling begins one hour before that value.

## Labels and authorization

The maintainer allowlist is reviewed in [`sdks.yaml`](../sdks.yaml). Agricola reconstructs label state from issue events at or before the canonical merge timestamp. An Agricola label is effective only when its final pre-merge application came from a configured maintainer.

Unknown labels and conflicts create diagnostic tracking plans. A clean `agricola:none` result is recorded without an issue. Post-merge edits do not change recorded state.

## Command transaction

For `issue_comment` events, the job:

1. validates the manifest and existing ledger;
2. runs `agricola handle-comment --reply-file "$RUNNER_TEMP/agricola-reply.json"`;
3. commits and pushes any ledger change;
4. runs `agricola deliver-reply` only if all prior steps succeeded.

Parse and scope errors also use the deferred reply file, although they normally produce no ledger change. State-changing acknowledgements are never posted before persistence. A rerun of the same event is safe because skip decisions use `issue-comment:<comment-id>:line:<line>` as the idempotency key.

## Polling and deduplication

The poller lists closed canonical pull requests ordered by update time and replays a one-hour overlap around the durable cursor. Pull requests with existing ledger snapshots are skipped before fetching full details. New candidates reconstruct merge-time labels, create an immutable snapshot, and then either suppress a clean `agricola:none`, reuse a directly listed issue containing the stable source marker, or create a tracking issue.

The cursor and new ledger files are committed together. If a tracking issue is created but Git persistence later fails, the next run finds it through the direct issues endpoint rather than waiting for search indexing.

## Safety boundary

All API writes target `mpp-tools`. Agricola does not write branches, commits, issues, or pull requests in SDK repositories, and manifest verification commands are not executed yet.

Mention commands, including `@agricola status`, are scoped to Agricola tracking issues in `mpp-tools`. Downstream branches, draft pull requests, and commands in other repositories would require a dedicated GitHub App installation token and cross-repository event delivery; those capabilities are not enabled.

## Troubleshooting

- `agricola validate` fails before processing: fix the reported duplicate key, unknown field, invalid manifest value, ledger decision, or cursor timestamp.
- No scheduled run appears: use `workflow_dispatch`; GitHub schedules are best-effort.
- A command has no reply: inspect the persistence step. Replies are intentionally skipped after a failed ledger push.
- A command is ignored: confirm the commenter is in `maintainers`, `@agricola` begins a line, the verb is currently supported, and the issue contains an Agricola source marker.
- A label is ignored: confirm its final application occurred before merge and was performed by a configured maintainer.
- The default branch rejects the ledger commit: adjust the branch rule or provide an approved write path before retrying the run.
