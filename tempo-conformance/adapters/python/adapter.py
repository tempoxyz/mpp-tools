"""pytempo adapter for the Tempo SDK conformance suite (see ../../SPEC.md).

Reads one JSON request per stdin line, writes one JSON response per line.
Only glue lives here: JSON <-> pytempo types. All encoding, hashing and
signing goes through pytempo's public API.
"""

from __future__ import annotations

import json
import re
import sys
import traceback

import rlp
from eth_utils import keccak

import pytempo
from pytempo import (
    AccessListItem,
    Call,
    KeyAuthorization,
    SignatureType,
    TempoTransaction,
    TokenLimit,
)


class AdapterError(Exception):
    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type = type_
        self.message = message


def invalid(msg: str) -> AdapterError:
    return AdapterError("invalid_input", msg)


def unsupported(msg: str) -> AdapterError:
    return AdapterError("unsupported", msg)


# --------------------------------------------------------------------------
# JSON glue
# --------------------------------------------------------------------------

_DEC = re.compile(r"^(0|[1-9][0-9]*)$")
_HEX = re.compile(r"^0x([0-9a-fA-F]{2})*$")


def req(obj: dict, key: str, where: str):
    if not isinstance(obj, dict):
        raise invalid(f"{where} must be an object")
    if key not in obj or obj[key] is None:
        raise invalid(f"{where}.{key} is required")
    return obj[key]


def opt(obj: dict, key: str):
    return obj.get(key) if isinstance(obj, dict) else None


def to_int(v, where: str) -> int:
    if isinstance(v, bool):
        raise invalid(f"{where} must be a decimal string")
    if isinstance(v, int):
        if v < 0:
            raise invalid(f"{where} must be non-negative")
        return v
    if isinstance(v, str) and _DEC.match(v):
        return int(v)
    raise invalid(f"{where} must be a non-negative decimal string, got {v!r}")


def to_opt_int(v, where: str):
    return None if v is None else to_int(v, where)


def to_hex_bytes(v, where: str) -> bytes:
    if not isinstance(v, str) or not _HEX.match(v):
        raise invalid(f"{where} must be 0x-prefixed even-length hex, got {v!r}")
    return bytes.fromhex(v[2:])


def to_addr(v, where: str) -> bytes:
    b = to_hex_bytes(v, where)
    if len(b) != 20:
        raise invalid(f"{where} must be a 20-byte address")
    return b


def to_key(v, where: str) -> str:
    b = to_hex_bytes(v, where)
    if len(b) != 32:
        raise invalid(f"{where} must be a 32-byte private key")
    return "0x" + b.hex()


def hx(b: bytes) -> str:
    return "0x" + bytes(b).hex()


_KEY_TYPES = {
    "secp256k1": SignatureType.SECP256K1,
    "p256": SignatureType.P256,
    "webauthn": SignatureType.WEBAUTHN,
}


def build_key_authorization(a: dict) -> KeyAuthorization:
    where = "authorization"
    chain_id = to_int(req(a, "chainId", where), f"{where}.chainId")
    kt = req(a, "keyType", where)
    if kt not in _KEY_TYPES:
        raise invalid(f"{where}.keyType must be one of {sorted(_KEY_TYPES)}")
    key_id = to_addr(req(a, "keyId", where), f"{where}.keyId")
    expiry = to_opt_int(opt(a, "expiry"), f"{where}.expiry")
    limits_in = opt(a, "limits")
    limits = None
    if limits_in is not None:
        if not isinstance(limits_in, list):
            raise invalid(f"{where}.limits must be an array or null")
        limits = []
        for i, lim in enumerate(limits_in):
            w = f"{where}.limits[{i}]"
            token = to_addr(req(lim, "token", w), f"{w}.token")
            limit = to_int(req(lim, "limit", w), f"{w}.limit")
            period = to_opt_int(opt(lim, "period"), f"{w}.period") or 0
            if period != 0:
                # pytempo/keychain.py KeyAuthorization.__attrs_post_init__ rejects
                # period != 0 and TokenLimit.to_rlp never encodes period.
                raise unsupported(
                    "pytempo inline KeyAuthorization does not support periodic "
                    "token limits (keychain.py: TokenLimit.to_rlp drops period; "
                    "KeyAuthorization.__attrs_post_init__ raises for period != 0)"
                )
            limits.append(TokenLimit(token=token, limit=limit, period=period))
    try:
        return KeyAuthorization(
            key_id=key_id,
            chain_id=chain_id,
            key_type=_KEY_TYPES[kt],
            expiry=expiry,
            limits=limits,
        )
    except (ValueError, TypeError) as e:
        raise invalid(f"pytempo rejected key authorization: {e}") from e


