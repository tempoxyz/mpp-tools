#!/usr/bin/env python3
"""Python conformance adapter using the pympp SDK."""

import sys
import json
import base64
import subprocess
import asyncio
from importlib.metadata import version as _package_version

from mpp import (
    Challenge,
    ChallengeEcho,
    Credential,
    Receipt,
    generate_challenge_id,
    parse_www_authenticate,
    parse_authorization,
    format_www_authenticate,
    format_authorization,
    parse_payment_receipt,
    format_payment_receipt,
)

MINIMUM_SECRET_KEY_BYTES = 32


def _engine_api() -> bool:
    """pympp >= 0.16 replaced Client and intent verification with the Mpp engine."""
    major, minor = (int(part) for part in _package_version("pympp").split(".")[:2])
    return (major, minor) >= (0, 16)


ENGINE_API = _engine_api()


def base64url_encode(data: str) -> str:
    """Encode a string to base64url without padding."""
    encoded = base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def base64url_decode(data: str) -> str:
    """Decode a base64url string (with or without padding).

    Invalid input must be rejected, not repaired: silently dropping
    out-of-alphabet characters would accept wire values every other
    adapter refuses.
    """
    import re

    stripped = data.rstrip("=")
    if re.search(r"[^A-Za-z0-9_-]", stripped):
        raise ValueError("invalid base64url character")
    padding = 4 - (len(stripped) % 4)
    if padding != 4:
        stripped += "=" * padding
    decoded = base64.urlsafe_b64decode(stripped)
    return decoded.decode("utf-8")


def generate_conformance_challenge_id(
    *,
    secret_key: str,
    realm: str,
    method: str,
    intent: str,
    request,
    expires: str | None = None,
    digest: str | None = None,
    header: str | None = None,
    opaque: str | None = None,
) -> str:
    """Compute a challenge ID with pympp's own ``generate_challenge_id``.

    Vectors carry ``opaque`` in its wire form (base64url JCS JSON); pympp binds
    the decoded map and re-canonicalizes it, exactly as its server does when it
    authenticates an echoed challenge.
    """
    # pympp accepts any secret length, while mppx refuses keys shorter than
    # 32 bytes (error_weak_secret_key); this guard is the adapter's, not the SDK's.
    if len(secret_key.encode("utf-8")) < MINIMUM_SECRET_KEY_BYTES:
        raise ValueError(f"secretKey must be at least {MINIMUM_SECRET_KEY_BYTES} bytes")

    opaque_map = None
    if opaque:
        opaque_map = json.loads(base64url_decode(opaque))
        if not isinstance(opaque_map, dict):
            raise ValueError("opaque must encode a JSON object")
    return generate_challenge_id(
        secret_key=secret_key,
        realm=realm,
        method=method,
        intent=intent,
        request=request or {},
        expires=expires,
        digest=digest,
        opaque=opaque_map,
        header=header,
    )


def tempo_proof_typed_data(
    *, account: str, chain_id: int, challenge_id: str, realm: str
):
    return {
        "domain": {
            "name": "MPP",
            "version": "3",
            "chainId": chain_id,
        },
        "types": {
            "Proof": [
                {"name": "account", "type": "address"},
                {"name": "challengeId", "type": "string"},
                {"name": "realm", "type": "string"},
            ],
        },
        "primaryType": "Proof",
        "message": {
            "account": account,
            "challengeId": challenge_id,
            "realm": realm,
        },
    }


