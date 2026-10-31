# Tempo SDK conformance

Checks that the core Tempo SDKs turn the same inputs into the same bytes, and
that the local Tempo node accepts what they produce.

| Adapter | SDK under test | Pin |
|---|---|---|
| `typescript` (reference) | viem / `ox/tempo` | `adapters/typescript/package.json` (ox 0.14.59, viem 2.57.13) |
| `rust` | `tempo-primitives`, the node's own types | `adapters/rust/Cargo.toml` (1.11.0, newest on crates.io) |
| `go` | `tempo-go` | `adapters/go/go.mod` (v0.6.1) |
| `python` | `pytempo` | `adapters/python/pyproject.toml` (0.6.2) |

```text
vectors/*.json -> scripts/run.py -> adapters/<sdk> (JSON lines) -> exact compare
                               \-> fresh tempo dev node (docker) -> verdict, hash, sender, fee payer
```

## Run

```bash
cd tempo-conformance
make all                 # build adapters, run vectors, run node cases (needs docker)
make vectors             # vectors only, no node
make all ADAPTERS=go     # one SDK
make generate            # regenerate vectors from the TypeScript reference
```

`TEMPO_RPC_URL` points the runner at an existing **fresh** node instead of
starting `ghcr.io/tempoxyz/tempo:1.17.0`.

## What is covered

- `tx.sign` (32 cases): calls, batches, contract creation, access lists, 2D
  nonces, validity windows, fee tokens, fee payer sponsorship, key
  authorizations (expiry, one-off, periodic and no-spending limits), V2
  keychain access-key signing, and inputs every SDK must refuse to sign.
- `tx.decode` (36 cases): every signed transaction above, plus malformed and
  malleable encodings (truncated, trailing byte, wrong type, high-s, bad v,
  legacy tempo.ts fee payer trailer, v = 0/1).
- `key_authorization.sign` (10 cases): signing hash, signature and RLP.

Every case with `node` is submitted, in order, to one fresh dev node. Each
sender is its own deterministic key, so nonces are fixed from genesis and the
expected hashes are the hashes the node reports. The node agrees with every
expectation, and the Rust adapter (the node's own encoding) matches the
TypeScript reference on every case but one, a known node bug.

Vectors were bootstrapped from tempo-go's `transaction_vectors_test.go` and
golden tempo.ts transaction, ox's `TxEnvelopeTempo` and `KeyAuthorization`
tests, and pytempo's keychain tests; each case names its `source`.

## Known gaps

`known-issues.json` lists every case an SDK currently gets wrong, with the
cause and file:line. Those report as KNOWN; anything else fails CI, and so does
a listed case that starts passing, so fixes get unlisted. Adding an op or case
that an SDK can't do yet means adding it there with a reason.

The adapter ABI and vector format are in [SPEC.md](./SPEC.md).

