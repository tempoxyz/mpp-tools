use hmac::{Hmac, KeyInit, Mac};
use mpp::protocol::core::{
    base64url_decode, base64url_encode, format_authorization, format_receipt,
    format_www_authenticate, parse_authorization, parse_receipt, parse_www_authenticate,
    Base64UrlJson, ChallengeEcho, PaymentChallenge, PaymentCredential, Receipt,
};
use serde_json::{json, Value};
use sha2::Sha256;
use std::io::{self, Read, Write};
use std::process::{Command, Stdio};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        handle_adapter_request();
        return;
    }

    let command = &args[1];
    let mut input = String::new();
    if io::stdin().read_to_string(&mut input).is_err() {
        print_error("Failed to read stdin", "unknown_error");
        return;
    }
    let input = input.trim();
    match command.as_str() {
        "parse-www-authenticate" => handle_parse_www_authenticate(input),
        "parse-authorization" => handle_parse_authorization(input),
        "parse-receipt" => handle_parse_receipt(input),
        "format-www-authenticate" => handle_format_www_authenticate(input),
        "format-authorization" => handle_format_authorization(input),
        "format-receipt" => handle_format_receipt(input),
        "base64url-encode" => handle_base64url_encode(input),
        "base64url-decode" => handle_base64url_decode(input),
        "generate-challenge-id" => handle_generate_challenge_id(input),
        _ => print_error(&format!("Unknown command: {}", command), "unknown_error"),
    }
}

fn print_success<T: serde::Serialize>(result: T) {
    let output = serde_json::json!({
        "success": true,
        "result": result
    });
    println!("{}", serde_json::to_string(&output).unwrap());
}

fn print_error(message: &str, error_type: &str) {
    let output = serde_json::json!({
        "success": false,
        "error": message,
        "error_type": error_type
    });
    println!("{}", serde_json::to_string(&output).unwrap());
}