def challenge_to_dict(challenge: Challenge) -> dict:
    """Convert a Challenge to a JSON-serializable dict."""
    result = {
        "id": challenge.id,
        "realm": challenge.realm,
        "method": challenge.method,
        "intent": challenge.intent,
        "request": challenge.request,
    }
    if challenge.expires is not None:
        result["expires"] = challenge.expires
    if challenge.description is not None:
        result["description"] = challenge.description
    if challenge.digest is not None:
        result["digest"] = challenge.digest
    opaque = getattr(challenge, "opaque", None)
    if isinstance(opaque, dict):
        # pympp decodes opaque; canonical values carry its wire form (JCS base64url).
        opaque = base64url_encode(
            json.dumps(opaque, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        )
    if opaque is not None:
        result["opaque"] = opaque
    header = getattr(challenge, "header", None)
    if header is not None:
        result["header"] = header
    return result


def credential_to_dict(credential: Credential) -> dict:
    """Convert a Credential to a JSON-serializable dict."""
    challenge = credential.challenge
    # Decode the base64url request string back to an object. Use the
    # hardened base64url_decode() here too: raw urlsafe_b64decode()
    # silently drops out-of-alphabet characters instead of rejecting
    # them, which would let a malformed embedded `request` field pass
    # through this adapter while every other adapter rejects it.
    request_decoded = json.loads(base64url_decode(challenge.request))

    challenge_dict = {
        "id": challenge.id,
        "realm": challenge.realm,
        "method": challenge.method,
        "intent": challenge.intent,
        "request": request_decoded,
    }
    if challenge.expires is not None:
        challenge_dict["expires"] = challenge.expires
    if challenge.digest is not None:
        challenge_dict["digest"] = challenge.digest
    opaque = getattr(challenge, "opaque", None)
    if opaque is not None:
        challenge_dict["opaque"] = opaque

    result = {
        "challenge": challenge_dict,
        "payload": credential.payload,
    }
    if credential.source is not None:
        result["source"] = credential.source
    return result


# Top-level receipt keys with named Receipt fields; any other key is a
# method-defined extension the SDK keeps in Receipt.extensions.
RECEIPT_BASE_FIELDS = {
    "status",
    "timestamp",
    "reference",
    "method",
    "externalId",
    "subscriptionId",
    "extra",
}


def receipt_to_dict(receipt: Receipt) -> dict:
    """Convert a Receipt to a JSON-serializable dict."""
    result = {
        "status": receipt.status,
        "timestamp": receipt.timestamp.isoformat().replace("+00:00", "Z"),
        "reference": receipt.reference,
    }
    if receipt.method:
        result["method"] = receipt.method
    if receipt.external_id is not None:
        result["externalId"] = receipt.external_id
    if receipt.subscription_id is not None:
        result["subscriptionId"] = receipt.subscription_id
    funding_currency = getattr(receipt, "funding_currency", None)
    if funding_currency is not None:
        result["fundingCurrency"] = funding_currency
    if receipt.extra is not None:
        result["extra"] = receipt.extra
    for key, value in (receipt.extensions or {}).items():
        result.setdefault(key, value)
    return result


def success(result):
    return {"success": True, "result": result}


def error(message: str, error_type: str = "unknown_error"):
    return {"success": False, "error": message, "error_type": error_type}


OP_TO_COMMAND = {
    "challenge.parse": "parse-www-authenticate",
    "challenge.format": "format-www-authenticate",
    "credential.parse": "parse-authorization",
    "credential.format": "format-authorization",
    "receipt.parse": "parse-receipt",
    "receipt.format": "format-receipt",
    "base64url.encode": "base64url-encode",
    "base64url.decode": "base64url-decode",
    "challenge.id": "generate-challenge-id",
    "tempo.receipt.verify": "verify-tempo-receipt",
    "tempo.proof.typed_data": "tempo-proof-typed-data",
}


class FakeRpcResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeRpcClient:
    """Serves eth_chainId and, for its own transactionHash only, the receipt
    fixture; eth_getTransactionByHash returns None, as the fixture has no
    transaction body. Any other call is recorded in unexpected_call so a
    broken setup is not mistaken for a rejected credential."""

    def __init__(self, receipt, chain_id=4217):
        self._receipt = receipt
        self._chain_id = chain_id
        self.unexpected_call = None

    async def post(self, *args, **kwargs):
        call = kwargs.get("json")
        if not isinstance(call, dict):
            self.unexpected_call = self.unexpected_call or f"undecodable JSON-RPC request: {call!r}"
            return FakeRpcResponse(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None}
            )
        call_id = call.get("id", 1)
        method = call.get("method")
        if method == "eth_chainId":
            return FakeRpcResponse({"jsonrpc": "2.0", "result": hex(self._chain_id), "id": call_id})
        if method == "eth_getTransactionReceipt":
            params = call.get("params") or []
            requested = params[0] if isinstance(params, list) and params else None
            fixture = str(self._receipt.get("transactionHash", ""))
            if isinstance(requested, str) and requested.lower() == fixture.lower():
                return FakeRpcResponse({"jsonrpc": "2.0", "result": self._receipt, "id": call_id})
            return FakeRpcResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "receipt not found for transaction hash"},
                    "id": call_id,
                }
            )
        if method == "eth_getTransactionByHash":
            return FakeRpcResponse({"jsonrpc": "2.0", "result": None, "id": call_id})
        self.unexpected_call = self.unexpected_call or f"unexpected JSON-RPC method {method}"
        return FakeRpcResponse(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"unexpected method {method}"},
                "id": call_id,
            }
        )


