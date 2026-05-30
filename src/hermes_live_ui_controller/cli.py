"""CLI entrypoint for the live-web-ui controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapter import InMemoryBrowserAdapter
from .controller import LiveWebUIRunner, NotImplementedAdapter
from .models import RunTask


def _build_mock_adapter(path: str | None) -> InMemoryBrowserAdapter:
    if not path:
        raise SystemExit("--mock-pages is required when using in-memory mock mode")

    pages_path = Path(path)
    payload = json.loads(pages_path.read_text(encoding="utf-8"))
    return InMemoryBrowserAdapter(pages=payload, start_url=payload["start_url"])


def _build_adapter(args: argparse.Namespace):
    if args.adapter == "mock":
        return _build_mock_adapter(args.mock_pages)

    if args.adapter == "none":
        return NotImplementedAdapter()

    raise SystemExit(f"unknown adapter '{args.adapter}'")


def run_from_manifest(args: argparse.Namespace) -> int:
    task = RunTask.from_path(args.manifest)
    adapter = _build_adapter(args)

    runner = LiveWebUIRunner(
        adapter=adapter,
        auto_confirm=args.auto_confirm,
        output_dir=args.output_dir,
    )

    result = runner.run(task)

    if result.success:
        print(f"OK: {result.task_id} ({result.session_id})")
        print(f"steps={len(result.steps)} failures={result.failures}")
        return 0

    print(f"FAIL: {result.task_id} ({result.session_id})")
    for step in result.steps:
        if not step.success:
            print(f"- {step.name}: {step.error}")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live web-ui editing task manifest")
    parser.add_argument("--manifest", required=True, help="Path to run task JSON")
    parser.add_argument(
        "--adapter",
        choices=["none", "mock"],
        default="none",
        help="Execution adapter",
    )
    parser.add_argument(
        "--mock-pages",
        help="Path to in-memory page map JSON (required for --adapter mock)",
    )
    parser.add_argument(
        "--output-dir",
        help="Persist run JSON to this directory",
        default=None,
    )
    parser.add_argument(
        "--auto-confirm",
        action="store_true",
        help="Allow confirm-required actions without interactive confirmation",
    )

    args = parser.parse_args()
    return run_from_manifest(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
