# mpp-tools

Tooling and test suites for MPP SDKs: the cross-SDK conformance suite, Tempo SDK conformance, Agricola (which propagates canonical mppx changes to the other SDKs), and the canonical AGENTS.md shape every MPP repository follows.

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
uv run --locked agricola validate                               # validate sdks.yaml and Agricola state

python3 -m unittest -v agents/test_sync.py                      # AGENTS.md tooling (run from agents/)
python3 agents/sync.py check AGENTS.md                          # this file's shape
```

Prerequisites for the conformance suite are in [conformance/README.md](./conformance/README.md#prerequisites).

## Layout

- `conformance/`: cross-SDK conformance suite. `vectors/*.json` are the normative fixtures, `adapters/` holds one thin CLI adapter per SDK, `flows/` the end-to-end 402 flows.
- `tempo-conformance/`: core Tempo SDK vectors (viem/ox, tempo-primitives, tempo-go, pytempo) run against a local Tempo node.
- `agricola/`: the propagation and drift-audit control plane for canonical SDK changes; its runbook is `docs/agricola-actions.md`.
- `ledger/`: append-only Agricola decision records.
- `sdks.yaml`: the reviewed Agricola SDK manifest (targets, owners, changelog style, verify commands).
- `agents/`: the canonical AGENTS.md template, the shared `## MPP` section, and `sync.py`; see `agents/README.md`.
- `SPEC.md`: the SDK conformance specification.

## Conventions

- Keep changes focused and follow existing patterns.
- Protocol behavior changes add or update vectors in `conformance/vectors/`; run the affected adapters, then the full suite.
- The pinned mppx release is the reference: it produces canonical flow results where the specification is ambiguous.
- Changes to `agents/shared.md` render into this file in the same pull request (`python3 agents/sync.py render AGENTS.md`); every other repository picks them up through its Sync AGENTS.md workflow.
- Workflows pin actions by full SHA and start with `tempoxyz/gh-actions/actions/secure-runner`. Cross-repository writes use GitHub STS with a policy in the target repository's `.github/sts/`, never a stored token.
- Do not commit secrets, credentials, local paths, or generated build artifacts.

<!-- BEGIN MPP SHARED: synced from tempoxyz/mpp-tools agents/shared.md. Edit it there; changes here are overwritten. -->

## MPP

This section is the same in every MPP repository. It is synced from [`agents/shared.md`](https://github.com/tempoxyz/mpp-tools/blob/main/agents/shared.md) in tempoxyz/mpp-tools: edit it there, not here. Everything above it belongs to this repository.

### Sources of truth

- **Protocol**: the IETF drafts in [tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs), published at [paymentauth.org](https://paymentauth.org). The core scheme is `specs/core/draft-httpauth-payment-01.md`; intents live under `specs/intents/`, payment methods under `specs/methods/<method>/`, and extensions and transports (discovery, MCP) under `specs/extensions/`.
- **Behavior the specs leave open**: [wevm/mppx](https://github.com/wevm/mppx), the reference SDK. Other SDKs match its behavior, names and error types.
- **Cross-SDK fixtures**: the conformance vectors in [tempoxyz/mpp-tools](https://github.com/tempoxyz/mpp-tools) (`conformance/vectors/`), which every SDK runs through a CLI adapter.
- **Docs**: [mpp.dev](https://mpp.dev), built from [tempoxyz/mpp](https://github.com/tempoxyz/mpp).

### Wire format

- **Challenge**: `WWW-Authenticate: Payment id="...", realm="...", method="...", intent="...", request="<base64url>"`, with optional `expires`, `digest`, `opaque` and `header`.
- **Credential**: `Authorization: Payment <base64url>` decoding to `{ challenge, payload, source? }`.
- **Receipt**: `Payment-Receipt: <base64url>` decoding to `{ status, method, timestamp, reference }`.
- Every JSON payload is base64url without padding (RFC 4648 §5). `request` and `opaque` are base64url-encoded JCS (RFC 8785) JSON.
- In a Credential's `challenge`, `opaque` stays the encoded string, not an expanded object. Clients return `id` and `opaque` unchanged.

### Challenge ID binding

The Challenge `id` is `base64url(HMAC-SHA256(server_secret, input))`, where `input` joins these fields with `|`:

```
realm | method | intent | request | expires | digest | opaque
```

Absent optional fields are empty strings, and `opaque` is always the final slot. A Challenge that advertises a credential header other than `Authorization` binds that header name immediately before `opaque`. Servers recompute the HMAC from the echoed Challenge and reject a mismatch, and also check the realm they expect and `expires`.

### Repositories

| Repository | Role |
| --- | --- |
| [wevm/mppx](https://github.com/wevm/mppx) | TypeScript SDK and reference implementation |
| [tempoxyz/mpp-rs](https://github.com/tempoxyz/mpp-rs) | Rust SDK |
| [tempoxyz/pympp](https://github.com/tempoxyz/pympp) | Python SDK |
| [tempoxyz/mpp-go](https://github.com/tempoxyz/mpp-go) | Go SDK |
| [tempoxyz/mpp-swift](https://github.com/tempoxyz/mpp-swift) | Swift SDK |
| [tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs) | IETF drafts for the scheme, intents and methods |
| [tempoxyz/mpp-tools](https://github.com/tempoxyz/mpp-tools) | Conformance suite, Agricola, and this shared section |
| [tempoxyz/mpp](https://github.com/tempoxyz/mpp) | mpp.dev documentation site |
| [tempoxyz/mpp-proxy](https://github.com/tempoxyz/mpp-proxy) | Pay-per-use API proxy on Cloudflare Workers |

### Changing behavior across repositories

- A wire-format or protocol change starts as a pull request to tempoxyz/mpp-specs.
- SDK behavior lands in mppx first. Agricola (tempoxyz/mpp-tools `agricola/`) propagates merged mppx pull requests labeled `agricola:all` or `agricola:<sdk>` to the other SDKs as pull requests.
- A change to wire output or cross-SDK behavior adds or updates conformance vectors in tempoxyz/mpp-tools.
- Port rather than diverge: keep mppx's names (Challenge, Credential, Receipt, Method), defaults and error types unless the language forces otherwise, and say why when it does.

### Tempo

- Mainnet chain ID `4217`; testnet (Moderato) `42431`. Never use `98865`, which is retired.

### This file

Every MPP repository's AGENTS.md has the same shape: `# <repository>` and a short paragraph on what it is, then `## Commands`, `## Layout` and `## Conventions` in that order, then any sections only this repository needs, then this section last. The repository's `.github/workflows/sync-agents.yml` keeps this section in sync with mpp-tools by opening a pull request when it changes. The template and checker are in [tempoxyz/mpp-tools `agents/`](https://github.com/tempoxyz/mpp-tools/tree/main/agents).

<!-- END MPP SHARED -->
