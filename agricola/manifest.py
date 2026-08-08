from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml import resolver
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from .models import AuditRegistry, Cursor, LedgerEntry, Manifest


class ManifestError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_manifest(path: str | Path = "sdks.yaml") -> Manifest:
    manifest_path = Path(path)
    try:
        raw = yaml.load(manifest_path.read_text(), Loader=_UniqueKeyLoader)
        return Manifest.model_validate(raw)
    except OSError as exc:
        raise ManifestError(f"cannot read {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {manifest_path}: {exc}") from exc
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors()
        )
        raise ManifestError(f"invalid manifest: {details}") from exc


def generated_schemas() -> dict[str, dict[str, object]]:
    return {
        "manifest": Manifest.model_json_schema(),
        "ledger": LedgerEntry.model_json_schema(),
        "cursor": Cursor.model_json_schema(),
        "audit": AuditRegistry.model_json_schema(),
    }


def print_schemas() -> str:
    return json.dumps(generated_schemas(), indent=2) + "\n"
