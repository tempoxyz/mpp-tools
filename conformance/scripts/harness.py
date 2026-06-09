from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, RefResolver


SCRIPT_DIR = Path(__file__).parent
CONFORMANCE_DIR = SCRIPT_DIR.parent
ADAPTERS_DIR = CONFORMANCE_DIR / "adapters"
SCHEMAS_DIR = CONFORMANCE_DIR / "schemas"
OPERATIONS_PATH = CONFORMANCE_DIR / "operations.json"

COMMAND_TO_OPERATION = {
    "parse-www-authenticate": "challenge.parse",
    "format-www-authenticate": "challenge.format",
    "parse-authorization": "credential.parse",
    "format-authorization": "credential.format",
    "parse-receipt": "receipt.parse",
    "format-receipt": "receipt.format",
    "base64url-encode": "base64url.encode",
    "base64url-decode": "base64url.decode",
    "generate-challenge-id": "challenge.id",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _schema_store() -> dict[str, Any]:
    store: dict[str, Any] = {}
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = load_json(path)
        store[path.name] = schema
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            store[schema_id] = schema
    return store


SCHEMA_STORE = _schema_store()


def validate_value(value: Any, schema: dict[str, Any] | str, label: str) -> None:
    schema_obj = SCHEMA_STORE[schema] if isinstance(schema, str) else schema
    resolver = RefResolver.from_schema(schema_obj, store=SCHEMA_STORE)
    validator = Draft202012Validator(schema_obj, resolver=resolver)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path)
        suffix = f" at {path}" if path else ""
        raise ValueError(f"{label} failed schema validation{suffix}: {error.message}")


def load_operations() -> dict[str, Any]:
    operations = load_json(OPERATIONS_PATH)
    validate_value(operations, "operation-registry.schema.json", "operations.json")
    return operations["operations"]


OPERATIONS = load_operations()


@dataclass
class AdapterConfig:
    name: str
    command: list[str]
    capabilities: list[str]
    build_command: list[str] | None = None
    cwd: Path | None = None
    env: dict[str, str] | None = None
    manifest_path: Path | None = None


def discover_adapters() -> dict[str, AdapterConfig]:
    adapters: dict[str, AdapterConfig] = {}
    for manifest_path in sorted(ADAPTERS_DIR.glob("*/adapter.json")):
        manifest = load_json(manifest_path)
        validate_value(manifest, "adapter-manifest.schema.json", str(manifest_path))
        manifest_dir = manifest_path.parent
        cwd = manifest_dir / manifest.get("cwd", ".")
        adapter = AdapterConfig(
            name=manifest["name"],
            command=manifest["command"],
            build_command=manifest.get("build"),
            cwd=cwd.resolve(),
            env=manifest.get("env"),
            capabilities=manifest["capabilities"],
            manifest_path=manifest_path,
        )
        adapters[adapter.name] = adapter
    return adapters


def build_adapter(adapter: AdapterConfig) -> str | None:
    if adapter.build_command is None:
        return None
    try:
        env = os.environ.copy()
        if adapter.env:
            env.update(adapter.env)
        result = subprocess.run(
            adapter.build_command,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=adapter.cwd,
            env=env,
        )
        if result.returncode == 0:
            return None
        return result.stderr.strip() or result.stdout.strip() or f"Build failed with exit code {result.returncode}"
    except Exception as exc:
        return str(exc)


def request_input_for_command(command: str, input_data: str) -> tuple[str, Any]:
    op = COMMAND_TO_OPERATION[command]
    if op in {"challenge.parse", "credential.parse", "receipt.parse"}:
        return op, {"header": input_data}
    if op in {"challenge.format", "credential.format", "receipt.format", "challenge.id"}:
        return op, json.loads(input_data)
    if op in {"base64url.encode", "base64url.decode"}:
        return op, {"text": input_data}
    raise ValueError(f"Unsupported command: {command}")


def legacy_response_for_operation(op: str, response: dict[str, Any]) -> dict[str, Any]:
    if response.get("ok") is False:
        error = response.get("error") or {}
        return {
            "success": False,
            "error": error.get("message", ""),
            "error_type": error.get("type", "unknown_error"),
        }

    value = response.get("value")
    result = value
    if op in {"challenge.format", "credential.format", "receipt.format"} and isinstance(value, dict):
        result = value.get("header")
    elif op in {"base64url.encode", "base64url.decode"} and isinstance(value, dict):
        result = value.get("text")
    elif op == "challenge.id" and isinstance(value, dict):
        result = value.get("id")

    return {"success": True, "result": result}


class AdapterClient:
    def __init__(self, adapter: AdapterConfig):
        self.adapter = adapter

    def call(self, op: str, input_value: Any, context: dict[str, Any] | None = None, timeout: float = 30) -> dict[str, Any]:
        if op not in self.adapter.capabilities:
            return {
                "ok": False,
                "error": {
                    "type": "unsupported_operation",
                    "message": f"{self.adapter.name} does not declare {op}",
                },
            }

        request: dict[str, Any] = {"schema": 1, "op": op, "input": input_value}
        if context:
            request["context"] = context
        validate_value(request, "adapter-request.schema.json", f"{self.adapter.name} request")

        env = os.environ.copy()
        if self.adapter.env:
            env.update(self.adapter.env)
        try:
            result = subprocess.run(
                self.adapter.command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.adapter.cwd,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": {"type": "unknown_error", "message": "Timeout"}}
        except Exception as exc:
            return {"ok": False, "error": {"type": "unknown_error", "message": str(exc)}}

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"Command failed with exit code {result.returncode}"
            return {"ok": False, "error": {"type": "unknown_error", "message": message}}

        try:
            response = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "error": {
                    "type": "unknown_error",
                    "message": f"Invalid JSON output: {exc}",
                },
            }

        validate_value(response, "adapter-response.schema.json", f"{self.adapter.name} response")
        if response.get("ok") is True:
            success_ref = OPERATIONS[op]["successRef"]
            validate_value(response["value"], {"$ref": success_ref}, f"{self.adapter.name} {op} value")
        elif response.get("error", {}).get("type") not in OPERATIONS[op]["errorTypes"]:
            error_type = response.get("error", {}).get("type")
            message = response.get("error", {}).get("message", "")
            return {
                "ok": False,
                "error": {
                    "type": "unknown_error",
                    "message": f"{self.adapter.name} {op} returned undeclared error type {error_type}: {message}",
                },
            }
        return response

    def run_legacy_command(self, command: str, input_data: str, timeout: float = 30) -> dict[str, Any]:
        op, input_value = request_input_for_command(command, input_data)
        response = self.call(op, input_value, timeout=timeout)
        return legacy_response_for_operation(op, response)
