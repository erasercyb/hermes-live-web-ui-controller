"""CLI entrypoint for the live-web-ui controller."""

from __future__ import annotations

import argparse

from .controller import LiveWebUIRunner
from .models import RunTask
from .runtime import AdapterConfig, build_adapter


def _build_adapter(args: argparse.Namespace, task: RunTask):
    config = AdapterConfig(
        mode=args.adapter,
        mock_pages=args.mock_pages,
        no_headless=args.no_headless,
        slow_mo_ms=args.slow_mo_ms,
    )
    return build_adapter(task, config)


def run_from_manifest(args: argparse.Namespace) -> int:
    task = RunTask.from_path(args.manifest)
    adapter = _build_adapter(args, task)

    try:
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
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live web-ui editing task manifest")
    parser.add_argument("--manifest", required=True, help="Path to run task JSON")
    parser.add_argument(
        "--adapter",
        choices=["none", "mock", "playwright"],
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
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Playwright in headed mode (when using --adapter playwright)",
    )
    parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright slow-motion delay in ms",
    )

    args = parser.parse_args()
    return run_from_manifest(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
