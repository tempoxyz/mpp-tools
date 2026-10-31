//! Tempo conformance adapter for the Rust SDK (crates.io tempo-primitives).
//! Reads one JSON request per stdin line, writes one JSON response per line.
//! See ../../SPEC.md for the ABI.

use std::io::{BufRead, Write};
use std::num::NonZeroU64;

use alloy_consensus::transaction::SignerRecoverable;
use alloy_eips::eip2718::{Decodable2718, Encodable2718};
use alloy_eips::eip2930::{AccessList, AccessListItem};
use alloy_primitives::{Address, B256, Bytes, Signature, TxKind, U256, hex, keccak256};
use alloy_signer::SignerSync;
use alloy_signer_local::PrivateKeySigner;
use serde_json::{Map, Value, json};
use tempo_primitives::transaction::{
    AASigned, Call, KeyAuthorization, KeychainSignature, PrimitiveSignature, SignatureType,
    SignedKeyAuthorization, TempoSignature, TempoTransaction, TokenLimit,
};

#[derive(Debug)]
struct Err {
    ty: &'static str,
    msg: String,
}

type R<T> = Result<T, Err>;

fn invalid(msg: impl Into<String>) -> Err {
    Err { ty: "invalid_input", msg: msg.into() }
}
fn decode_err(msg: impl Into<String>) -> Err {
    Err { ty: "decode_error", msg: msg.into() }
}
fn verification(msg: impl Into<String>) -> Err {
    Err { ty: "verification_error", msg: msg.into() }
}
fn unknown(msg: impl Into<String>) -> Err {
    Err { ty: "unknown_error", msg: msg.into() }
}

// ---------- input helpers ----------

fn obj<'a>(v: &'a Value, what: &str) -> R<&'a Map<String, Value>> {
    v.as_object().ok_or_else(|| invalid(format!("{what}: expected object")))
}

/// Field that may be absent or null.
fn opt<'a>(o: &'a Map<String, Value>, k: &str) -> Option<&'a Value> {
    match o.get(k) {
        None | Some(Value::Null) => None,
        Some(v) => Some(v),
    }
}

fn req<'a>(o: &'a Map<String, Value>, k: &str) -> R<&'a Value> {
    opt(o, k).ok_or_else(|| invalid(format!("missing field {k}")))
}

fn int_str(v: &Value, k: &str) -> R<String> {
    match v {
        Value::String(s) => Ok(s.clone()),
        Value::Number(n) if n.is_u64() => Ok(n.to_string()),
        _ => Err(invalid(format!("{k}: expected decimal string"))),
    }
}

fn parse_u256(v: &Value, k: &str) -> R<U256> {
    let s = int_str(v, k)?;
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(invalid(format!("{k}: not a decimal integer: {s:?}")));
    }
    U256::from_str_radix(&s, 10).map_err(|e| invalid(format!("{k}: {e}")))
}

fn parse_u64(v: &Value, k: &str) -> R<u64> {
    let n = parse_u256(v, k)?;
    u64::try_from(n).map_err(|_| invalid(format!("{k}: does not fit u64")))
}

fn parse_u128(v: &Value, k: &str) -> R<u128> {
    let n = parse_u256(v, k)?;
    u128::try_from(n).map_err(|_| invalid(format!("{k}: does not fit u128")))
}

fn parse_bytes(v: &Value, k: &str) -> R<Vec<u8>> {
    let s = v.as_str().ok_or_else(|| invalid(format!("{k}: expected hex string")))?;
    let h = s.strip_prefix("0x").ok_or_else(|| invalid(format!("{k}: missing 0x prefix")))?;
    hex::decode(h).map_err(|e| invalid(format!("{k}: {e}")))
}

fn parse_address(v: &Value, k: &str) -> R<Address> {
    let b = parse_bytes(v, k)?;
    if b.len() != 20 {
        return Err(invalid(format!("{k}: expected 20 bytes")));
    }
    Ok(Address::from_slice(&b))
}

fn parse_b256(v: &Value, k: &str) -> R<B256> {
    let b = parse_bytes(v, k)?;
    if b.len() != 32 {
        return Err(invalid(format!("{k}: expected 32 bytes")));
    }
    Ok(B256::from_slice(&b))
}

fn parse_signer(v: &Value, k: &str) -> R<PrivateKeySigner> {
    let key = parse_b256(v, k)?;
    PrivateKeySigner::from_bytes(&key).map_err(|e| invalid(format!("{k}: {e}")))
}

fn sign(signer: &PrivateKeySigner, hash: &B256) -> R<Signature> {
    signer.sign_hash_sync(hash).map_err(|e| unknown(format!("signing failed: {e}")))
}