fn str_field(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn opt_str_field(value: &Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn opt_base64url_json_field(value: &Value, key: &str) -> Result<Option<Base64UrlJson>, String> {
    let Some(raw) = value.get(key) else {
        return Ok(None);
    };

    let opaque = match raw {
        Value::Null => return Ok(None),
        Value::String(text) => Base64UrlJson::from_value(&json!({ "_raw": text })),
        Value::Object(_) => Base64UrlJson::from_value(raw),
        _ => return Err(format!("{} must be a string or object", key)),
    }
    .map_err(|e| e.to_string())?;

    Ok(Some(opaque))
}

fn opaque_to_json(opaque: &Base64UrlJson) -> Result<Value, String> {
    let decoded = opaque.decode_value().map_err(|e| e.to_string())?;
    match decoded {
        Value::Object(map) if map.len() == 1 => {
            if let Some(Value::String(raw)) = map.get("_raw") {
                return Ok(Value::String(raw.clone()));
            }
            Ok(Value::Object(map))
        }
        other => Ok(other),
    }
}

fn challenge_to_json(challenge: &PaymentChallenge) -> Result<Value, String> {
    let request_decoded = challenge
        .request
        .decode_value()
        .map_err(|e| format!("Invalid JSON in request: {}", e))?;

    let mut obj = json!({
        "id": challenge.id,
        "realm": challenge.realm.as_str(),
        "method": challenge.method.as_str(),
        "intent": challenge.intent.as_str(),
        "request": request_decoded,
    });

    if let Some(ref expires) = challenge.expires {
        obj["expires"] = json!(expires);
    }
    if let Some(ref description) = challenge.description {
        obj["description"] = json!(description);
    }
    if let Some(ref digest) = challenge.digest {
        obj["digest"] = json!(digest);
    }
    if let Some(ref opaque) = challenge.opaque {
        obj["opaque"] =
            opaque_to_json(opaque).map_err(|e| format!("Invalid JSON in opaque: {}", e))?;
    }

    Ok(obj)
}

fn decode_request_string(request_b64: &str) -> Value {
    let bytes = match base64url_decode(request_b64) {
        Ok(b) => b,
        Err(_) => return json!({}),
    };
    serde_json::from_slice(&bytes).unwrap_or(json!({}))
}

fn credential_to_json(credential: &PaymentCredential) -> Result<Value, String> {
    let challenge_request_decoded = decode_request_string(credential.challenge.request.raw());

    let mut challenge_obj = json!({
        "id": credential.challenge.id,
        "realm": credential.challenge.realm,
        "method": credential.challenge.method,
        "intent": credential.challenge.intent,
        "request": challenge_request_decoded,
    });

    if let Some(ref expires) = credential.challenge.expires {
        challenge_obj["expires"] = json!(expires);
    }
    if let Some(ref digest) = credential.challenge.digest {
        challenge_obj["digest"] = json!(digest);
    }
    if let Some(ref opaque) = credential.challenge.opaque {
        challenge_obj["opaque"] = opaque_to_json(opaque)?;
    }

    let mut obj = json!({
        "challenge": challenge_obj,
        "payload": credential.payload,
    });

    if let Some(ref source) = credential.source {
        obj["source"] = json!(source);
    }

    Ok(obj)
}

fn handle_parse_www_authenticate(input: &str) {
    match parse_www_authenticate(input) {
        Ok(challenge) => match challenge_to_json(&challenge) {
            Ok(json) => print_success(json),
            Err(e) => print_error(&e, "parse_error"),
        },
        Err(e) => print_error(&e.to_string(), "parse_error"),
    }
}

fn handle_parse_authorization(input: &str) {
    match parse_authorization(input) {
        Ok(credential) => match credential_to_json(&credential) {
            Ok(json) => print_success(json),
            Err(e) => print_error(&e, "parse_error"),
        },
        Err(e) => print_error(&e.to_string(), "parse_error"),
    }
}

fn handle_parse_receipt(input: &str) {
    match parse_receipt(input) {
        Ok(receipt) => print_success(receipt),
        Err(e) => print_error(&e.to_string(), "parse_error"),
    }
}

fn handle_format_www_authenticate(input: &str) {
    match serde_json::from_str::<Value>(input) {
        Ok(value) => {
            let request_obj = value.get("request").cloned().unwrap_or(json!({}));
            let request_b64 = match Base64UrlJson::from_value(&request_obj) {
                Ok(b64) => b64,
                Err(e) => {
                    print_error(&e.to_string(), "format_error");
                    return;
                }
            };

            let challenge = PaymentChallenge {
                id: str_field(&value, "id"),
                realm: str_field(&value, "realm"),
                method: str_field(&value, "method").into(),
                intent: str_field(&value, "intent").into(),
                request: request_b64,
                expires: opt_str_field(&value, "expires"),
                description: opt_str_field(&value, "description"),
                digest: opt_str_field(&value, "digest"),
                opaque: match opt_base64url_json_field(&value, "opaque") {
                    Ok(opaque) => opaque,
                    Err(e) => {
                        print_error(&e, "format_error");
                        return;
                    }
                },
            };

            match format_www_authenticate(&challenge) {
                Ok(header) => print_success(header),
                Err(e) => print_error(&e.to_string(), "format_error"),
            }
        }
        Err(e) => print_error(&e.to_string(), "format_error"),
    }
}

fn handle_format_authorization(input: &str) {
    match serde_json::from_str::<Value>(input) {
        Ok(value) => {
            let challenge_val = value.get("challenge").cloned().unwrap_or(json!({}));
            let request_obj = challenge_val.get("request").cloned().unwrap_or(json!({}));
            let request_b64 = match Base64UrlJson::from_value(&request_obj) {
                Ok(b64) => b64,
                Err(e) => {
                    print_error(&e.to_string(), "format_error");
                    return;
                }
            };

            let challenge_echo = ChallengeEcho {
                id: str_field(&challenge_val, "id"),
                realm: str_field(&challenge_val, "realm"),
                method: str_field(&challenge_val, "method").into(),
                intent: str_field(&challenge_val, "intent").into(),
                request: request_b64,
                expires: opt_str_field(&challenge_val, "expires"),
                digest: opt_str_field(&challenge_val, "digest"),
                opaque: match opt_base64url_json_field(&challenge_val, "opaque") {
                    Ok(opaque) => opaque,
                    Err(e) => {
                        print_error(&e, "format_error");
                        return;
                    }
                },
            };

            let payload_val = value.get("payload").cloned().unwrap_or(json!({}));

            let credential = PaymentCredential {
                challenge: challenge_echo,
                payload: payload_val,
                source: opt_str_field(&value, "source"),
            };

            match format_authorization(&credential) {
                Ok(header) => print_success(header),
                Err(e) => print_error(&e.to_string(), "format_error"),
            }
        }
        Err(e) => print_error(&e.to_string(), "format_error"),
    }
}

fn handle_format_receipt(input: &str) {
    match serde_json::from_str::<Receipt>(input) {
        Ok(receipt) => match format_receipt(&receipt) {
            Ok(header) => print_success(header),
            Err(e) => print_error(&e.to_string(), "format_error"),
        },
        Err(e) => print_error(&e.to_string(), "format_error"),
    }
}

fn handle_base64url_encode(input: &str) {
    let encoded = base64url_encode(input.as_bytes());
    print_success(encoded);
}

fn handle_base64url_decode(input: &str) {
    match base64url_decode(input) {
        Ok(decoded) => match String::from_utf8(decoded) {
            Ok(s) => print_success(s),
            Err(e) => print_error(&e.to_string(), "encoding_error"),
        },
        Err(e) => print_error(&e.to_string(), "encoding_error"),
    }
}

fn handle_generate_challenge_id(input: &str) {
    match serde_json::from_str::<serde_json::Value>(input) {
        Ok(params) => {
            let secret_key = match params.get("secretKey").and_then(|v| v.as_str()) {
                Some(s) => s,
                None => {
                    print_error("Missing secretKey", "generation_error");
                    return;
                }
            };
            let realm = str_field(&params, "realm");
            let method = str_field(&params, "method");
            let intent = str_field(&params, "intent");
            let request = params
                .get("request")
                .cloned()
                .unwrap_or(serde_json::json!({}));
            let expires = opt_str_field(&params, "expires");
            let digest = opt_str_field(&params, "digest");
            let opaque = opt_str_field(&params, "opaque");

            let challenge_id_params = ChallengeIdParams {
                secret_key,
                realm: &realm,
                method: &method,
                intent: &intent,
                request: &request,
                expires: expires.as_deref(),
                digest: digest.as_deref(),
                opaque: opaque.as_deref(),
            };

            let id = match generate_conformance_challenge_id(challenge_id_params) {
                Ok(id) => id,
                Err(e) => {
                    print_error(&e.to_string(), "generation_error");
                    return;
                }
            };
            print_success(id);
        }
        Err(e) => print_error(&e.to_string(), "generation_error"),
    }
}

fn print_adapter_success<T: serde::Serialize>(value: T) {
    println!(
        "{}",
        serde_json::to_string(&json!({ "ok": true, "value": value })).unwrap()
    );
}

fn print_adapter_error(message: &str, error_type: &str) {
    println!(
        "{}",
        serde_json::to_string(&json!({
            "ok": false,
            "error": { "type": error_type, "message": message },
        }))
        .unwrap()
    );
}

fn handle_adapter_request() {
    let mut input = String::new();
    if io::stdin().read_to_string(&mut input).is_err() {
        print_adapter_error("Failed to read stdin", "unknown_error");
        return;
    }
    let request: Value = match serde_json::from_str(&input) {
        Ok(value) => value,
        Err(e) => {
            print_adapter_error(&e.to_string(), "unknown_error");
            return;
        }
    };
    let op = request.get("op").and_then(|v| v.as_str()).unwrap_or("");
    let input_value = request.get("input").cloned().unwrap_or(json!({}));

    let Some(command) = legacy_command_for_operation(op) else {
        print_adapter_error(
            &format!("Unknown operation: {}", op),
            "unsupported_operation",
        );
        return;
    };
    let legacy_input = match legacy_input_for_operation(op, &input_value) {
        Ok(value) => value,
        Err(e) => {
            print_adapter_error(&e, "unknown_error");
            return;
        }
    };
    let result = run_legacy_command(command, &legacy_input, &[]);
    print_adapter_from_legacy(op, result);
}

fn legacy_command_for_operation(op: &str) -> Option<&'static str> {
    match op {
        "challenge.parse" => Some("parse-www-authenticate"),
        "challenge.format" => Some("format-www-authenticate"),
        "credential.parse" => Some("parse-authorization"),
        "credential.format" => Some("format-authorization"),
        "receipt.parse" => Some("parse-receipt"),
        "receipt.format" => Some("format-receipt"),
        "base64url.encode" => Some("base64url-encode"),
        "base64url.decode" => Some("base64url-decode"),
        "challenge.id" => Some("generate-challenge-id"),
        _ => None,
    }
}

fn legacy_input_for_operation(op: &str, input: &Value) -> Result<String, String> {
    if op.ends_with(".parse") {
        return input
            .get("header")
            .and_then(|v| v.as_str())
            .map(|value| value.to_string())
            .ok_or_else(|| "missing header".to_string());
    }
    if op.starts_with("base64url.") {
        return input
            .get("text")
            .and_then(|v| v.as_str())
            .map(|value| value.to_string())
            .ok_or_else(|| "missing text".to_string());
    }
    serde_json::to_string(input).map_err(|e| e.to_string())
}

fn run_legacy_command(command: &str, input: &str, env: &[(&str, &str)]) -> Value {
    let executable = match std::env::current_exe() {
        Ok(value) => value,
        Err(e) => {
            return json!({ "success": false, "error": e.to_string(), "error_type": "unknown_error" })
        }
    };
    let mut child = match Command::new(executable)
        .arg(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .envs(env.iter().copied())
        .spawn()
    {
        Ok(value) => value,
        Err(e) => {
            return json!({ "success": false, "error": e.to_string(), "error_type": "unknown_error" })
        }
    };
    if let Some(mut stdin) = child.stdin.take() {
        if let Err(e) = stdin.write_all(input.as_bytes()) {
            return json!({ "success": false, "error": e.to_string(), "error_type": "unknown_error" });
        }
    }
    let output = match child.wait_with_output() {
        Ok(value) => value,
        Err(e) => {
            return json!({ "success": false, "error": e.to_string(), "error_type": "unknown_error" })
        }
    };
    if !output.status.success() {
        return json!({
            "success": false,
            "error": String::from_utf8_lossy(&output.stderr).to_string(),
            "error_type": "unknown_error",
        });
    }
    serde_json::from_slice(&output.stdout).unwrap_or_else(
        |e| json!({ "success": false, "error": e.to_string(), "error_type": "unknown_error" }),
    )
}

fn print_adapter_from_legacy(op: &str, result: Value) {
    if result.get("success").and_then(|v| v.as_bool()) == Some(false) {
        print_adapter_error(
            result.get("error").and_then(|v| v.as_str()).unwrap_or(""),
            result
                .get("error_type")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown_error"),
        );
        return;
    }
    let value = if result.get("success").is_some() {
        result.get("result").cloned().unwrap_or(Value::Null)
    } else {
        result
    };
    print_adapter_success(adapter_value_for_operation(op, value));
}

fn adapter_value_for_operation(op: &str, result: Value) -> Value {
    if op.ends_with(".format") {
        return json!({ "header": result });
    }
    if op.starts_with("base64url.") {
        return json!({ "text": result });
    }
    if op == "challenge.id" {
        return json!({ "id": result });
    }
    result
}

struct ChallengeIdParams<'a> {
    secret_key: &'a str,
    realm: &'a str,
    method: &'a str,
    intent: &'a str,
    request: &'a Value,
    expires: Option<&'a str>,
    digest: Option<&'a str>,
    opaque: Option<&'a str>,
}

fn generate_conformance_challenge_id(
    params: ChallengeIdParams<'_>,
) -> Result<String, std::fmt::Error> {
    type HmacSha256 = Hmac<Sha256>;

    let request_json = stable_json(params.request)?;
    let request_b64 = base64url_encode(request_json.as_bytes());
    let hmac_input = [
        params.realm,
        params.method,
        params.intent,
        &request_b64,
        params.expires.unwrap_or(""),
        params.digest.unwrap_or(""),
        params.opaque.unwrap_or(""),
    ]
    .join("|");

    let mut mac = HmacSha256::new_from_slice(params.secret_key.as_bytes())
        .expect("HMAC can take key of any size");
    mac.update(hmac_input.as_bytes());
    Ok(base64url_encode(&mac.finalize().into_bytes()))
}

fn stable_json(value: &Value) -> Result<String, std::fmt::Error> {
    let mut output = String::new();
    write_stable_json(&mut output, value)?;
    Ok(output)
}

fn write_stable_json(output: &mut String, value: &Value) -> Result<(), std::fmt::Error> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(boolean) => {
            if *boolean {
                output.push_str("true");
            } else {
                output.push_str("false");
            }
        }
        Value::Number(number) => output.push_str(&number.to_string()),
        Value::String(text) => write_json_string(output, text)?,
        Value::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_stable_json(output, item)?;
            }
            output.push(']');
        }
        Value::Object(map) => {
            let mut entries = map.iter().collect::<Vec<_>>();
            entries.sort_by_key(|(key, _)| *key);

            output.push('{');
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_json_string(output, key)?;
                output.push(':');
                write_stable_json(output, item)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn write_json_string(output: &mut String, value: &str) -> Result<(), std::fmt::Error> {
    use std::fmt::Write;

    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0C}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            control if control.is_control() => write!(output, "\\u{:04x}", control as u32)?,
            other => output.push(other),
        }
    }
    output.push('"');
    Ok(())
}