def adapter_success(value):
    return {"ok": True, "value": value}


def adapter_error(message: str, error_type: str = "unknown_error"):
    return {"ok": False, "error": {"type": error_type, "message": message}}


class ConformancePaymentMethod:
    name = "tempo"

    def __init__(self, payload, source: str | None):
        self.payload = payload
        self.source = source

    async def create_credential(self, challenge: Challenge) -> Credential:
        return Credential(
            challenge=challenge.to_echo(),
            payload=self.payload,
            source=self.source,
        )


def _engine_client_method(method: "ConformancePaymentMethod"):
    """Wrap the fixed-payload method as a pympp >= 0.16 ClientMethod."""
    from pydantic import BaseModel, ConfigDict

    from mpp.client import ClientMethod, Method

    class _Open(BaseModel):
        model_config = ConfigDict(extra="allow")

    async def create_credential(params) -> str:
        credential = await method.create_credential(params.challenge)
        return format_authorization(credential)

    return ClientMethod(
        method=Method(name="tempo", intent="charge", request_model=_Open, payload_model=_Open),
        create_credential=create_credential,
    )


async def run_http_payment_request(input_value: dict):
    mode = input_value.get("mode")
    if mode == "plain":
        methods = []
    elif mode in {"payment", "invalid_payload"}:
        payment = input_value.get("payment") or {}
        methods = [
            ConformancePaymentMethod(
                payload=payment.get("payload") or {},
                source=payment.get("source"),
            )
        ]
    else:
        raise ValueError(f"Unsupported http.payment_request mode: {mode}")

    request = {
        "headers": input_value.get("headers") or {},
        "content": input_value.get("body"),
        "follow_redirects": True,
    }
    if ENGINE_API:
        from mpp.client import Mpp

        engine = Mpp(methods=[_engine_client_method(method) for method in methods])
        async with engine, engine.client() as client:
            response = await client.request(input_value["method"], input_value["url"], **request)
    else:
        from mpp.client import Client

        async with Client(methods=methods) as client:
            response = await client.request(input_value["method"], input_value["url"], **request)

    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": response.text,
    }


def command_input_for_request(op: str, input_value):
    if op.endswith(".parse"):
        return input_value["header"]
    if op.startswith("base64url."):
        return input_value["text"]
    return json.dumps(input_value)


def response_value_for_operation(op: str, result):
    if op.endswith(".format"):
        return {"header": result}
    if op.startswith("base64url."):
        return {"text": result}
    if op == "challenge.id":
        return {"id": result}
    return result


async def verify_tempo_receipt(input_value: dict):
    from mpp.methods.tempo.intents import ChargeIntent
    if ENGINE_API:
        from mpp.errors import VerificationError
    else:
        from mpp.server.intent import VerificationError

    credential_data = input_value["credential"]
    challenge_data = credential_data["challenge"]
    request_json = json.dumps(challenge_data["request"], separators=(",", ":"))
    request_b64 = base64url_encode(request_json)

    credential = Credential(
        challenge=ChallengeEcho(
            id=challenge_data["id"],
            realm=challenge_data.get("realm", ""),
            method=challenge_data["method"],
            intent=challenge_data["intent"],
            request=request_b64,
            expires=challenge_data.get("expires"),
            digest=challenge_data.get("digest"),
        ),
        payload=credential_data["payload"],
        source=credential_data.get("source"),
    )

    intent = ChargeIntent(rpc_url="https://rpc.test")
    rpc = FakeRpcClient(input_value["receipt"], input_value.get("rpcChainId", 4217))
    intent._http_client = rpc

    try:
        receipt = await intent.verify(credential, challenge_data["request"])
    except Exception as exc:
        # A call the fake RPC could not serve means the setup is broken, so
        # the outcome, even a rejection, says nothing about the SDK.
        if rpc.unexpected_call:
            return error(f"fake RPC received {rpc.unexpected_call}", "unknown_error")
        if isinstance(exc, VerificationError):
            return error(str(exc), "verification_error")
        raise
    if rpc.unexpected_call:
        return error(f"fake RPC received {rpc.unexpected_call}", "unknown_error")

    return success({"status": receipt.status, "reference": receipt.reference})


