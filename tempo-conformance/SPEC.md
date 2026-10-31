# Tempo SDK conformance

Cross-SDK conformance for the core Tempo SDKs: the same input vectors must give
byte-identical outputs in every SDK, and every transaction the vectors call
valid must be accepted and executed by a local Tempo node.

| Adapter | SDK | Package |
|---|---|---|
| typescript | viem / ox (`ox/tempo`) | npm `viem` |
| rust | tempo-primitives (the node's own types) | crates.io `tempo-primitives` |
| go | tempo-go | Go module `github.com/tempoxyz/tempo-go` |
| python | pytempo | PyPI `pytempo` |

The node is the arbiter of validity. Expected outputs are generated with the
TypeScript reference and must also match the Rust adapter, which uses the
node's own encoding.

## Adapter ABI

Each adapter lives in `adapters/<name>/` with an `adapter.json` manifest
(`schemas/adapter-manifest.schema.json`, the MPP manifest plus `sdk` and
`capabilityGaps`):

```json
{
  "schema": 1, "name": "go", "language": "go",
  "sdk": { "package": "github.com/tempoxyz/tempo-go", "manifest": "go.mod", "lockfile": "go.mod" },
  "build": ["sh", "build.sh"], "command": ["./adapter"], "cwd": ".",
  "capabilities": ["tx.sign", "tx.decode", "key_authorization.sign"],
  "capabilityGaps": { "signature.recover": "why, with file:line and the issue or PR" }
}
```

`sdk` names the package under test and the lockfile that pins it;
`scripts/sdk_version.py` reads the version `sdkVersions` constraints are
evaluated against from there. Every operation in `operations.json` that an
adapter does not advertise needs a `capabilityGaps` reason. The adapter
translates JSON to and from SDK types only: encoding, hashing and signing come
from the SDK, and an input the SDK has no API for answers `unsupported`.

`command` reads one JSON request per stdin line and writes one JSON response
line per request, in order, until stdin closes. stderr is for logs.

Request: `{"op": "<operation>", "input": {...}}`

Response: `{"ok": true, "value": {...}}` or
`{"ok": false, "error": {"type": "<type>", "message": "..."}}`, where type is
one of `invalid_input`, `decode_error`, `verification_error`, `unsupported`
(the SDK has no API for this input), `unknown_error`.

An adapter must never crash on bad input: catch, and answer with an error.

### Encoding rules

- Integers are decimal strings (`"1337"`), whatever their size.
- Bytes, hashes and addresses are lowercase `0x` hex. Empty bytes are `"0x"`.
- Absent optional fields are `null` in outputs; inputs may omit them or use `null`.
- Outputs contain exactly the keys listed below.

### Shared types

```ts
type Call = { to: Address | null; value: Int; data: Hex }   // to null = contract creation
type AccessListItem = { address: Address; storageKeys: Hash[] }

type Tx = {
  chainId: Int
  nonce: Int
  nonceKey: Int                // "0" = protocol nonce
  gas: Int
  maxFeePerGas: Int
  maxPriorityFeePerGas: Int
  calls: Call[]
  accessList?: AccessListItem[]   // default []
  validBefore?: Int | null
  validAfter?: Int | null
  feeToken?: Address | null
  keyAuthorization?: SignedKeyAuthorizationInput | null
}

type TokenLimit = { token: Address; limit: Int; period?: Int }  // period "0" (default) = one-off limit, >0 = resets every period seconds
type SelectorRule = { selector: Hex /* 4 bytes */; recipients?: Address[] }  // absent or [] = any recipient
type CallScope = { target: Address; selectorRules?: SelectorRule[] }        // absent or [] = any selector on target
type KeyAuthorization = {
  chainId: Int
  keyType: "secp256k1" | "p256" | "webauthn"
  keyId: Address
  expiry?: Int | null            // null = never expires
  limits?: TokenLimit[] | null   // null = unlimited, [] = no spending
  allowedCalls?: CallScope[] | null  // TIP-1011 call scopes: null = unrestricted, [] = no call allowed
  witness?: Hash | null          // TIP-1053: null = none; the all-zero hash is a present witness
  isAdmin?: boolean              // TIP-1049 admin access key (default false)
  account?: Address | null       // TIP-1049 account the authorization is bound to
}
type SignedKeyAuthorizationInput = { authorization: KeyAuthorization; privateKey: Hex }  // root key signs

type Signer =
  | { type: "secp256k1"; privateKey: Hex }
  | { type: "keychain"; accessKeyPrivateKey: Hex; rootAccount: Address; version?: "v1" | "v2" }
    // secp256k1 access key. v2 (default): envelope 0x04 || root || inner, inner over keccak256(0x04 || hash || root).
    // v1 (legacy, deprecated at T1C): envelope 0x03 || root || inner, inner over the signing hash itself.
```

### `tx.sign`

Input: `{ tx: Tx, signer: Signer, feePayerPrivateKey?: Hex | null }`

With `feePayerPrivateKey`, the transaction is sponsored: the sender signs the
fee-payer form (fee token left out of the sender payload, fee payer marker set),
then the fee payer co-signs.

Output:

```ts
{
  senderSigningHash: Hash      // the transaction's sign payload (see keychain note below)
  feePayerSigningHash: Hash | null
  raw: Hex                     // 0x76-prefixed signed transaction, as sent to eth_sendRawTransaction
  hash: Hash                   // keccak256(raw); secp256k1 v bytes are 27/28, the only form whose hash the node reports
  sender: Address              // the account the transaction is from (rootAccount for keychain)
}
```

For `keychain`, `senderSigningHash` is the transaction's sign payload
(`TxEnvelopeTempo.getSignPayload`, the same as a plain secp256k1 sender). The
access key signs whatever digest the V2 keychain scheme derives from it.

### `tx.decode`

Input: `{ raw: Hex }`

Output:

```ts
{
  tx: {
    chainId, nonce, nonceKey, gas, maxFeePerGas, maxPriorityFeePerGas: Int
    calls: Call[]; accessList: AccessListItem[]
    validBefore: Int | null; validAfter: Int | null; feeToken: Address | null
    keyAuthorization: {
      chainId: Int; keyType: string; keyId: Address; expiry: Int | null; limits: TokenLimit[] | null
      allowedCalls: CallScope[] | null   // every scope with selectorRules, every rule with recipients, as arrays
      witness: Hash | null; isAdmin: boolean; account: Address | null
    } | null
  }
  signatureType: "secp256k1" | "p256" | "webauthn" | "keychain"
  sender: Address               // recovered (rootAccount for keychain)
  feePayer: Address | null      // recovered fee payer, null when not sponsored
  hash: Hash
}
```

`TokenLimit` in output always carries `period` (`"0"` for a one-off limit).
Malformed or non-canonical input must answer `decode_error`.

### `key_authorization.sign`

Input: `{ authorization: KeyAuthorization, privateKey: Hex }`

Output: `{ signingHash: Hash, signature: Hex, serialized: Hex }` where
`signature` is the root key's Tempo signature envelope as it appears inside the
signed authorization (secp256k1: 65 bytes `r || s || v`, v = 27/28, the form the
node uses) and `serialized` is the RLP of the signed authorization, as it
appears in a transaction's key authorization field.

### `signature.recover`

Input: `{ hash: Hash, signature: Hex }`, where `signature` is a serialized Tempo
signature envelope: secp256k1 (65 bytes), P256 (`0x01`), WebAuthn (`0x02`), or a
keychain envelope (`0x03` V1, `0x04` V2) wrapping one of those.

Output: `{ signer: Address }`: the account the signature signs `hash` for, as the
node's `TempoSignature::recover_signer` decides it. For keychain envelopes that is
the root (`userAddress`); the inner signature must verify over `hash` (V1) or
`keccak256(0x04 || hash || userAddress)` (V2). Any envelope that does not yield a
valid signer (malformed, wrong key, a WebAuthn clientDataJSON the node rejects)
answers `verification_error`.

## Vectors

`operations.json` registers each operation with its input and output schema
(`schemas/protocol.schema.json`) and error types. `vectors/*.json` use the MPP
vector format (`schemas/vector.schema.json`), one operation per file:

```ts
type VectorFile = {
  version: string                // SemVer of the file format and content
  description: string
  spec_ref: string
  generatedBy?: string
  commands: { operation: Operation }
  scenarios: Scenario[]
}
type Scenario = {
  name: string                   // unique across all files
  description: string
  tags: string[]
  adapters?: string[]            // run only on these adapters
  sdkVersions?: { [adapter: string]: string }  // run on that adapter only when its pinned SDK satisfies the constraint
  skipReasons?: { [adapter: string]: string }  // required: exactly one per adapter that sdkVersions constrains or adapters leaves out
  source: string                 // where the scenario was bootstrapped from
  input: object                  // validated against the operation's input schema
  expected: { ok: true; value: object }        // exact output, validated against the operation's output schema
          | { ok: false; error: { type: ErrorType; messageContains?: string } }
  node?: {
    expect: "success" | "revert" | "reject"  // submit the adapter's raw (tx.sign) or input.raw (tx.decode) to the local node
    fund?: Address[]             // faucet-fund these first
    feePayer?: Address           // who the receipt must name as fee payer
    requires?: string[]          // node scenarios that must pass first (earlier in file order)
    knownIssue?: { reason: string; problems: string[] }  // a known node bug: KNOWN only when the problems match exactly
  }
}
```

Constraints are npm-style SemVer (`=`, `<`, `<=`, `>`, `>=`, whitespace-separated
comparators combined with AND), evaluated by the MPP suite's
`version_constraints.py`. Unknown adapter names, invalid constraints and missing
or extra `skipReasons` are runner errors, not skips.

Scenarios with `node` run in file order on one fresh dev node per adapter. The
runner funds each sender or fee payer with the faucet before submitting. Each
scenario uses its own sender key, so nonces are fixed from genesis. A `tx.sign`
scenario an adapter skipped or could not sign is skipped on its node too.
