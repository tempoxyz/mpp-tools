<br>
<br>

<p align="center">
  <a href="https://mpp.dev">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/tempoxyz/mpp/refs/heads/main/public/lockup-light.svg">
      <img alt="Machine Payments Protocol" src="https://raw.githubusercontent.com/tempoxyz/mpp/refs/heads/main/public/lockup-dark.svg" width="auto" height="120">
    </picture>
  </a>
</p>

<br>
<br>

# mpp-tools

Open-source monorepo for tooling around the ["Payment" HTTP Authentication Scheme](https://paymentauth.org) (MPP).

[![Website](https://img.shields.io/badge/website-mpp.dev-black)](https://mpp.dev)
[![Protocol](https://img.shields.io/badge/protocol-paymentauth.org-blue)](https://paymentauth.org)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[MPP](https://mpp.dev) lets any client -- agents, apps, or humans -- pay for any service in the same HTTP request. It standardizes [HTTP 402](https://mpp.dev/protocol/http-402) with an open [IETF specification](https://paymentauth.org), so servers can charge and clients can pay without API keys, billing accounts, or checkout flows.

`mpp-tools` provides tooling and test suites for managing MPP SDKs and other ecosystem primitives. 

## Documentation

- [Conformance suite](./conformance/README.md)
- [SDK specification](./SPEC.md)
- [Protocol overview](https://mpp.dev/protocol/)
- [IETF wire specification](https://paymentauth.org)

## Quick Start

```bash
cd conformance
make all
```

This installs the pinned SDK releases, runs the vector suite, and runs the end-to-end 402 flow suite.

Run individual stages while developing:

```bash
make install      # install pinned SDKs and adapter dependencies
make test         # run vector conformance against all adapters
make flow         # run end-to-end 402 flow conformance
```

## Repository Layout

```text
mpp-tools/
├── agricola/             # Control plane for SDK change review
├── ledger/               # Append-only Agricola decision records
├── conformance/          # Cross-SDK conformance test suite
│   ├── adapters/         # Per-language CLI adapters
│   ├── flows/            # End-to-end 402 flow tests
│   ├── golden/           # TypeScript golden adapter
│   ├── scripts/          # Test runners and helpers
│   └── vectors/          # Hand-authored protocol vectors
├── SPEC.md               # SDK conformance specification
├── sdks.yaml             # Reviewed Agricola SDK manifest
└── README.md
```

## Agricola

[Agricola](./agricola/README.md) is the review control plane for canonical SDK changes. It creates deterministic dry-run tracking plans, reconstructs authorized merge-time labels, and records maintainer decisions without writing to downstream repositories. Its deployment runbook is [docs/agricola-actions.md](./docs/agricola-actions.md).

## Conformance Suite

The conformance suite ensures every SDK produces identical protocol outputs for the same inputs. The pinned `mppx` release is the reference implementation: it produces canonical flow results and resolves behavior that the protocol specification leaves ambiguous. Checked-in JSON vectors remain the normative fixtures for their explicit inputs and expected outputs. Each SDK is exercised through a thin CLI adapter with a shared request/response contract.

```text
vectors/*.json -> vector_runner.py -> adapter -> pass/fail
flows/*.json   -> flow_runner.py   -> adapter -> pass/fail
```

The suite covers:

- `WWW-Authenticate: Payment ...` challenge parsing and formatting
- `Authorization: Payment ...` credential parsing and formatting
- `Payment-Receipt: ...` receipt parsing and formatting
- base64url encoding and decoding
- deterministic challenge ID generation
- client-to-server HTTP 402 payment flows

See [conformance/README.md](./conformance/README.md) for adapter commands, vector schemas, flow tests, prerequisites, and targeted test commands.

## Validated SDKs

The harness validates the SDKs declared by the adapter manifests in `conformance/adapters/`. Each SDK follows the shared [SDK specification](./SPEC.md). The harness installs pinned package releases from each package manager, and Dependabot opens SDK bump PRs when newer versions are available.

| SDK | Language | Repository | Package |
|-----|----------|------------|---------|
| `mppx` | TypeScript | [wevm/mppx](https://github.com/wevm/mppx) | [npm](https://www.npmjs.com/package/mppx) |
| `mpp` | Rust | [tempoxyz/mpp-rs](https://github.com/tempoxyz/mpp-rs) | [crates.io](https://crates.io/crates/mpp) |
| `pympp` | Python | [tempoxyz/pympp](https://github.com/tempoxyz/pympp) | [PyPI](https://pypi.org/project/pympp/) |
| `mpp-go` | Go | [tempoxyz/mpp-go](https://github.com/tempoxyz/mpp-go) | [Go module](https://pkg.go.dev/github.com/tempoxyz/mpp-go) |
| `mpp-rb` | Ruby | [stripe/mpp-rb](https://github.com/stripe/mpp-rb) | [RubyGems](https://rubygems.org/gems/mpp-rb) |
| `mpp-java` | Java | [stripe/mpp-java](https://github.com/stripe/mpp-java) | JitPack Maven |

## Updating SDK Pins

```bash
cd conformance
make update-locks
make all
```

SDK versions are pinned in package-manager manifests and lockfiles:

| Language | Package | Pin |
|----------|---------|-----|
| TypeScript | `mppx` | `package.json` / `package-lock.json` |
| Rust | `mpp` | `adapters/rust/Cargo.toml` / `Cargo.lock` |
| Python | `pympp` | `adapters/python/pyproject.toml` / `uv.lock` |
| Go | `github.com/tempoxyz/mpp-go` | `adapters/go/go.mod` / `go.sum` |
| Ruby | `mpp-rb` | `adapters/ruby/Gemfile` / `Gemfile.lock` |
| Java | `com.github.stripe:mpp-java` | `adapters/java/build.gradle` / `gradle.lockfile` |

## Protocol

Built on the ["Payment" HTTP Authentication Scheme](https://paymentauth.org), an open specification proposed to the IETF. See [mpp.dev/protocol](https://mpp.dev/protocol/) for the full protocol overview, or the [IETF specification](https://paymentauth.org) for the wire format.

## Contributing

```bash
git clone https://github.com/tempoxyz/mpp-tools
cd mpp-tools/conformance
make all
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full workflow. When adding protocol behavior, add or update the relevant vectors and verify every adapter before opening a PR.

## Security

See [SECURITY.md](./SECURITY.md) for reporting vulnerabilities.

## License

[MIT](./LICENSE)