# Problem types pympp's server reports, by the server.verify result errorType.
SERVER_ERROR_TYPES = {
    "malformed-credential": "malformed_credential",
    "invalid-challenge": "invalid_challenge",
    "payment-expired": "payment_expired",
    "verification-failed": "verification_failed",
}


def server_error_type(error: BaseException | None) -> str:
    from mpp.errors import PaymentError

    if not isinstance(error, PaymentError):
        return "unknown_error"
    return SERVER_ERROR_TYPES.get(error.type.rsplit("/", 1)[-1], "unknown_error")


class ConformanceServerIntent:
    """Settles every credential pympp's server lets through, or rejects it with
    ``verificationError``, so server.verify isolates the SDK's challenge checks."""

    def __init__(self, name: str, method: str, error_message: str | None):
        self.name = name
        self._method = method
        self._error_message = error_message

    async def verify(self, credential: Credential, request: dict) -> Receipt:
        from datetime import UTC, datetime

        from mpp.errors import VerificationFailedError

        if self._error_message:
            raise VerificationFailedError(self._error_message)
        return Receipt(
            status="success",
            timestamp=datetime(2026, 1, 29, 12, 0, 0, tzinfo=UTC),
            reference="conformance-receipt",
            method=self._method,
        )


def credential_from_dict(data: dict) -> Credential:
    """Build a Credential whose echo carries the request as base64url JSON."""
    challenge_data = data["challenge"]
    request_json = json.dumps(challenge_data.get("request", {}), separators=(",", ":"))
    echo_fields = {}
    if challenge_data.get("opaque") is not None:
        echo_fields["opaque"] = challenge_data["opaque"]
    return Credential(
        challenge=ChallengeEcho(
            id=challenge_data["id"],
            realm=challenge_data.get("realm", ""),
            method=challenge_data["method"],
            intent=challenge_data["intent"],
            request=base64url_encode(request_json),
            expires=challenge_data.get("expires"),
            digest=challenge_data.get("digest"),
            **echo_fields,
        ),
        payload=data.get("payload") or {},
        source=data.get("source"),
    )


async def run_server_verify(input_value: dict) -> dict:
    """Verify a credential the way pympp's server does for a paid route.

    ``verify_or_challenge`` is the verifier the Mpp engine's route handlers run for
    each offer (pympp >= 0.16) and the server entry point before it. A rejected
    credential comes back as a fresh challenge, with the reason on the
    ``payment.failed`` event; a failed settlement raises.
    """
    from mpp.events import PAYMENT_FAILED, EventDispatcher
    from mpp.server.verify import verify_or_challenge

    credential = credential_from_dict(input_value["credential"])
    failures: list[BaseException] = []
    events = EventDispatcher()
    events.on(PAYMENT_FAILED, lambda payload: failures.append(payload["error"]))
    intent = ConformanceServerIntent(
        input_value["intent"], input_value["method"], input_value.get("verificationError")
    )
    try:
        result = await verify_or_challenge(
            authorization=format_authorization(credential),
            intent=intent,
            request=input_value["request"],
            realm=input_value["realm"],
            secret_key=input_value["secretKey"],
            method=input_value["method"],
            expires=input_value.get("expires"),
            events=events,
        )
    except Exception as exc:
        return {"ok": False, "errorType": server_error_type(exc)}
    if isinstance(result, Challenge):
        return {
            "ok": False,
            "errorType": server_error_type(failures[-1] if failures else None),
        }
    _, receipt = result
    return {"ok": True, "receipt": receipt_to_dict(receipt)}


