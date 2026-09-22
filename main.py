"""CLI entry point.

Examples
--------
    python main.py                          # uses settings from .env
    python main.py --provider mock --clean  # offline dry-run, no API key needed
    python main.py --provider gemini --workers 2
    python main.py --data-dir data --output-dir output --log-level DEBUG
"""
from __future__ import annotations

import argparse
import logging
import sys

from complaint_processor.config import SUPPORTED_PROVIDERS, ConfigError, Settings
from complaint_processor.llm import create_llm_clients
from complaint_processor.logging_setup import setup_logging
from complaint_processor.schemas import ProcessingStatus
from complaint_processor.workflow import BatchResult, ComplaintProcessingWorkflow


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI-powered customer complaint & case processing workflow",
    )
    parser.add_argument("--data-dir", help="Folder with input documents (default: DATA_DIR or ./data)")
    parser.add_argument("--output-dir", help="Folder for outputs (default: OUTPUT_DIR or ./output)")
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, help="LLM provider (overrides LLM_PROVIDER)")
    parser.add_argument("--model", help="Model name (overrides MODEL_NAME)")
    parser.add_argument("--workers", type=int, help="Documents processed in parallel (overrides MAX_WORKERS)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Console log level")
    parser.add_argument("--clean", action="store_true", help="Delete previous outputs before running")
    parser.add_argument("--env-file", default=".env", help="Path to the .env file (default: .env)")
    return parser.parse_args(argv)


def print_summary(batch: BatchResult) -> None:
    line = "-" * 118
    print(f"\n{line}")
    print(f"{'FILE':<30} {'STATUS':<8} {'CUSTOMER':<20} {'CATEGORY':<30} {'CASE STATUS':<17} {'ESC':<4} {'WARN':<4}")
    print(line)
    for r in batch.results:
        e = r.extraction
        print(
            f"{r.file_name[:29]:<30} {r.status.value:<8} "
            f"{(e.customer_name if e else '-')[:19]:<20} "
            f"{(e.complaint_category if e else '-')[:29]:<30} "
            f"{(e.overall_case_status if e else '-'):<17} "
            f"{(e.escalation_required if e else '-'):<4} {len(r.warnings):<4}"
        )
        if r.errors:
            print(f"{'':<30} ! {r.errors[0][:84]}")
    print(line)
    print(
        f"Processed {len(batch.results)} file(s) in {batch.elapsed_seconds:.1f}s  |  "
        + "  ".join(f"{s.value}: {batch.count(s)}" for s in ProcessingStatus)
    )
    print(f"Report:      {batch.report_path}")
    print(f"Run summary: {batch.run_summary_path}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = Settings.from_env(
            env_file=args.env_file,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            llm_provider=args.provider,
            model_name=args.model,
            max_workers=args.workers,
            log_level=args.log_level,
        )
        settings.validate()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log_file = setup_logging(settings.log_level, settings.log_dir)
    logger = logging.getLogger("main")
    logger.info("Logging to %s", log_file)
    if settings.llm_provider == "mock":
        logger.warning("Using the MOCK provider (offline heuristics, not an LLM). "
                       "Use openai / azure_openai / gemini for real results.")

    try:
        extraction_llm, generation_llm = create_llm_clients(settings)
        workflow = ComplaintProcessingWorkflow(settings, extraction_llm, generation_llm)
        batch = workflow.run(clean_output=args.clean)
    except (FileNotFoundError, NotADirectoryError, ConfigError) as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130

    print_summary(batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
