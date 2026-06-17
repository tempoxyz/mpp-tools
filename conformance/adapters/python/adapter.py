#!/usr/bin/env python3
"""Python conformance adapter using the pympp SDK."""

import sys
import json
import base64
import hashlib
import hmac
import subprocess
import asyncio

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


def base64url_encode(data: str) -> str:
    """Encode a string to base64url without padding."""
    encoded = base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def base64url_decode(data: str) -> str:
    """Decode a base64url string (with or without padding)."""
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", data)
    padding = 4 - (len(cleaned) % 4)
    if padding != 4:
        cleaned += "=" * padding
    decoded = base64.urlsafe_b64decode(cleaned)
    return decoded.decode("utf-8")


def canonical_json(value) -> str:
    """Encode JSON with stable ordering for challenge-id vectors."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def generate_conformance_challenge_id(*, secret_key: str, realm: str, method: str, intent: str, request, expires: str | None = None, digest: str | None = None, opaque: str | None = None) -> str:
    request_b64 = base64.urlsafe_b64encode(canonical_json(request or {}).encode("utf-8")).decode("ascii").rstrip("=")
    payload = "|".join([
        realm,
        method,
        intent,
        request_b64,
        expires or "",
        digest or "",
        opaque or "",
    ])
    signature = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


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
    if opaque is not None:
        result["opaque"] = opaque
    return result


def credential_to_dict(credential: Credential) -> dict:
    """Convert a Credential to a JSON-serializable dict."""
    challenge = credential.challenge
    # Decode the base64url request string back to an object
    request_decoded = json.loads(
        base64.urlsafe_b64decode(challenge.request + "=" * (-len(challenge.request) % 4))
    )

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
    if receipt.extra is not None:
        result["extra"] = receipt.extra
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
}


class FakeRpcResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeRpcClient:
    def __init__(self, receipt):
        self._receipt = receipt

    async def post(self, *args, **kwargs):
        return FakeRpcResponse({"jsonrpc": "2.0", "result": self._receipt, "id": 1})


def adapter_success(value):
    return {"ok": True, "value": value}


def adapter_error(message: str, error_type: str = "unknown_error"):
    return {"ok": False, "error": {"type": error_type, "message": message}}


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
    intent._http_client = FakeRpcClient(input_value["receipt"])

    try:
        receipt = await intent.verify(credential, challenge_data["request"])
    except VerificationError as exc:
        return error(str(exc), "verification_error")

    return success({"status": receipt.status, "reference": receipt.reference})


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
                extra=data.get("extra"),
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
                opaque=params.get("opaque"),
            )
            print(json.dumps(success(result)))
        elif command == "verify-tempo-receipt":
            params = json.loads(input_data)
            print(json.dumps(asyncio.run(verify_tempo_receipt(params))))
        else:
            print(json.dumps(error(f"Unknown command: {command}")))
    except Exception as e:
        if command.startswith("parse-") or command == "base64url-decode":
            error_type = "parse_error"
        elif command.startswith("format-"):
            error_type = "format_error"
        elif command == "base64url-encode":
            error_type = "encoding_error"
        elif command.startswith("generate-"):
            error_type = "generation_error"
        elif command.startswith("verify-tempo-receipt"):
            error_type = "verification_error"
        else:
            error_type = "unknown_error"
        print(json.dumps(error(str(e), error_type)))



if __name__ == "__main__":
    main()
