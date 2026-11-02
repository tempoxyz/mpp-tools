# mpp-tools

Tooling and test suites for MPP SDKs: the cross-SDK conformance suite, Tempo SDK conformance, the SDK change tooling in `agricola/`, and the shared AGENTS.md conventions every MPP repository follows.

## Commands

```bash
cd conformance && make all        # install pinned SDKs, run vectors, then the 402 flow suite
cd conformance && make test       # vector conformance against every adapter
cd conformance && make flow       # end-to-end 402 flow conformance
cd conformance && uv run --locked python -m unittest discover -s scripts -p "test_*.py"  # script tests

uv sync --locked --group dev                                    # Agricola dev environment
uv run --locked ruff format --check agricola                    # format
uv run --locked ruff check agricola                             # lint
uv run --locked ty check agricola                               # type-check
uv run --locked coverage run -m unittest discover -s agricola/tests -v && uv run --locked coverage report
uv run --locked agricola validate                               # validate sdks.yaml and state

(cd agents && python3 -m unittest -v test_sync)                # AGENTS.md tooling and the sync workflow's renderer
python3 agents/sync.py check AGENTS.md                          # this file's shape
```

Prerequisites for the conformance suite are in [conformance/README.md](./conformance/README.md#prerequisites).

## Layout

- `conformance/`: cross-SDK conformance suite. `vectors/*.json` are the normative fixtures, `adapters/` holds one thin CLI adapter per SDK, `flows/` the end-to-end 402 flows.
- `tempo-conformance/`: core Tempo SDK vectors (viem/ox, tempo-primitives, tempo-go, pytempo) run against a local Tempo node.
- `agricola/`: SDK change tooling; its runbook is `docs/agricola-actions.md`.
- `ledger/`: append-only decision records.
- `sdks.yaml`: the SDK manifest (targets, owners, changelog style, verify commands).
- `agents/`: the AGENTS.md template, the shared `## MPP` conventions, and `sync.py`; see `agents/README.md`.
- `SPEC.md`: the SDK conformance specification.

## Conventions

- Keep changes focused and follow existing patterns.
- Protocol behavior changes add or update vectors in `conformance/vectors/`; run the affected adapters, then the full suite.
- The pinned mppx release is the reference: it produces canonical flow results where the specification is ambiguous.
- Changes to `agents/shared.md` render into this file in the same pull request (`python3 agents/sync.py render AGENTS.md`); every other repository picks them up through its Sync AGENTS.md workflow.

<!-- BEGIN MPP SHARED: synced from tempoxyz/mpp-tools agents/shared.md. Edit it there; changes here are overwritten. -->

## MPP

Conventions every MPP repository shares, pulled together from their AGENTS.md files. Synced from [`agents/shared.md`](https://github.com/tempoxyz/mpp-tools/blob/main/agents/shared.md) in tempoxyz/mpp-tools: edit it there. The sections above it are this repository's own and win where they are more specific.

### This file

- Keep it to `# <repository>` and a short paragraph, then `## Commands`, `## Layout`, `## Conventions`, then only the sections this repository needs, then this section.
- Write rules an agent can act on: commands, paths, constraints. Leave out background, history and anything the code, specs or README already say.
- When you learn a convention the hard way, add it to the section it belongs to in the same pull request.

### Code

- Make the smallest change that does the job and follow the surrounding code. Start concrete; extract an abstraction only once several real uses share it.
- Keep the public API narrow. Add rather than remove or rename, and mark what you replace as deprecated before deleting it.
- Document every exported item (JSDoc, rustdoc, docstrings, Go doc comments). Comments say why, not what.
- Match mppx, the reference SDK: its names (Challenge, Credential, Receipt, Method), defaults, errors and behavior, unless the language forces otherwise; say why when it does.
- The IETF drafts in tempoxyz/mpp-specs (paymentauth.org) are the source of truth for the protocol, and mppx settles what they leave open. Link them instead of restating them.
- Add no dependency without a reason the pull request states. Prefer the standard library and what the repository already uses.
- Never log or commit keys, credentials, signatures, authorization headers or raw upstream errors.
- Tempo chain IDs: mainnet `4217`, Moderato testnet `42431`. Never `98865`.

### Tooling

Use each language's standard toolchain, run through the commands in `## Commands`, and keep it clean with warnings as errors:

- TypeScript: pnpm, strict `tsc`, ESM with `.js` relative imports, the repository's configured formatter and linter, Vitest.
- Python: uv, ruff (format and lint), a strict type checker (pyright or ty), pytest or unittest.
- Rust: cargo, `cargo fmt`, `cargo clippy -D warnings`, `cargo test`.
- Go: `gofmt`, `go vet`, golangci-lint, `go test -race`.
- Swift: SwiftPM, `swift test -Xswiftc -warnings-as-errors`.
- Run the checks that cover your change before opening a pull request, and add tests for behavior that matters, next to the existing ones.
- Pin GitHub Actions by full commit SHA. Workflows start with `tempoxyz/gh-actions/actions/secure-runner` and get write tokens from GitHub STS policies in `.github/sts/`, never stored secrets.

### Changes

- Commits and pull request titles use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, optionally scoped.
- Never edit `CHANGELOG.md` by hand. Record a release note the way `## Conventions` says (changeset, `.changelog/` fragment, or none when a workflow writes it), in the past tense.
- Before 1.0, bump `patch` for everything except a breaking public API change, which is `minor`.
- Don't edit README files unless asked.
- Never force-push.

### Docs and prose

- Active voice, present tense, second person. Sentence-case headings. No filler (just, simply, easily) and no Latin abbreviations.
- Capitalize Challenge, Credential and Receipt as MPP concepts. Say stablecoins, not crypto, and USDC.e for bridged USDC on Tempo. Write `mppx` in lowercase.

<!-- END MPP SHARED -->
