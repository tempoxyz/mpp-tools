# Tempo SDK conformance

Checks that the core Tempo SDKs turn the same inputs into the same bytes, and
that the local Tempo node accepts what they produce. It follows the structure of
the MPP conformance suite in [`../conformance`](../conformance): an operation
registry, one vector file per operation in the MPP vector format, adapters that
speak JSON lines, SDKs pinned in each adapter's package manifest and lockfile and
bumped by Dependabot, and per-scenario `sdkVersions` and `adapters` to skip known
SDK gaps.

| Adapter | SDK under test (version used by `sdkVersions`) | Pin |
|---|---|---|
| `typescript` (reference) | `ox` (`ox/tempo`), with viem | `adapters/typescript/package.json` / `package-lock.json` |
| `rust` | `tempo-primitives`, the node's own types | `adapters/rust/Cargo.toml` / `Cargo.lock` |
| `go` | `github.com/tempoxyz/tempo-go` | `adapters/go/go.mod` / `go.sum` |
| `python` | `pytempo` | `adapters/python/pyproject.toml` / `uv.lock` |

```text
operations.json + schemas/   the operation registry and JSON schemas (vectors, manifests, inputs, outputs)
vectors/*.json               one file per operation, MPP vector format
adapters/<sdk>/adapter.json  manifest: sdk, build, command, capabilities, capabilityGaps
scripts/run.py               runner: validate -> each adapter (JSON lines) -> exact compare
                                     \-> fresh tempo dev node per adapter (docker) -> verdict, hash, sender, fee payer
scripts/sdk_version.py       the pinned SDK version of each adapter, read from its lockfile
```

## Run

```bash
cd tempo-conformance
make all                 # build adapters, run vectors, run node scenarios (needs docker and uv)
make vectors             # vectors only, no node
make all ADAPTERS=go     # one SDK
make skips               # every skipped scenario per adapter, with its reason
make sdk-versions        # pinned SDK versions, as sdkVersions sees them
make validate            # schemas only
make generate            # regenerate vectors from the TypeScript reference
```

The runner uses the MPP runner's Python environment (`../conformance/uv.lock`)
and its SemVer matcher (`../conformance/scripts/version_constraints.py`).
`TEMPO_RPC_URL` points it at an existing **fresh** node instead of starting
`ghcr.io/tempoxyz/tempo:1.17.0`.

## Pins and skips

SDK versions are pinned in each adapter's package manifest and lockfile;
Dependabot (`.github/dependabot.yml`) opens bump PRs, and this suite is their
gate. Known SDK gaps are not failures, they are skips recorded on the scenario,
as in the MPP suite:

- `sdkVersions`: `{ "<adapter>": "<npm-style SemVer constraint>" }`. The
  scenario runs on that adapter only when its pinned version satisfies the
  constraint. A gap fixed in a release newer than the pin, or not fixed yet, is
  `">PINNED"` (for example `"go": ">0.6.1"`): the Dependabot bump that moves
  past the pin runs the scenario, and fails until the fix is really there, at
  which point the constraint moves to the new version.
- `adapters`: the scenario runs only on the listed adapters, for a case an SDK
  will deliberately never match.
- `skipReasons`: required, one entry per adapter that `sdkVersions`
  constrains or `adapters` leaves out: what the SDK does, with file:line and the
  issue or PR. The runner rejects a scenario whose reasons don't match its skips.
- `capabilityGaps` in `adapter.json`: the reason for every operation an
  adapter does not advertise (Go and Python have no `signature.recover`).

```json
{
  "name": "decode_reject_high_s",
  "description": "Sender signature with s > n/2 (the malleable twin).",
  "tags": ["malformed", "node", "node-reject"],
  "sdkVersions": { "typescript": ">0.14.59" },
  "skipReasons": { "typescript": "ox TxEnvelopeTempo.deserialize and Secp256k1.recoverAddress accept a high-s (malleable) sender signature; the node rejects it. Not fixed in ox yet: re-check on the next ox bump." },
  "source": "new",
  "input": { "raw": "0x76f8bd…" },
  "expected": { "ok": false, "error": { "type": "decode_error" } },
  "node": { "expect": "reject", "fund": ["0x…"] }
}
```

To check an unreleased SDK, point the adapter at a local checkout (a Go
`replace`, a venv with the checkout) and pass `--sdk-version go=0.7.0` so the
constraints are evaluated for the version it will become; `--probe-skips` also
runs skipped scenarios and lists the ones that already pass.

Node bugs have no MPP equivalent: a scenario's `node.knownIssue` (`reason`,
`problems`) reports KNOWN only when the node's problems are exactly those, and
fails once they change or disappear.

## What is covered

- `tx.sign` (54): calls, batches, contract creation, access lists, 2D and
  expiring nonces, validity windows, fee tokens, fee payer sponsorship, key
  authorizations (expiry, one-off, periodic, mixed and no-spending limits, TIP-1011
  call scopes, TIP-1053 witnesses, TIP-1049 admin keys and account binding),
  keychain V1 and V2 access keys (including a key that signs its own
  authorization), and inputs every SDK must refuse to sign.
- `tx.decode` (68): every signed transaction above, viem's P256/WebAuthn
  transactions, and malformed or non-canonical encodings (truncated, trailing
  byte, wrong type, high-s, bad v, legacy tempo.ts fee payer trailer, v = 0/1,
  CREATE after the first call, trailing 0x80 in a key authorization or token
  limit, admin marker other than 0x01).
- `key_authorization.sign` (23): signing hash, signature and RLP, including call
  scopes, witnesses, admin keys and account binding.
- `signature.recover` (40): P256, WebAuthn and access-key (keychain V1 and V2)
  envelopes from viem, and 35 WebAuthn clientDataJSON edge cases with the
  verdicts tempo-primitives gives.

Every scenario with `node` is submitted, in order, to one fresh dev node per
adapter, with the bytes that adapter produced. Each sender is its own
deterministic key, so nonces are fixed from genesis and the expected hashes are
the hashes the node reports.

`vectors/sources/viem/` holds the P256/WebAuthn source files (with `gen.mjs` to
regenerate them); `generate.ts` reads them. Each scenario names its `source`.

The adapter ABI and vector format are in [SPEC.md](./SPEC.md).
