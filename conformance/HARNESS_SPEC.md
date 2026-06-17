# Conformance Harness Spec

Target shape for a simpler cross-language harness.

Goal: every SDK exposes one small adapter executable with the same JSON ABI. The
runner owns discovery, vector loading, flow orchestration, comparison, output,
and CI formatting. Language adapters only translate ABI operations into SDK
calls.

The normative contract lives in JSON Schema:

- [`schemas/adapter-manifest.schema.json`](./schemas/adapter-manifest.schema.json)
- [`schemas/adapter-request.schema.json`](./schemas/adapter-request.schema.json)
- [`schemas/adapter-response.schema.json`](./schemas/adapter-response.schema.json)
- [`schemas/operation-registry.schema.json`](./schemas/operation-registry.schema.json)
- [`schemas/protocol.schema.json`](./schemas/protocol.schema.json)

The operation list lives in [`operations.json`](./operations.json). Examples
live in [`examples/`](./examples/).

## Simplification Plan

1. Add one adapter manifest per language.
2. Replace hardcoded adapter registration in `scripts/vector_runner.py` and
   `scripts/flow_runner.py` with manifest discovery.
3. Replace command-per-operation CLIs with one JSON request/response ABI.
4. Move all normalization and semantic comparison into the runner.
5. Make flow tests call a generic SDK HTTP operation instead of per-language
   bespoke flow-case clients.
6. Keep old commands behind a compatibility wrapper until all languages move.

## Directory Contract

Each language lives under `conformance/adapters/<name>/`.

```text
adapters/python/
  adapter.json
  adapter.py
  ...
adapters/go/
  adapter.json
  main.go
  ...
```

`adapter.json` is the only file the runner needs to know. It must validate
against `schemas/adapter-manifest.schema.json`.

```json
{
  "$schema": "../../schemas/adapter-manifest.schema.json",
  "schema": 1,
  "name": "python",
  "language": "python",
  "command": ["uv", "run", "--project", ".", "--python", "3.12", "adapter.py"],
  "build": ["uv", "sync", "--locked", "--python", "3.12"],
  "cwd": ".",
  "capabilities": [
    "challenge.parse",
    "challenge.format",
    "credential.parse",
    "credential.format",
    "receipt.parse",
    "receipt.format",
    "base64url.encode",
    "base64url.decode",
    "challenge.id",
    "stripe.external_id_binding",
    "http.payment_request"
  ]
}
```

Rules:

- `cwd` is relative to the manifest directory.
- `build` is optional.
- `command` must start a process that reads one JSON request from stdin and
  writes one JSON response to stdout.
- stderr is free for logs. stdout must contain only the response JSON.

## Adapter ABI

All adapter requests must validate against
`schemas/adapter-request.schema.json`. All adapter responses must validate
against `schemas/adapter-response.schema.json`.

Request:

```ts
type AdapterRequest = {
  schema: 1
  op: Operation
  input: unknown
  context?: {
    caseName?: string
    vectorName?: string
    timeoutMs?: number
  }
}
```

Response:

```ts
type AdapterResponse =
  | { ok: true; value: unknown }
  | {
      ok: false
      error: {
        type:
          | 'parse_error'
          | 'format_error'
          | 'encoding_error'
          | 'generation_error'
          | 'verification_error'
          | 'http_error'
          | 'unsupported_operation'
          | 'unknown_error'
        message: string
      }
    }
```

Exit codes:

- `0`: valid `AdapterResponse` was written.
- non-zero: adapter crashed or could not produce a valid response.

The runner treats non-zero exits, invalid JSON, missing fields, and timeouts as
adapter failures.

## Operations

`operations.json` is the maintainer-facing operation registry. It names each
operation, points to input/output schemas, lists allowed error types, and tells
the runner which comparison mode to use.

```json
{
  "$schema": "./schemas/operation-registry.schema.json",
  "schema": 1,
  "operations": {
    "challenge.parse": {
      "category": "vector",
      "inputRef": "protocol.schema.json#/$defs/HeaderInput",
      "successRef": "protocol.schema.json#/$defs/Challenge",
      "errorTypes": ["parse_error"],
      "comparison": "semantic",
      "description": "Parse a WWW-Authenticate Payment challenge header into a canonical challenge object."
    }
  }
}
```

Comparison modes:

| Mode | Meaning |
|---|---|
| `exact` | Compare JSON values directly. |
| `semantic` | Normalize protocol values before compare, for example header field order or embedded request encoding. |
| `http` | Compare normalized HTTP status, headers, receipt, body, and problem details. |

Required for vector conformance:

| Operation | Input | Output |
|---|---|---|
| `challenge.parse` | `{ "header": string }` | challenge object |
| `challenge.format` | challenge object | `{ "header": string }` |
| `credential.parse` | `{ "header": string }` | credential object |
| `credential.format` | credential object | `{ "header": string }` |
| `receipt.parse` | `{ "header": string }` | receipt object |
| `receipt.format` | receipt object | `{ "header": string }` |
| `base64url.encode` | `{ "text": string }` | `{ "text": string }` |
| `base64url.decode` | `{ "text": string }` | `{ "text": string }` |
| `challenge.id` | challenge id params | `{ "id": string }` |
| `tempo.fee_payer.cosign` | fee-payer transaction params plus sponsor config | cosigned transaction summary |
| `tempo.receipt.verify` | Tempo credential plus receipt fixture | `{ "status": "success", "reference": string }` |
| `stripe.external_id_binding` | Stripe request, credential payload, and PaymentIntent fixture | verification outcome |