def serve_http(input_value: dict) -> None:
    """Serve a payment-protected route set until terminated (http.server).

    Every offer uses a stand-in charge method whose payload is a fixed
    signature, so the pair runner exercises pympp's challenge issuance,
    credential parsing and binding checks without a chain. Like pympp's own
    charge methods, it settles each challenge once (a replay raises
    CredentialReplayedError), declines the signature 0xdecline, and returns a
    fixed receipt. Writes the adapter response, then serves until SIGTERM.
    """
    from datetime import UTC, datetime
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from typing import Literal
    from urllib.parse import urlsplit

    from pydantic import BaseModel

    from mpp.errors import CredentialReplayedError, VerificationFailedError
    from mpp.methods import Method
    from mpp.server import Mpp, Request, Response, ServerMethod

    class ChargeRequest(BaseModel):
        amount: str
        currency: str
        recipient: str

    class ChargePayload(BaseModel):
        type: Literal["transaction"]
        signature: str

    settled: set[str] = set()

    def conformance_method(name: str) -> ServerMethod:
        async def verify(context) -> Receipt:
            payload = context.credential.payload
            signature = payload["signature"] if isinstance(payload, dict) else payload.signature
            if signature == "0xdecline":
                raise VerificationFailedError("payment declined")
            if context.credential.challenge.id in settled:
                raise CredentialReplayedError("credential already used")
            settled.add(context.credential.challenge.id)
            return Receipt(
                status="success",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                reference=f"ref-{signature}",
                method=name,
            )

        return ServerMethod(
            method=Method(
                name=name,
                intent="charge",
                request_model=ChargeRequest,
                payload_model=ChargePayload,
            ),
            verify=verify,
        )

    routes = input_value["routes"]
    names = list(dict.fromkeys(offer["method"] for route in routes for offer in route["offers"]))
    server = Mpp(
        methods=[conformance_method(name) for name in names],
        realm=input_value["realm"],
        secret_key=input_value["secretKey"],
        requires_auth=bool(input_value.get("requiresAuth")),
    )

    def route_options(offer: dict) -> dict:
        options = dict(offer["request"])
        for key in ("expires", "meta"):
            if key in offer:
                options[key] = offer[key]
        return options

    handlers = {}
    for route in routes:
        entries = [(f"{offer['method']}/charge", route_options(offer)) for offer in route["offers"]]
        if len(entries) == 1:
            handler = server[entries[0][0]](**entries[0][1])
        else:
            handler = server.compose(*entries)
        handlers[route["path"]] = (handler, route)

    loop = asyncio.new_event_loop()

    async def respond(method: str, url: str, headers, body: bytes | None) -> Response:
        entry = handlers.get(urlsplit(url).path)
        if entry is None:
            return Response(status=404, body="not found")
        handler, route = entry
        request = Request(method, url, headers=headers, body=body)
        result = await handler(request)
        if result.status != 200:
            return result.challenge
        echoed = {name: request.headers.get(name) for name in route.get("echoHeaders", [])}
        return result.with_receipt(Response.json({"ok": True, "path": route["path"], **echoed}))

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}{self.path}"
            try:
                response = loop.run_until_complete(
                    respond(self.command, url, list(self.headers.items()), body)
                )
            except Exception as exc:
                # Surface a crash as a 500 the snapshot shows, not a dropped connection.
                print(f"http.server error: {exc!r}", file=sys.stderr)
                response = Response(status=500, body=str(exc))
            payload = response.body or b""
            self.send_response(response.status)
            for name, value in response.headers.multi_items():
                if name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        def log_message(self, format, *args) -> None:
            return None

    httpd = HTTPServer(("127.0.0.1", int(input_value.get("port") or 0)), Handler)
    host, port = httpd.server_address[:2]
    print(json.dumps(adapter_success({"url": f"http://{host}:{port}"})), flush=True)
    httpd.serve_forever()