fn parse_key_type(v: &Value) -> R<SignatureType> {
    match v.as_str() {
        Some("secp256k1") => Ok(SignatureType::Secp256k1),
        Some("p256") => Ok(SignatureType::P256),
        Some("webauthn") => Ok(SignatureType::WebAuthn),
        _ => Err(invalid(format!("keyType: unknown {v}"))),
    }
}

fn key_type_str(t: SignatureType) -> &'static str {
    match t {
        SignatureType::Secp256k1 => "secp256k1",
        SignatureType::P256 => "p256",
        SignatureType::WebAuthn => "webauthn",
    }
}

/// Optional non-zero u64 (validBefore, validAfter, expiry). The SDK types these as
/// Option<NonZeroU64>; "0" maps to None (same as absent).
fn parse_opt_nz(o: &Map<String, Value>, k: &str) -> R<Option<NonZeroU64>> {
    match opt(o, k) {
        None => Ok(None),
        Some(v) => Ok(NonZeroU64::new(parse_u64(v, k)?)),
    }
}

fn parse_key_authorization(v: &Value) -> R<KeyAuthorization> {
    let o = obj(v, "authorization")?;
    let chain_id = parse_u64(req(o, "chainId")?, "chainId")?;
    let key_type = parse_key_type(req(o, "keyType")?)?;
    let key_id = parse_address(req(o, "keyId")?, "keyId")?;
    let mut auth = KeyAuthorization::unrestricted(chain_id, key_type, key_id);
    auth.expiry = parse_opt_nz(o, "expiry")?;
    auth.limits = match opt(o, "limits") {
        None => None,
        Some(Value::Array(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for it in items {
                let l = obj(it, "limit")?;
                out.push(TokenLimit {
                    token: parse_address(req(l, "token")?, "token")?,
                    limit: parse_u256(req(l, "limit")?, "limit")?,
                    period: match opt(l, "period") {
                        None => 0,
                        Some(p) => parse_u64(p, "period")?,
                    },
                });
            }
            Some(out)
        }
        Some(_) => return Err(invalid("limits: expected array or null")),
    };
    Ok(auth)
}

fn sign_key_authorization(auth: KeyAuthorization, key: &PrivateKeySigner) -> R<SignedKeyAuthorization> {
    let hash = auth.signature_hash();
    let sig = sign(key, &hash)?;
    Ok(auth.into_signed(PrimitiveSignature::Secp256k1(sig)))
}

fn parse_tx(v: &Value) -> R<TempoTransaction> {
    let o = obj(v, "tx")?;
    let mut calls = Vec::new();
    match req(o, "calls")? {
        Value::Array(items) => {
            for it in items {
                let c = obj(it, "call")?;
                let to = match opt(c, "to") {
                    None => TxKind::Create,
                    Some(t) => TxKind::Call(parse_address(t, "to")?),
                };
                let value = match opt(c, "value") {
                    None => U256::ZERO,
                    Some(x) => parse_u256(x, "value")?,
                };
                let input = match opt(c, "data") {
                    None => Bytes::new(),
                    Some(x) => Bytes::from(parse_bytes(x, "data")?),
                };
                calls.push(Call { to, value, input });
            }
        }
        _ => return Err(invalid("calls: expected array")),
    }
    let mut access_list = Vec::new();
    if let Some(al) = opt(o, "accessList") {
        let items = al.as_array().ok_or_else(|| invalid("accessList: expected array"))?;
        for it in items {
            let a = obj(it, "accessList item")?;
            let mut keys = Vec::new();
            if let Some(ks) = opt(a, "storageKeys") {
                for k in ks.as_array().ok_or_else(|| invalid("storageKeys: expected array"))? {
                    keys.push(parse_b256(k, "storageKeys")?);
                }
            }
            access_list.push(AccessListItem {
                address: parse_address(req(a, "address")?, "address")?,
                storage_keys: keys,
            });
        }
    }
    let key_authorization = match opt(o, "keyAuthorization") {
        None => None,
        Some(ka) => {
            let ko = obj(ka, "keyAuthorization")?;
            let auth = parse_key_authorization(req(ko, "authorization")?)?;
            let key = parse_signer(req(ko, "privateKey")?, "keyAuthorization.privateKey")?;
            Some(sign_key_authorization(auth, &key)?)
        }
    };
    Ok(TempoTransaction {
        chain_id: parse_u64(req(o, "chainId")?, "chainId")?,
        fee_token: opt(o, "feeToken").map(|t| parse_address(t, "feeToken")).transpose()?,
        max_priority_fee_per_gas: parse_u128(req(o, "maxPriorityFeePerGas")?, "maxPriorityFeePerGas")?,
        max_fee_per_gas: parse_u128(req(o, "maxFeePerGas")?, "maxFeePerGas")?,
        gas_limit: parse_u64(req(o, "gas")?, "gas")?,
        calls,
        access_list: AccessList(access_list),
        nonce_key: parse_u256(req(o, "nonceKey")?, "nonceKey")?,
        nonce: parse_u64(req(o, "nonce")?, "nonce")?,
        fee_payer_signature: None,
        valid_before: parse_opt_nz(o, "validBefore")?,
        valid_after: parse_opt_nz(o, "validAfter")?,
        key_authorization,
        tempo_authorization_list: Vec::new(),
    })
}

// ---------- output helpers ----------

fn hx(b: impl AsRef<[u8]>) -> Value {
    Value::String(format!("0x{}", hex::encode(b.as_ref())))
}
fn dec(n: impl ToString) -> Value {
    Value::String(n.to_string())
}

fn key_authorization_json(a: &KeyAuthorization) -> Value {
    json!({
        "chainId": dec(a.chain_id),
        "keyType": key_type_str(a.key_type),
        "keyId": hx(a.key_id),
        "expiry": a.expiry.map(|e| dec(e.get())),
        "limits": a.limits.as_ref().map(|ls| ls.iter().map(|l| json!({
            "token": hx(l.token),
            "limit": dec(l.limit),
            "period": dec(l.period),
        })).collect::<Vec<_>>()),
    })
}

// ---------- ops ----------

fn op_tx_sign(input: &Value) -> R<Value> {
    let o = obj(input, "input")?;
    let mut tx = parse_tx(req(o, "tx")?)?;
    // The SDK's structural checks (non-empty calls, CREATE placement, validBefore > validAfter).
    tx.validate().map_err(|e| invalid(format!("TempoTransaction::validate: {e}")))?;
    let s = obj(req(o, "signer")?, "signer")?;
    let fee_payer = opt(o, "feePayerPrivateKey")
        .map(|k| parse_signer(k, "feePayerPrivateKey"))
        .transpose()?;

    enum Kind {
        Secp(PrivateKeySigner),
        Keychain(PrivateKeySigner, Address),
    }
    let kind = match s.get("type").and_then(Value::as_str) {
        Some("secp256k1") => Kind::Secp(parse_signer(req(s, "privateKey")?, "privateKey")?),
        Some("keychain") => Kind::Keychain(
            parse_signer(req(s, "accessKeyPrivateKey")?, "accessKeyPrivateKey")?,
            parse_address(req(s, "rootAccount")?, "rootAccount")?,
        ),
        other => return Err(invalid(format!("signer.type: unknown {other:?}"))),
    };

    // Sponsored: the sender's payload is the fee-payer form, selected in the SDK by
    // the presence of a fee payer signature (its value is a placeholder until co-signed).
    if fee_payer.is_some() {
        tx.fee_payer_signature = Some(Signature::new(U256::ZERO, U256::ZERO, false));
    }
    let sender_hash = tx.signature_hash();

    let (signature, sender) = match &kind {
        Kind::Secp(k) => {
            let sig = sign(k, &sender_hash)?;
            (TempoSignature::Primitive(PrimitiveSignature::Secp256k1(sig)), k.address())
        }
        Kind::Keychain(k, root) => {
            let digest = KeychainSignature::signing_hash(sender_hash, *root);
            let sig = sign(k, &digest)?;
            (
                TempoSignature::Keychain(KeychainSignature::new(*root, PrimitiveSignature::Secp256k1(sig))),
                *root,
            )
        }
    };

    let fee_payer_hash = match &fee_payer {
        Some(fp) => {
            let h = tx.fee_payer_signature_hash(sender);
            tx.fee_payer_signature = Some(sign(fp, &h)?);
            Some(h)
        }
        None => None,
    };

    let signed: AASigned = tx.into_signed(signature);
    let raw = signed.encoded_2718();
    Ok(json!({
        "senderSigningHash": hx(sender_hash),
        "feePayerSigningHash": fee_payer_hash.map(hx),
        "raw": hx(&raw),
        "hash": hx(keccak256(&raw)),
        "sender": hx(sender),
    }))
}

fn op_tx_decode(input: &Value) -> R<Value> {
    let o = obj(input, "input")?;
    let raw_v = req(o, "raw")?;
    let s = raw_v.as_str().ok_or_else(|| invalid("raw: expected hex string"))?;
    let h = s.strip_prefix("0x").ok_or_else(|| decode_err("raw: missing 0x prefix"))?;
    let raw = hex::decode(h).map_err(|e| decode_err(format!("raw: {e}")))?;

    let signed = AASigned::decode_2718_exact(&raw).map_err(|e| decode_err(format!("{e}")))?;
    let sender = signed
        .recover_signer()
        .map_err(|e| decode_err(format!("sender recovery failed: {e}")))?;
    let tx = signed.tx();
    let fee_payer = match &tx.fee_payer_signature {
        Some(_) => Some(
            tx.recover_fee_payer(sender)
                .map_err(|e| decode_err(format!("fee payer recovery failed: {e}")))?,
        ),
        None => None,
    };
    let signature_type = match signed.signature() {
        TempoSignature::Keychain(_) => "keychain",
        TempoSignature::Primitive(p) => key_type_str(p.signature_type()),
    };
    let calls: Vec<Value> = tx
        .calls
        .iter()
        .map(|c| json!({
            "to": match c.to { TxKind::Create => Value::Null, TxKind::Call(a) => hx(a) },
            "value": dec(c.value),
            "data": hx(&c.input),
        }))
        .collect();
    let access_list: Vec<Value> = tx
        .access_list
        .0
        .iter()
        .map(|i| json!({
            "address": hx(i.address),
            "storageKeys": i.storage_keys.iter().map(hx).collect::<Vec<_>>(),
        }))
        .collect();
    Ok(json!({
        "tx": {
            "chainId": dec(tx.chain_id),
            "nonce": dec(tx.nonce),
            "nonceKey": dec(tx.nonce_key),
            "gas": dec(tx.gas_limit),
            "maxFeePerGas": dec(tx.max_fee_per_gas),
            "maxPriorityFeePerGas": dec(tx.max_priority_fee_per_gas),
            "calls": calls,
            "accessList": access_list,
            "validBefore": tx.valid_before.map(|v| dec(v.get())),
            "validAfter": tx.valid_after.map(|v| dec(v.get())),
            "feeToken": tx.fee_token.map(hx),
            "keyAuthorization": tx.key_authorization.as_ref().map(|k| key_authorization_json(&k.authorization)),
        },
        "signatureType": signature_type,
        "sender": hx(sender),
        "feePayer": fee_payer.map(hx),
        "hash": hx(signed.hash()),
    }))
}

fn op_key_authorization_sign(input: &Value) -> R<Value> {
    let o = obj(input, "input")?;
    let auth = parse_key_authorization(req(o, "authorization")?)?;
    let key = parse_signer(req(o, "privateKey")?, "privateKey")?;
    let signing_hash = auth.signature_hash();
    let signed = sign_key_authorization(auth, &key)?;
    // The SDK's own 65-byte encoding (PrimitiveSignature::to_bytes), the same bytes that appear
    // inside serialized. v is 27/28 (alloy Signature::as_bytes), as SPEC.md specifies.
    let sig65 = signed.signature.to_bytes();
    Ok(json!({
        "signingHash": hx(signing_hash),
        "signature": hx(&sig65),
        "serialized": hx(alloy_rlp::encode(&signed)),
    }))
}

/// signature.recover: the account a Tempo signature envelope signs for, through
/// tempo-primitives' TempoSignature::from_bytes and recover_signer (the node's path).
fn op_signature_recover(input: &Value) -> R<Value> {
    let o = obj(input, "input")?;
    let hash = parse_b256(req(o, "hash")?, "hash")?;
    let bytes = parse_bytes(req(o, "signature")?, "signature")?;
    let sig = TempoSignature::from_bytes(&bytes).map_err(|e| verification(format!("TempoSignature::from_bytes: {e}")))?;
    let signer = sig.recover_signer(&hash).map_err(|e| verification(format!("recover_signer: {e:?}")))?;
    Ok(json!({ "signer": hx(signer) }))
}

fn handle(line: &str) -> Value {
    let res: R<Value> = (|| {
        let req: Value = serde_json::from_str(line).map_err(|e| invalid(format!("bad JSON: {e}")))?;
        let op = req.get("op").and_then(Value::as_str).ok_or_else(|| invalid("missing op"))?;
        let input = req.get("input").cloned().unwrap_or(Value::Null);
        match op {
            "tx.sign" => op_tx_sign(&input),
            "tx.decode" => op_tx_decode(&input),
            "key_authorization.sign" => op_key_authorization_sign(&input),
            "signature.recover" => op_signature_recover(&input),
            other => Err(Err { ty: "unsupported", msg: format!("unknown op {other}") }),
        }
    })();
    match res {
        Ok(v) => json!({"ok": true, "value": v}),
        Err(e) => json!({"ok": false, "error": {"type": e.ty, "message": e.msg}}),
    }
}

fn main() {
    // Never let a panic inside the SDK kill the process: catch and answer unknown_error.
    std::panic::set_hook(Box::new(|info| eprintln!("panic: {info}")));
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let resp = std::panic::catch_unwind(|| handle(&line)).unwrap_or_else(|p| {
            let msg = p
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "panic".into());
            json!({"ok": false, "error": {"type": "unknown_error", "message": format!("panic: {msg}")}})
        });
        let _ = writeln!(out, "{resp}");
        let _ = out.flush();
    }
}