# --------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------


def op_key_authorization_sign(inp: dict) -> dict:
    auth = build_key_authorization(req(inp, "authorization", "input"))
    pk = to_key(req(inp, "privateKey", "input"), "input.privateKey")
    signed = auth.sign(pk)
    return {
        "signingHash": hx(auth.signature_hash()),
        # pytempo's own 65-byte form (r || s || v, v = 27/28), the same bytes it
        # puts in "serialized" and that the node's PrimitiveSignature uses; the
        # vectors expect this form.
        "signature": hx(signed.signature.to_bytes()),
        "serialized": hx(signed.rlp_encode()),
    }


def build_tx(t: dict, sponsored: bool) -> TempoTransaction:
    w = "tx"
    calls_in = req(t, "calls", w)
    if not isinstance(calls_in, list):
        raise invalid("tx.calls must be an array")
    calls = []
    for i, c in enumerate(calls_in):
        cw = f"tx.calls[{i}]"
        if not isinstance(c, dict):
            raise invalid(f"{cw} must be an object")
        to = c.get("to")
        # pytempo models contract creation as an empty 'to' address.
        to_b = b"" if to is None else to_addr(to, f"{cw}.to")
        value = to_int(c.get("value", "0"), f"{cw}.value")
        data = to_hex_bytes(c.get("data", "0x"), f"{cw}.data")
        calls.append(Call.create(to=to_b, value=value, data=data))
    al_in = opt(t, "accessList") or []
    if not isinstance(al_in, list):
        raise invalid("tx.accessList must be an array")
    access_list = []
    for i, it in enumerate(al_in):
        aw = f"tx.accessList[{i}]"
        addr = to_addr(req(it, "address", aw), f"{aw}.address")
        keys = it.get("storageKeys") or []
        if not isinstance(keys, list):
            raise invalid(f"{aw}.storageKeys must be an array")
        kb = []
        for j, k in enumerate(keys):
            b = to_hex_bytes(k, f"{aw}.storageKeys[{j}]")
            if len(b) != 32:
                raise invalid(f"{aw}.storageKeys[{j}] must be 32 bytes")
            kb.append(b)
        access_list.append(AccessListItem.create(address=addr, storage_keys=tuple(kb)))
    fee_token = opt(t, "feeToken")
    fee_token_b = None if fee_token is None else to_addr(fee_token, "tx.feeToken")
    ka_in = opt(t, "keyAuthorization")
    signed_ka = None
    if ka_in is not None:
        ka = build_key_authorization(req(ka_in, "authorization", "tx.keyAuthorization"))
        ka_pk = to_key(
            req(ka_in, "privateKey", "tx.keyAuthorization"),
            "tx.keyAuthorization.privateKey",
        )
        signed_ka = ka.sign(ka_pk)
    try:
        return TempoTransaction.create(
            chain_id=to_int(req(t, "chainId", w), "tx.chainId"),
            nonce=to_int(req(t, "nonce", w), "tx.nonce"),
            nonce_key=to_int(req(t, "nonceKey", w), "tx.nonceKey"),
            gas_limit=to_int(req(t, "gas", w), "tx.gas"),
            max_fee_per_gas=to_int(req(t, "maxFeePerGas", w), "tx.maxFeePerGas"),
            max_priority_fee_per_gas=to_int(
                req(t, "maxPriorityFeePerGas", w), "tx.maxPriorityFeePerGas"
            ),
            calls=tuple(calls),
            access_list=tuple(access_list),
            valid_before=to_opt_int(opt(t, "validBefore"), "tx.validBefore"),
            valid_after=to_opt_int(opt(t, "validAfter"), "tx.validAfter"),
            fee_token=fee_token_b,
            awaiting_fee_payer=sponsored,
            key_authorization=signed_ka,
        )
    except (ValueError, TypeError) as e:
        raise invalid(f"pytempo rejected transaction: {e}") from e


