# Conformance Test Suite

Cross-SDK protocol compatibility testing for [MPP](https://datatracker.ietf.org/doc/draft-ietf-httpauth-payment/) implementations. Ensures the TypeScript golden fixtures plus Rust, Python, Go, Ruby, and Java SDKs produce **identical outputs** for the same inputs.

The harness installs pinned SDK releases from each language package manager. Dependabot opens SDK bump PRs when newer releases are available, and conformance is the compatibility gate for those bumps.

## Quick Start

```bash
make all          # install pinned SDKs, run vectors + flows
```

Or step by step:

```bash
make install      # install the pinned TS/Rust/Python/Go/Ruby/Java releases
make test         # run SDK adapters against all vectors
make flow         # run the end-to-end flow suite against golden results
make server-verify # run SDK server verification ABI tests
```

## How It Works

Test vectors are hand-authored JSON files in `vectors/`. No SDK is privileged — the vectors are the single source of truth.

Each SDK has a thin **adapter** CLI that wraps its library and exposes a uniform interface. A Python test runner invokes every adapter against every vector and compares outputs.

```
vectors/*.json ──► vector_runner.py ──► adapter (Rust/Python/Go/Ruby/Java) ──► pass/fail
```

The TypeScript adapter remains the golden implementation for fixture maintenance and can be run explicitly with `make test-typescript`. Default vector runs skip it because the vector JSON files are the checked golden source of truth.

See [`HARNESS_SPEC.md`](./HARNESS_SPEC.md) for the schema-backed adapter ABI, manifest format, operation registry, migration plan, and language skeletons.

## Test Vectors

Vectors live in `vectors/` and cover the core protocol surface:

| Vector File | What It Tests |
|-------------|---------------|
| `www-authenticate.json` | Parsing and formatting `WWW-Authenticate: Payment ...` challenge headers. Covers required fields (`id`, `realm`, `method`, `intent`, `request`), optional fields (`expires`, `description`, `digest`), and error cases. |
| `authorization.json` | Parsing and formatting `Authorization: Payment ...` credential headers. The credential is base64url-encoded JSON containing `challenge`, `payload`, and optional `source`. |
| `receipt.json` | Parsing and formatting `Payment-Receipt: ...` headers. Base64url-encoded JSON with `status`, `method`, `timestamp`, `reference`. |
| `base64url.json` | RFC 4648 §5 encoding: no padding, URL-safe alphabet (`-`/`_` instead of `+`/`/`). |
| `challenge-id.json` | Deterministic challenge ID generation via HMAC-SHA256. Input is pipe-delimited (`realm\|method\|intent\|canonicalized_request\|expires\|digest\|opaque`), output is unpadded base64url. |

Each vector file contains **scenarios** — individual test cases with a name, description, tags, and expected inputs/outputs:

```json
{
  "name": "basic_challenge",
  "description": "Minimal challenge with required fields",
  "tags": ["happy-path", "required-fields"],
  "object": { "id": "abc", "realm": "api", "method": "tempo", ... },
  "wire": "Payment id=\"abc\", realm=\"api\", method=\"tempo\", ...",
  "tests": { "parse": true, "format": true, "roundtrip": true }
}
```

Scenarios may optionally include `adapters` to restrict an edge case to specific SDK adapters, and `maxDurationMs` or `maxDurationMsByAdapter` to assert bounded execution for parser stress cases. Long stress inputs can use `wire` as `{ "prefix": "...", "repeat": "...", "count": 123, "suffix": "..." }` to keep fixtures reviewable.

### Test Types

| Test | What It Checks |
|------|----------------|
| `parse` | `parse(wire)` produces `object` |
| `format` | `format(object)` produces `wire` (compared semantically) |
| `roundtrip` | `parse(format(object))` equals `object` |
| `parse` (error) | `parse(wire)` fails with a specific `error_type` |

## Flow Tests

End-to-end 402 flow tests live in `flows/`. These spin up a compliance server and exercise the full client→402→credential→retry→receipt cycle.

```bash
make flow
```

The Python flow runner owns the HTTP state machine. It calls each adapter's existing parse/format commands to parse the challenge, format the credential, and parse the receipt. This keeps flow tests focused on protocol compatibility rather than each SDK's HTTP transport implementation.

Flow assertions compare adapter results against `flows/golden-results.json`, generated with the pinned TypeScript `mppx` package. Regenerate it with `make update-flow-golden` when the flow fixtures intentionally change.

## Server Verification Tests

Server verification cases live in `server-verification/`. These call the `server.verify` adapter operation directly so conformance can cover SDK server-side credential verification paths that are not observable through the client flow runner.

```bash
make server-verify
make server-verify-go
```

## Adapters

Each adapter is a CLI binary that reads from stdin and writes JSON to stdout:

| Command | Input (stdin) | Output (stdout) |
|---------|---------------|-----------------|
| `parse-www-authenticate` | Header string | JSON challenge object |
| `parse-authorization` | Header string | JSON credential object |
| `parse-receipt` | Header string | JSON receipt object |
| `format-www-authenticate` | JSON challenge | Header string |
| `format-authorization` | JSON credential | Header string |
| `format-receipt` | JSON receipt | Header string |
| `base64url-encode` | Plain string | Base64url encoded |
| `base64url-decode` | Base64url string | Plain string |
| `generate-challenge-id` | JSON params | Challenge ID string |

All commands return `{"success": true, "result": <value>}` on success or `{"success": false, "error": "...", "error_type": "..."}` on failure.

Schema-backed adapter operations use the JSON ABI from `HARNESS_SPEC.md`: requests are
`{"schema": 1, "op": "<operation>", "input": <value>}` and responses are
`{"ok": true, "value": <value>}` or `{"ok": false, "error": <value>}`.
`server.verify` is exposed only through that JSON ABI and accepts server route
params plus a credential object, returning a normalized verification result.

Adapter locations:

| Language | Path |
|----------|------|
| TypeScript (golden) | `golden/adapter.ts` |
| Rust | `adapters/rust/` |
| Python | `adapters/python/` |
| Go | `adapters/go/` |
| Ruby (`mpp-rb`) | `adapters/ruby/` |
| Java (`mpp-java`) | `adapters/java/` |

## SDK Versions

SDK pins live in package-manager manifests and lockfiles where the ecosystem supports them:

| Language | Package | Pin |
|----------|---------|-----|
| TypeScript | `mppx` | `package.json` / `package-lock.json` |
| Rust | `mpp` | `adapters/rust/Cargo.toml` / `Cargo.lock` |
| Python | `pympp` | `adapters/python/pyproject.toml` / `uv.lock` |
| Go | `github.com/tempoxyz/mpp-go` | `adapters/go/go.mod` / `go.sum` |
| Ruby | `mpp-rb` | `adapters/ruby/Gemfile` / `Gemfile.lock` |
| Java | `com.github.stripe:mpp-java` | `adapters/java/build.gradle` / `gradle.lockfile` |

Dependabot checks all configured package managers daily and opens PRs when updates are available. Every PR runs vector and flow conformance in CI, so dependency bump PRs are gated by the same compatibility suite.

The Java adapter currently pins `mpp-java` to an exact JitPack commit because `mpp-java` does not publish versioned Maven releases yet. Update `adapters/java/build.gradle` manually and run `make update-java` when changing that pin.

## Running Specific Tests

```bash
# Single adapter
make test-typescript
make test-rust
make test-python
make test-go
make test-ruby
make test-java

# Single vector file
python3 scripts/vector_runner.py --vector www-authenticate

# Filter by tag
python3 scripts/vector_runner.py --tag happy-path

# Verbose output
python3 scripts/vector_runner.py --verbose

# JSON output (for CI)
python3 scripts/vector_runner.py --output json

# Flow JSON output
python3 scripts/flow_runner.py --output json
```

JSON output includes a `checks` array. Each check has a stable `id`, `name`,
`description`, `status`, `timestamp`, `specReferences`, `details`, and
`errorMessage`.

## Gating SDK Repositories

SDK pull requests can call the reusable workflow in this repository and run the
same vector and flow conformance suite against the SDK checkout from the PR,
instead of the pinned package release.

Example `.github/workflows/conformance.yml` in `mpp-rs`:

```yaml
name: Conformance

on:
  pull_request:
    types:
      - opened
      - synchronize
      - reopened
      - edited
      - labeled
      - unlabeled
  push:
    branches:
      - main

jobs:
  conformance:
    uses: tempoxyz/mpp-tools/.github/workflows/sdk-conformance.yml@main
    with:
      adapter: rust
```

Use the matching adapter name in each SDK repository:

| SDK Repo | Adapter |
|----------|---------|
| [`tempoxyz/mpp-rs`](https://github.com/tempoxyz/mpp-rs) | `rust` |
| [`stripe/mpp-rb`](https://github.com/stripe/mpp-rb) | `ruby` |
| [`tempoxyz/mpp-go`](https://github.com/tempoxyz/mpp-go) | `go` |
| [`tempoxyz/pympp`](https://github.com/tempoxyz/pympp) | `python` |
| [`wevm/mppx`](https://github.com/wevm/mppx) | `typescript` |

Then make the called `conformance` job a required branch-protection or ruleset
check in the SDK repository.

For protocol-sensitive SDK paths, add the policy gate before the behavior gate.
When those paths change, the policy gate chooses the conformance ref used by
the behavior gate. By default it uses `mpp-tools` `main`, which lets SDK PRs pass
when existing conformance coverage already exercises the behavior. If new
coverage is still pending in `mpp-tools`, reference that conformance PR in the
SDK PR body. Maintainers can apply the `conformance-not-needed` label when a
protocol-sensitive SDK change intentionally does not need conformance coverage.
Include the `edited`, `labeled`, and `unlabeled` pull request event types so
updates to those fields rerun the policy check.

```yaml
jobs:
  conformance-policy:
    uses: tempoxyz/mpp-tools/.github/workflows/sdk-conformance-policy.yml@main
    with:
      protocol-paths: |
        src/**
        Cargo.toml

  conformance:
    needs: conformance-policy
    uses: tempoxyz/mpp-tools/.github/workflows/sdk-conformance.yml@main
    with:
      adapter: rust
      conformance-ref: ${{ needs.conformance-policy.outputs.conformance_ref }}
```

When a PR needs pending conformance coverage that has not landed on
`mpp-tools` `main`, include this in the PR body:

```text
Conformance-PR: tempoxyz/mpp-tools#123
```

The referenced `mpp-tools` PR must be open or merged, and it must touch one of
the configured conformance coverage paths. The SDK behavior gate will run
against `refs/pull/<number>/head` for that conformance PR. By default the
coverage paths are
`conformance/vectors/**`, `conformance/flows/**`, `conformance/schemas/**`, and
`conformance/operations.json`.

Set `require-conformance-reference: true` if an SDK repository wants to keep the
stricter policy where every protocol-sensitive PR must reference a conformance
PR or carry the skip label.

To run the same mode locally:

```bash
make install-runner
make use-local-sdk ADAPTER=rust SDK_PATH=../mpp-rs
make test-sdk ADAPTER=rust
make flow-sdk ADAPTER=rust
```

## Adding a New Test Scenario

1. Edit the appropriate vector file in `vectors/`
2. Add a new scenario object to the `scenarios` array
3. Run `make test` to verify all adapters pass
4. Submit a PR

## Prerequisites

- Node.js ≥ 20
- Rust toolchain (for the Rust adapter)
- Python ≥ 3.12 + [uv](https://github.com/astral-sh/uv) (for the Python adapter)
- Go with toolchain auto-download enabled or Go ≥ 1.26 (for the Go adapter)
- Ruby ≥ 3.3 + Bundler (for the `mpp-rb` adapter)
- JDK 17 or newer (for the Java adapter; it builds Java 11 bytecode)
- `python3 -m pip install -r requirements.txt` (for the test runner)
