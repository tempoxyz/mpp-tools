> [!IMPORTANT]
> This pull request is maintained by automation. Merge it to checkpoint state on the default branch; do not close or edit it manually.

## Motivation

Persist Agricola's durable state through the repository's required pull-request path.

## Summary

- tracks the latest processed canonical merge
- records immutable source snapshots and maintainer decisions
- assigns stable IDs to recurring audit findings

## Key design considerations

- workflow code always executes from the protected default branch
- writers replay against the newest ledger and publish with a guarded Git lease
- this pull request is updated in place as state advances
