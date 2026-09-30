"""Comando privado para Eduardo operar a ingestão local."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.application.ingest import VersionConflict, ingest
from app.modules.data_governance.application.process import (
    RunNotFound,
    UnknownRuleVersion,
    process_run,
)
from app.modules.data_governance.ports.source import PackageError
from app.platform.config.settings import settings


def _json_default(value: object) -> str:
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    raise TypeError()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingestão privada de catálogo local")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("package", type=Path)
    summary_parser = commands.add_parser("summary")
    summary_parser.add_argument("run_id", type=UUID)
    process_parser = commands.add_parser("process")
    process_parser.add_argument("run_id", type=UUID)
    process_parser.add_argument("--rule-version", default="editorial-v1")
    process_summary_parser = commands.add_parser("process-summary")
    process_summary_parser.add_argument("run_id", type=UUID)
    process_summary_parser.add_argument("--rule-version", default="editorial-v1")
    args = parser.parse_args(argv)
    try:
        engine = create_engine(settings.database_url)
        repository = PostgresRepository(engine)
        if args.command == "ingest":
            result = ingest(
                LocalPackage(args.package), repository, settings.app_version
            )
        elif args.command == "summary":
            summary = repository.summary(args.run_id)
            if summary is None:
                sys.stdout.write(json.dumps({"code": "run_not_found"}) + "\n")
                return 1
            result = summary
        elif args.command == "process":
            result = process_run(repository, args.run_id, args.rule_version)
        else:
            result = repository.processing_summary(args.run_id, args.rule_version)
            if result is None:
                sys.stdout.write(json.dumps({"code": "processing_not_found"}) + "\n")
                return 1
        sys.stdout.write(
            json.dumps(
                result, default=_json_default, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        )
        return 0
    except VersionConflict:
        sys.stdout.write(json.dumps({"code": "source_version_conflict"}) + "\n")
        return 2
    except RunNotFound:
        sys.stdout.write(json.dumps({"code": "run_not_found"}) + "\n")
        return 1
    except UnknownRuleVersion:
        sys.stdout.write(json.dumps({"code": "unknown_rule_version"}) + "\n")
        return 2
    except PackageError, OSError, ValueError:
        sys.stdout.write(json.dumps({"code": "invalid_package"}) + "\n")
        return 2
    except Exception:
        sys.stdout.write(json.dumps({"code": "ingestion_unavailable"}) + "\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