def run_legacy_subprocess(command: str, input_data: str = ""):
    result = subprocess.run(
        [sys.executable, __file__, command],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return error(result.stderr.strip() or result.stdout.strip(), "unknown_error")
    return json.loads(result.stdout)


def run_adapter_request(request: dict):
    op = request.get("op")
    input_value = request.get("input")
    if op == "http.payment_request":
        try:
            return adapter_success(asyncio.run(run_http_payment_request(input_value)))
        except Exception as exc:
            return adapter_error(str(exc), "http_error")
    if op == "server.verify":
        try:
            return adapter_success(asyncio.run(run_server_verify(input_value)))
        except Exception as exc:
            return adapter_error(str(exc), "verification_error")

    if op == "http.server":
        return adapter_error("http.server needs the pympp >= 0.16 engine", "unsupported_operation")

    command = OP_TO_COMMAND.get(op)
    if command is None:
        return adapter_error(f"Unknown operation: {op}", "unsupported_operation")
    result = run_legacy_subprocess(command, command_input_for_request(op, input_value))
    if not result.get("success"):
        return adapter_error(result.get("error", ""), result.get("error_type", "unknown_error"))
    return adapter_success(response_value_for_operation(op, result.get("result")))


def main():
    if len(sys.argv) < 2:
        try:
            request = json.loads(sys.stdin.read())
            if request.get("op") == "http.server" and ENGINE_API:
                serve_http(request.get("input"))
                return
            print(json.dumps(run_adapter_request(request)))
        except Exception as e:
            print(json.dumps(adapter_error(str(e))))
        return

    command = sys.argv[1]

    input_data = sys.stdin.read().strip()

    try:
        if command == "parse-www-authenticate":
            challenge = parse_www_authenticate(input_data)
            print(json.dumps(success(challenge_to_dict(challenge))))
        elif command == "parse-authorization":
            credential = parse_authorization(input_data)
            print(json.dumps(success(credential_to_dict(credential))))
        elif command == "parse-receipt":
            receipt = parse_payment_receipt(input_data)
            print(json.dumps(success(receipt_to_dict(receipt))))
        elif command == "format-www-authenticate":
            data = json.loads(input_data)
            if not data.get("id"):
                raise ValueError("id must not be empty")
            challenge = Challenge(
                id=data["id"],
                method=data["method"],
                intent=data["intent"],
                request=data["request"],
                realm=data.get("realm", ""),
                digest=data.get("digest"),
                expires=data.get("expires"),
                description=data.get("description"),
            )
            result = format_www_authenticate(challenge, data.get("realm", ""))
            print(json.dumps(success(result)))
        elif command == "format-authorization":
            data = json.loads(input_data)
            challenge_data = data.get("challenge", {})
            request_obj = challenge_data.get("request", {})
            request_json = json.dumps(request_obj, separators=(",", ":"))
            request_b64 = base64url_encode(request_json)
            echo = ChallengeEcho(
                id=challenge_data.get("id", ""),
                realm=challenge_data.get("realm", ""),
                method=challenge_data.get("method", ""),
                intent=challenge_data.get("intent", ""),
                request=request_b64,
                expires=challenge_data.get("expires"),
                digest=challenge_data.get("digest"),
                **{
                    key: challenge_data[key]
                    for key in ("opaque", "header")
                    if challenge_data.get(key) is not None
                },
            )
            credential = Credential(
                challenge=echo,
                payload=data.get("payload", {}),
                source=data.get("source"),
            )
            result = format_authorization(credential)
            print(json.dumps(success(result)))
        elif command == "format-receipt":
            data = json.loads(input_data)
            from datetime import datetime, UTC

            ts_str = data.get("timestamp", "")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            receipt = Receipt(
                status=data["status"],
                timestamp=ts,
                reference=data["reference"],
                method=data.get("method", ""),
                external_id=data.get("externalId"),
                subscription_id=data.get("subscriptionId"),
                extra=data.get("extra"),
                extensions={
                    key: value
                    for key, value in data.items()
                    if key not in RECEIPT_BASE_FIELDS
                }
                or None,
            )
            result = format_payment_receipt(receipt)
            print(json.dumps(success(result)))
        elif command == "base64url-encode":
            result = base64url_encode(input_data)
            print(json.dumps(success(result)))
        elif command == "base64url-decode":
            result = base64url_decode(input_data)
            print(json.dumps(success(result)))
        elif command == "generate-challenge-id":
            params = json.loads(input_data)
            result = generate_conformance_challenge_id(
                secret_key=params["secretKey"],
                realm=params.get("realm", ""),
                method=params.get("method", ""),
                intent=params.get("intent", ""),
                request=params.get("request", {}),
                expires=params.get("expires"),
                digest=params.get("digest"),
                header=params.get("header"),
                opaque=params.get("opaque"),
            )
            print(json.dumps(success(result)))
        elif command == "verify-tempo-receipt":
            params = json.loads(input_data)
            print(json.dumps(asyncio.run(verify_tempo_receipt(params))))
        elif command == "tempo-proof-typed-data":
            params = json.loads(input_data)
            result = tempo_proof_typed_data(
                account=params["account"],
                chain_id=params["chainId"],
                challenge_id=params["challengeId"],
                realm=params["realm"],
            )
            print(json.dumps(success(result)))
        else:
            print(json.dumps(error(f"Unknown command: {command}")))
    except Exception as e:
        if command.startswith("parse-"):
            error_type = "parse_error"
        elif command.startswith("format-"):
            error_type = "format_error"
        elif command.startswith("base64url-"):
            error_type = "encoding_error"
        elif command.startswith("generate-"):
            error_type = "generation_error"
        else:
            error_type = "unknown_error"
        print(json.dumps(error(str(e), error_type)))



if __name__ == "__main__":
    main()