`tempo.receipt.verify` is an optional staged vector capability. Adapters that do
not advertise it are skipped; passing it demonstrates conformance for supplied
receipt fixture verification, not full signed Tempo transaction verification.

Required for flow conformance:

| Operation | Input | Output |
|---|---|---|
| `http.payment_request` | normalized HTTP request plus payment config | normalized HTTP response |

The built-in flow runner owns the end-to-end 402 state machine and calls the vector operations above to parse challenges, format credentials, and parse receipts.

To add a new operation:

1. Add the operation name to `Operation` in `schemas/protocol.schema.json`.
2. Add shared input/output value definitions to `schemas/protocol.schema.json`.
3. Add an entry in `operations.json`.
4. Add an `if`/`then` input rule to `schemas/adapter-request.schema.json`.
5. Add one example request and response under `examples/`.
6. Document the operation in this table.
7. Add vector or flow coverage.

The runner should load `operations.json`, validate it, and use it to drive:

- manifest capability validation
- request validation
- response success-value validation
- allowed error type validation
- comparison mode selection

Adapters should not import `operations.json`; it is a runner contract.

## Example Files

Example adapter manifest:

```json
{
  "$schema": "../schemas/adapter-manifest.schema.json",
  "schema": 1,
  "name": "example",
  "language": "example",
  "command": ["./adapter"],
  "build": ["make", "adapter"],
  "cwd": ".",
  "capabilities": ["challenge.parse"]
}
```

Example request:

```json
{
  "schema": 1,
  "op": "challenge.parse",
  "input": {
    "header": "Payment id=\"ch_abc123\", realm=\"api.example.com\", method=\"tempo\", intent=\"charge\", request=\"e30\""
  }
}
```

Example response:

```json
{
  "ok": true,
  "value": {
    "id": "ch_abc123",
    "realm": "api.example.com",
    "method": "tempo",
    "intent": "charge",
    "request": {}
  }
}
```

### Flow Operation

The runner starts the compliance server and passes one request at a time to the
adapter. The adapter uses the SDK's payment-aware HTTP client/transport and
returns the final HTTP response. The runner validates the response.

Input:

```json
{
  "url": "http://127.0.0.1:43999/charge/success",
  "method": "GET",
  "headers": {},
  "body": null,
  "payment": {
    "payload": {
      "type": "transaction",
      "signature": "0xabc123"
    },
    "source": null
  },
  "mode": "payment"
}
```

`mode` values:

| Mode | Meaning |
|---|---|
| `payment` | Use the SDK payment transport normally. |
| `plain` | Send without payment handling. |
| `invalid_payload` | Use payment transport with the provided invalid payload. |
| `mismatched_request` | Pay a challenge after mutating the embedded request. |
| `invalid_challenge_id` | Pay a challenge after mutating the challenge id. |

Output:

```json
{
  "status": 200,
  "headers": {
    "payment-receipt": "eyJzdGF0dXMiOiJzdWNjZXNzIn0"
  },
  "body": ""
}
```

## Canonical Values

Adapters return protocol objects, not SDK-native wrappers.

Challenge:

```json
{
  "id": "ch_abc123",
  "realm": "api.example.com",
  "method": "tempo",
  "intent": "charge",
  "request": {
    "amount": "1000",
    "currency": "USD",
    "recipient": "merchant"
  },
  "expires": "2099-01-01T00:00:00Z",
  "description": "optional",
  "digest": "optional",
  "opaque": "optional"
}
```

Credential:

```json
{
  "challenge": {
    "id": "ch_abc123",
    "realm": "api.example.com",
    "method": "tempo",
    "intent": "charge",
    "request": {}
  },
  "payload": {
    "type": "transaction",
    "signature": "0xabc123"
  },
  "source": null
}
```

Receipt:

```json
{
  "status": "success",
  "method": "tempo",
  "timestamp": "2026-01-29T12:00:00Z",
  "reference": "ref_123",
  "externalId": "optional",
  "extra": {}
}
```

Normalization rules:

- Omit absent optional fields, except `source` may be `null`.
- Use RFC3339 UTC timestamps with `Z`.
- Decode embedded `request` values to JSON objects.
- Header field order is never significant.
- JSON object key order is never significant.
- Base64url output uses RFC4648 URL-safe alphabet without padding.

## Example Calls

Parse a challenge:

```bash
printf '%s\n' '{
  "schema": 1,
  "op": "challenge.parse",
  "input": {
    "header": "Payment id=\"ch_abc123\", realm=\"api.example.com\", method=\"tempo\", intent=\"charge\", request=\"e30\""
  }
}' | ./adapter
```

Response:

```json
{
  "ok": true,
  "value": {
    "id": "ch_abc123",
    "realm": "api.example.com",
    "method": "tempo",
    "intent": "charge",
    "request": {}
  }
}
```

Format a receipt:

```json
{
  "schema": 1,
  "op": "receipt.format",
  "input": {
    "status": "success",
    "method": "tempo",
    "timestamp": "2026-01-29T12:00:00Z",
    "reference": "ref_123"
  }
}
```

Response:

```json
{
  "ok": true,
  "value": {
    "header": "eyJzdGF0dXMiOiJzdWNjZXNzIiwibWV0aG9kIjoidGVtcG8iLCJ0aW1lc3RhbXAiOiIyMDI2LTAxLTI5VDEyOjAwOjAwWiIsInJlZmVyZW5jZSI6InJlZl8xMjMifQ"
  }
}
```

Parse error:

```json
{
  "ok": false,
  "error": {
    "type": "parse_error",
    "message": "missing request parameter"
  }
}
```

## Minimal Adapter Skeletons

### TypeScript

```ts
type Req = { schema: 1; op: string; input: any }
type Res = { ok: true; value: any } | { ok: false; error: { type: string; message: string } }

const success = (value: any): Res => ({ ok: true, value })
const failure = (type: string, message: string): Res => ({ ok: false, error: { type, message } })

async function main() {
  const req: Req = JSON.parse(await new Promise<string>((resolve) => {
    let data = ''
    process.stdin.on('data', (chunk) => (data += chunk))
    process.stdin.on('end', () => resolve(data))
  }))

  let res: Res
  try {
    switch (req.op) {
      case 'challenge.parse':
        res = success(Challenge.deserialize(req.input.header))
        break
      case 'challenge.format':
        res = success({ header: Challenge.serialize(Challenge.from(req.input)) })
        break
      default:
        res = failure('unsupported_operation', req.op)
    }
  } catch (err) {
    res = failure(req.op.includes('parse') ? 'parse_error' : 'unknown_error', String(err))
  }
  process.stdout.write(JSON.stringify(res) + '\n')
}
```

### Python

```python
import json
import sys

def ok(value):
    return {"ok": True, "value": value}

def err(error_type, message):
    return {"ok": False, "error": {"type": error_type, "message": message}}

def handle(req):
    op = req["op"]
    data = req["input"]
    if op == "challenge.parse":
        challenge = parse_www_authenticate(data["header"])
        return ok(challenge_to_dict(challenge))
    if op == "challenge.format":
        challenge = Challenge(**data)
        return ok({"header": format_www_authenticate(challenge, challenge.realm)})
    return err("unsupported_operation", op)

try:
    print(json.dumps(handle(json.load(sys.stdin))))
except Exception as exc:
    print(json.dumps(err("unknown_error", str(exc))))
```

### Go

```go
type Request struct {
	Schema int             `json:"schema"`
	Op     string          `json:"op"`
	Input  json.RawMessage `json:"input"`
}

type Response struct {
	OK    bool        `json:"ok"`
	Value any         `json:"value,omitempty"`
	Error *ErrorValue `json:"error,omitempty"`
}

type ErrorValue struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

func main() {
	var req Request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		writeErr("unknown_error", err.Error())
		return
	}

	switch req.Op {
	case "challenge.parse":
		var in struct{ Header string `json:"header"` }
		json.Unmarshal(req.Input, &in)
		challenge, err := mpp.ParseChallenge(in.Header)
		if err != nil {
			writeErr("parse_error", err.Error())
			return
		}
		writeOK(toMap(challenge))
	default:
		writeErr("unsupported_operation", req.Op)
	}
}
```

### Rust

```rust
#[derive(serde::Deserialize)]
struct Request {
    schema: u8,
    op: String,
    input: serde_json::Value,
}

fn main() {
    let req: Request = serde_json::from_reader(std::io::stdin()).unwrap();
    let res = match req.op.as_str() {
        "challenge.parse" => {
            let header = req.input["header"].as_str().unwrap();
            match mpp::Challenge::parse(header) {
                Ok(challenge) => serde_json::json!({ "ok": true, "value": challenge }),
                Err(err) => serde_json::json!({
                    "ok": false,
                    "error": { "type": "parse_error", "message": err.to_string() }
                }),
            }
        }
        _ => serde_json::json!({
            "ok": false,
            "error": { "type": "unsupported_operation", "message": req.op }
        }),
    };
    println!("{}", res);
}
```

## Runner Shape

Pseudo-code:

```python
for manifest in discover("adapters/*/adapter.json"):
    adapter = Adapter(manifest)
    adapter.build()
    assert "challenge.parse" in adapter.capabilities

for vector in vectors:
    for scenario in vector.scenarios:
        for op, input_value, expected in expand_scenario(vector, scenario):
            actual = adapter.call(op, input_value)
            compare(normalize(expected), normalize(actual))
```

Flow pseudo-code:

```python
server = ComplianceServer(flows)
for flow_case in flows:
    request = build_http_request(server.url, flow_case)
    response = adapter.call("http.payment_request", request)
    compare_response(flow_case, response)
```

The runner should never know language-specific commands. Adapters should never
know vector files, expected values, or comparison rules.
