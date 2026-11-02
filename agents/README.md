# agents

The shape of AGENTS.md in every MPP repository, and the conventions they all share.

- [`template.md`](./template.md): the shape. A `# <repository>` title and a short paragraph on what the repository is, then `## Commands`, `## Layout` and `## Conventions` in that order, then any sections only that repository needs.
- [`shared.md`](./shared.md): the `## MPP` section every AGENTS.md ends with: shared code, tooling, change and prose conventions, pulled together from the repositories' own AGENTS.md files. Edit it here only.
- [`repos.json`](./repos.json): the repositories that follow the shape, and how each gets the shared section.
- [`sync.py`](./sync.py): `render PATH` writes the shared section into an AGENTS.md between its markers (appending it the first time); `check PATH...` reports shape errors; `audit` checks every repository's AGENTS.md on main.

## Sync

Each repository with `"sync": "workflow"` runs `.github/workflows/sync-agents.yml` hourly and on demand. It calls the reusable [`agents-sync.yml`](../.github/workflows/agents-sync.yml), which checks out `agents/` from mpp-tools main, renders the shared section into that repository's AGENTS.md, and opens or updates an auto-merge pull request on `automation/sync-agents`. The token comes from GitHub STS through the repository's own `.github/sts/agents-sync.sts.yaml` policy, as tempoxyz/plugins does for skills. A merged change to `shared.md` reaches every repository within an hour; run the repository's Sync AGENTS.md workflow to do it at once.

wevm/mppx (`"sync": "manual"`) is outside tempoxyz's STS installation: update its section by hand with `sync.py render`. mpp-tools (`"sync": "local"`) renders its own AGENTS.md in the same pull request that changes `shared.md`; CI fails when they differ.

The [AGENTS.md workflow](../.github/workflows/agents.yml) tests `sync.py`, checks this repository's AGENTS.md, and audits every repository daily, writing the result to the run summary.

## Adding a repository

1. Rewrite its AGENTS.md from `template.md` and run `python3 agents/sync.py render AGENTS.md` there.
2. Copy `.github/workflows/sync-agents.yml` and `.github/sts/agents-sync.sts.yaml` from tempoxyz/mpp-go, putting the repository's name and ID in the policy's subject (`gh api repos/<owner>/<name> --jq .id`).
3. Add it to `repos.json`.
