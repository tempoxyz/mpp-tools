"""pytempo adapter for the Tempo SDK conformance suite (see ../../SPEC.md).

Reads one JSON request per stdin line, writes one JSON response per line.
Only glue lives here: JSON <-> pytempo types. All encoding, hashing and
signing goes through pytempo's public API.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import traceback
from importlib.metadata import version as _dist_version

import attrs
import rlp
from eth_utils import keccak
from pytempo import (
    AccessListItem,
    Call,
    KeyAuthorization,
    KeychainSignature,
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
# Feature detection (never version checks): what the installed pytempo can do
# --------------------------------------------------------------------------

_SDK = "pytempo"
_SDK_V = f"pytempo {_dist_version('pytempo')}"


def _where(obj, needle: str | None = None) -> str:
    """'pytempo/<file>:<line>' of *obj* in the installed SDK (of *needle* in it)."""
    try:
        lines, start = inspect.getsourcelines(obj)
        rel = "pytempo/" + os.path.basename(inspect.getsourcefile(obj) or "?")
    except (OSError, TypeError):
        return "pytempo"
    for i, line in enumerate(lines):
        if needle is not None and needle in line:
            return f"{rel}:{start + i}"
    return f"{rel}:{start}"


def _probe_periodic_limits() -> bool:
    try:
        lim = TokenLimit(token=b"\x01" * 20, limit=1, period=1)
        KeyAuthorization(key_id=b"\x01" * 20, limits=[lim])
    except (ValueError, TypeError):
        return False
    return len(lim.to_rlp()) == 3


_HAS_DECODE = callable(getattr(TempoTransaction, "decode", None))
_HAS_PERIODIC_LIMITS = _probe_periodic_limits()
_KA_FIELDS = attrs.fields_dict(KeyAuthorization)
_KEYCHAIN_FIELDS = attrs.fields_dict(KeychainSignature)


def _unsupported_decode() -> AdapterError:
    return unsupported(
        f"{_SDK_V} has no decoder for signed Tempo transactions "
        f"({_where(TempoTransaction, 'class TempoTransaction')}: TempoTransaction has encode()/from_dict() "
        "only); pytempo#134 adds TempoTransaction.decode, unreleased"
    )


def _unsupported_periodic() -> AdapterError:
    return unsupported(
        f"{_SDK_V} inline KeyAuthorization rejects periodic token limits "
        f"({_where(KeyAuthorization.__attrs_post_init__, 'lim.period != 0')}) and "
        f"TokenLimit.to_rlp never encodes period ({_where(TokenLimit.to_rlp)}); "
        "pytempo#134 adds them, unreleased"
    )


def _unsupported_allowed_calls() -> AdapterError:
    return unsupported(
        f"{_SDK} has no inline KeyAuthorization call scopes (TIP-1011 "
        "allowed_calls): KeyAuthorization has no allowed_calls field and always "
        "writes it absent "
        f"({_where(KeyAuthorization.as_rlp_payload, 'allowed_calls (unsupported')}"
        f"), and decoding refuses it "
        f"({_where(KeyAuthorization.from_rlp, 'allowed_calls is not supported')}); "
        "call scopes exist only via AccountKeychain.authorize_key"
    )


def _unsupported_keychain_v1() -> AdapterError:
    if "version" not in _KEYCHAIN_FIELDS:
        return unsupported(
            f"{_SDK_V} has no keychain V1 envelope: KeychainSignature is V2-only "
            f"(type byte 0x04, {_where(KeychainSignature, 'TYPE_BYTE: ClassVar')}; "
            "KeychainSignature.sign always derives the V2 digest, "
            f"{_where(KeychainSignature.sign, 'signing_hash = keccak')})"
        )
    return unsupported(
        f"{_SDK} models the keychain V1 envelope (KeychainSignature.version, "
        f"{_where(KeychainSignature, 'version: int')}) but has no API to sign one: "
        "KeychainSignature.sign always derives the V2 digest "
        f"({_where(KeychainSignature.sign, 'signing_hash = keccak')}) and "
        "TempoTransaction.sign_access_key takes no version "
        f"({_where(TempoTransaction.sign_access_key)})"
    )


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
_KEY_TYPE_NAMES = {v: k for k, v in _KEY_TYPES.items()}


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
            if period != 0 and not _HAS_PERIODIC_LIMITS:
                raise _unsupported_periodic()
            try:
                limits.append(TokenLimit(token=token, limit=limit, period=period))
            except (ValueError, TypeError) as e:
                raise invalid(f"pytempo rejected {w}: {e}") from e
    extra = {}
    allowed_calls = opt(a, "allowedCalls")
    if allowed_calls is not None:
        # null = unrestricted; [] = scoped mode with no allowed calls.
        if not isinstance(allowed_calls, list):
            raise invalid(f"{where}.allowedCalls must be an array or null")
        raise _unsupported_allowed_calls()
    witness = opt(a, "witness")
    if witness is not None:
        # 0x00..00 is a present witness, distinct from null.
        wb = to_hex_bytes(witness, f"{where}.witness")
        if len(wb) != 32:
            raise invalid(f"{where}.witness must be 32 bytes")
        extra["witness"] = wb
    is_admin = opt(a, "isAdmin")
    if is_admin is not None and not isinstance(is_admin, bool):
        raise invalid(f"{where}.isAdmin must be a boolean")
    if is_admin:
        extra["is_admin"] = True
    account = opt(a, "account")
    if account is not None:
        extra["account"] = to_addr(account, f"{where}.account")
    for field in extra:
        if field not in _KA_FIELDS:
            raise unsupported(
                f"{_SDK} KeyAuthorization has no {field} field ({_where(KeyAuthorization)})"
            )
    try:
        return KeyAuthorization(
            key_id=key_id,
            chain_id=chain_id,
            key_type=_KEY_TYPES[kt],
            expiry=expiry,
            limits=limits,
            **extra,
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
            root = to_addr(
                req(signer, "rootAccount", "input.signer"), "signer.rootAccount"
            )
            kc_version = opt(signer, "version") or "v2"
            if kc_version not in ("v1", "v2"):
                raise invalid("signer.version must be 'v1' or 'v2'")
            if kc_version == "v1":
                raise _unsupported_keychain_v1()
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


def _opt_str(v):
    return None if v is None else str(v)


def _opt_hex(v):
    return None if v is None else hx(v)


def op_tx_decode(inp: dict) -> dict:
    raw = to_hex_bytes(req(inp, "raw", "input"), "input.raw")
    if not _HAS_DECODE:
        raise _unsupported_decode()
    try:
        tx = TempoTransaction.decode(raw)
        fee_payer = tx.recover_fee_payer()
        h = tx.hash()
    except NotImplementedError as e:
        # pytempo decodes and recovers secp256k1 and secp256k1 keychain senders
        # only (P256/WebAuthn recovery: pytempo#133).
        raise unsupported(f"pytempo cannot decode this transaction: {e}") from e
    except (ValueError, TypeError, rlp.exceptions.RLPException) as e:
        if "allowed_calls is not supported" in str(e):
            # Well-formed, but pytempo cannot represent call scopes.
            raise _unsupported_allowed_calls() from e
        raise AdapterError("decode_error", f"pytempo rejected transaction: {e}") from e

    assert h == keccak(raw)
    ka = None
    if tx.key_authorization is not None:
        auth = tx.key_authorization.authorization
        ka = {
            "chainId": str(auth.chain_id),
            "keyType": _KEY_TYPE_NAMES[auth.key_type],
            "keyId": hx(auth.key_id),
            "expiry": _opt_str(auth.expiry),
            "limits": None
            if auth.limits is None
            else [
                {
                    "token": hx(lim.token),
                    "limit": str(lim.limit),
                    "period": str(lim.period),
                }
                for lim in auth.limits
            ],
            # pytempo refuses call scopes on decode (unsupported above).
            "allowedCalls": None,
            "witness": _opt_hex(getattr(auth, "witness", None)),
            "isAdmin": bool(getattr(auth, "is_admin", False)),
            "account": _opt_hex(getattr(auth, "account", None)),
        }
    return {
        "tx": {
            "chainId": str(tx.chain_id),
            "nonce": str(tx.nonce),
            "nonceKey": str(tx.nonce_key),
            "gas": str(tx.gas_limit),
            "maxFeePerGas": str(tx.max_fee_per_gas),
            "maxPriorityFeePerGas": str(tx.max_priority_fee_per_gas),
            "calls": [
                {
                    "to": hx(c.to) if bytes(c.to) else None,
                    "value": str(c.value),
                    "data": hx(c.data),
                }
                for c in tx.calls
            ],
            "accessList": [
                {
                    "address": hx(a.address),
                    "storageKeys": [hx(k) for k in a.storage_keys],
                }
                for a in tx.access_list
            ],
            "validBefore": _opt_str(tx.valid_before),
            "validAfter": _opt_str(tx.valid_after),
            "feeToken": None if tx.fee_token is None else hx(tx.fee_token),
            "keyAuthorization": ka,
        },
        "signatureType": "keychain"
        if isinstance(tx.sender_signature, KeychainSignature)
        else "secp256k1",
        "sender": hx(tx.sender_address),
        "feePayer": None if fee_payer is None else fee_payer.lower(),
        "hash": hx(h),
    }


OPS = {
    "tx.sign": op_tx_sign,
    "tx.decode": op_tx_decode,
    "key_authorization.sign": op_key_authorization_sign,
}


def handle(line: str) -> dict:
    try:
        try:
            req_obj = json.loads(line)
        except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001  never crash
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
