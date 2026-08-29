# Agricola on Auto

Agricola's primary control plane is declared in [`.auto/`](../.auto/). It uses
GitHub issues as its durable approval queue and Auto session bindings for natural
language feedback. No issue command syntax is required.

## Architecture

| Agent | Access | Responsibility |
| --- | --- | --- |
| `agricola-scout` | Read every SDK; write `mpp-tools` issues | Continuously inspect canonical changes, run a weekly drift review, and create one deduplicated proposal per target. |
| `agricola-implementer` | Write `mpp-rs` and `pympp`; never merge | Start only after approval, implement one proposal, open a draft PR, and handle issue, review, and CI feedback. |

The workflow is:

```text
canonical merge or heartbeat
          |
          v
  proposal issue
          |
          | maintainer applies agricola:approved
          v
 implementation session ---> draft PR ---> human merge
          ^                    |
          |____________________|
             comments/reviews/CI
```

Each proposal has one target SDK, exact source commits, evidence, scope, tests,
and a stable hidden deduplication key. Ordinary issue comments and pull-request
reviews steer the bound implementation session.

## Deployment

1. Request Auto access and create a project for `tempoxyz/mpp-tools`.
2. Install the Auto GitHub App on `mpp-tools`, `mpp-rs`, and `pympp`. Grant the
   project access to those repositories.
3. Bind GitHub Sync to `tempoxyz/mpp-tools` on `main`.
4. Ensure the `agricola`, `rust`, and `python` labels exist in `mpp-tools`, then
   create `agricola:approved`. These labels are required routing metadata.
5. Review the PR's **Sync plan**, merge it, and confirm both agents apply.
6. Start `agricola-scout` manually once. Confirm it can read every mount and
   create or reconcile a proposal without changing an SDK.
7. Apply `agricola:approved` to a small proposal. Confirm the implementer opens
   a draft PR, binds it, and responds to an ordinary issue comment.

The canonical merge trigger requires the project's GitHub connection to receive
events for `wevm/mppx`. The thirty-minute heartbeat performs the same deduplicated
scan when that installation is unavailable, so continuous discovery does not
depend on cross-organization webhook access.

## Approval and permissions

Applying `agricola:approved` is the only authorization action. GitHub repository
permissions determine who may manage that label. Closing a proposal rejects it.

The scout has no downstream write mount. The implementer has repository-scoped
contents and pull-request write access only for the two `automation: pr` targets
in [`sdks.yaml`](../sdks.yaml). Merge, secrets, and workflow writes are denied.
Every result remains a draft until human review and merge.

When adding a PR-enabled SDK, update all of:

- `sdks.yaml`;
- the scout's read mount;
- the implementer's write mount and repository trigger filters;
- `agricola/tests/test_auto_configuration.py`.

## Rollback

The legacy [propagation](../.github/workflows/agricola.yml) and
[audit](../.github/workflows/agricola-audit.yml) workflows remain manually
dispatchable but have no schedule or issue-comment trigger. To roll back, archive
the Auto agents and restore the reviewed workflow triggers in a pull request.