def op_tx_sign(inp: dict) -> dict:
    fp_in = opt(inp, "feePayerPrivateKey")
    fp_key = None if fp_in is None else to_key(fp_in, "input.feePayerPrivateKey")
    signer = req(inp, "signer", "input")
    stype = req(signer, "type", "input.signer")
    tx = build_tx(req(inp, "tx", "input"), sponsored=fp_key is not None)

    try:
        if stype == "secp256k1":
            pk = to_key(req(signer, "privateKey", "input.signer"), "signer.privateKey")
            sender_hash = tx.get_signing_hash()
            signed = tx.sign(pk)
        elif stype == "keychain":
            ak = to_key(
                req(signer, "accessKeyPrivateKey", "input.signer"),
                "signer.accessKeyPrivateKey",
            )
            root = to_addr(req(signer, "rootAccount", "input.signer"), "signer.rootAccount")
            sender_hash = tx.get_signing_hash()
            signed = tx.sign_access_key(ak, hx(root))
        else:
            raise invalid(f"unknown signer type {stype!r}")

        fee_payer_hash = None
        if fp_key is not None:
            fee_payer_hash = signed.get_signing_hash(for_fee_payer=True)
            signed = signed.sign(fp_key, for_fee_payer=True)

        raw = signed.encode()
        h = signed.hash()
    except AdapterError:
        raise
    except (ValueError, TypeError, rlp.exceptions.SerializationError) as e:
        raise invalid(f"pytempo rejected transaction: {e}") from e

    assert h == keccak(raw)
    return {
        "senderSigningHash": hx(sender_hash),
        "feePayerSigningHash": None if fee_payer_hash is None else hx(fee_payer_hash),
        "raw": hx(raw),
        "hash": hx(h),
        "sender": hx(signed.sender_address),
    }


def op_tx_decode(inp: dict) -> dict:
    to_hex_bytes(req(inp, "raw", "input"), "input.raw")
    # pytempo 0.6.2 has no API to parse a signed 0x76 transaction:
    # TempoTransaction (pytempo/models.py) only has encode()/from_dict(), and
    # no signature recovery for sender or fee payer.
    raise unsupported(
        "pytempo has no decoder for signed Tempo transactions "
        "(models.py TempoTransaction has encode() and from_dict() only; "
        "no sender/fee payer recovery)"
    )


OPS = {
    "tx.sign": op_tx_sign,
    "tx.decode": op_tx_decode,
    "key_authorization.sign": op_key_authorization_sign,
}


def handle(line: str) -> dict:
    try:
        try:
            req_obj = json.loads(line)
        except Exception as e:
            raise invalid(f"request is not valid JSON: {e}")
        if not isinstance(req_obj, dict):
            raise invalid("request must be a JSON object")
        op = req_obj.get("op")
        fn = OPS.get(op)
        if fn is None:
            raise unsupported(f"unknown op {op!r}")
        inp = req_obj.get("input")
        if not isinstance(inp, dict):
            raise invalid("input must be an object")
        return {"ok": True, "value": fn(inp)}
    except AdapterError as e:
        return {"ok": False, "error": {"type": e.type, "message": e.message}}
    except Exception as e:  # never crash
        traceback.print_exc(file=sys.stderr)
        return {
            "ok": False,
            "error": {"type": "unknown_error", "message": f"{type(e).__name__}: {e}"},
        }


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        resp = handle(line)
        sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
