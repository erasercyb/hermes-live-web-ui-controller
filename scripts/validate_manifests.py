"""CLI helper to validate example task manifests against JSON Schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA_PATH = Path("schemas") / "run_task_schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _candidate_manifests() -> list[Path]:
    examples_dir = Path("examples")
    manifests = sorted(examples_dir.glob("*.json"))
    return [path for path in manifests if path.name != "pages_mock.json"]


def validate_manifest(path: Path, validator: Draft202012Validator) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: item.path):
        location = ".".join(str(segment) for segment in error.path) or "<root>"
        errors.append(f"{path}: {location}: {error.message}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate task manifests against schema")
    parser.add_argument(
        "manifests",
        nargs="*",
        default=[*_candidate_manifests()],
        help="Manifest files to validate. Defaults to all example manifests in examples/*.json",
    )
    args = parser.parse_args()

    schema = _load_schema()
    validator = Draft202012Validator(schema)

    all_errors: list[str] = []
    for manifest_path in map(Path, args.manifests):
        if not manifest_path.exists():
            all_errors.append(f"{manifest_path}: file not found")
            continue
        all_errors.extend(validate_manifest(manifest_path, validator))

    if all_errors:
        for error in all_errors:
            print(error)
        print(f"Validation failed: {len(all_errors)} issue(s)")
        return 1

    print(f"Validated {len(args.manifests)} manifest(s) successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
