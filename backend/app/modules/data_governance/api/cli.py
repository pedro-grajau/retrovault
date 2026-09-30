"""Comando privado para Eduardo operar a ingestão local."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from app.modules.catalog.domain.publication import (
    GameNotFound,
    IdempotencyConflict,
    PublicationConflict,
)
from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import (
    InvalidCorrection,
    PostgresRepository,
    ReviewConflict,
    ReviewNotFound,
)
from app.modules.data_governance.adapters.retroachievements import (
    RetroAchievementsSource,
)
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
    ra_ingest_parser = commands.add_parser("ra-ingest")
    ra_ingest_parser.add_argument("manifest", type=Path)
    review_parser = commands.add_parser("review")
    review_parser.add_argument("run_id", type=UUID)
    review_parser.add_argument("record_id")
    review_parser.add_argument("--rule-version", default="editorial-v1")
    correction_parser = commands.add_parser("correct")
    correction_parser.add_argument("run_id", type=UUID)
    correction_parser.add_argument("record_id")
    correction_parser.add_argument("--field", required=True)
    correction_parser.add_argument("--value", required=True)
    correction_parser.add_argument("--value-json", action="store_true")
    correction_parser.add_argument("--reason", required=True)
    correction_parser.add_argument("--etag", required=True)
    correction_parser.add_argument("--rule-version", default="editorial-v1")
    approval_parser = commands.add_parser("approve")
    approval_parser.add_argument("run_id", type=UUID)
    approval_parser.add_argument("record_id")
    approval_parser.add_argument("--reason", required=True)
    approval_parser.add_argument("--idempotency-key", required=True)
    approval_parser.add_argument("--etag", required=True)
    approval_parser.add_argument("--rule-version", default="editorial-v1")
    withdrawal_parser = commands.add_parser("withdraw")
    withdrawal_parser.add_argument("public_id", type=UUID)
    withdrawal_parser.add_argument("--reason", required=True)
    withdrawal_parser.add_argument("--idempotency-key", required=True)
    withdrawal_parser.add_argument("--etag", required=True)
    args = parser.parse_args(argv)
    try:
        engine = create_engine(settings.database_url)
        repository = PostgresRepository(engine)
        if args.command == "ingest":
            result = ingest(
                LocalPackage(args.package), repository, settings.app_version
            )
        elif args.command == "ra-ingest":
            result = ingest(
                RetroAchievementsSource(
                    args.manifest,
                    settings.retroachievements_api_key.get_secret_value(),
                ),
                repository,
                settings.app_version,
            )
        elif args.command == "summary":
            summary = repository.summary(args.run_id)
            if summary is None:
                sys.stdout.write(json.dumps({"code": "run_not_found"}) + "\n")
                return 1
            result = summary
        elif args.command == "process":
            result = process_run(repository, args.run_id, args.rule_version)
        elif args.command == "process-summary":
            result = repository.processing_summary(args.run_id, args.rule_version)
            if result is None:
                sys.stdout.write(json.dumps({"code": "processing_not_found"}) + "\n")
                return 1
        elif args.command == "review":
            result = repository.review_candidate(args.run_id, args.record_id, args.rule_version)
        elif args.command == "correct":
            try:
                value = json.loads(args.value) if args.value_json else args.value
            except json.JSONDecodeError as exc:
                raise InvalidCorrection("invalid_correction") from exc
            result = repository.correct_review(
                args.run_id,
                args.record_id,
                args.field,
                value,
                args.reason,
                args.etag,
                args.rule_version,
            )
        elif args.command == "approve":
            from app.modules.catalog.application.publisher import CatalogPublisher

            publisher = CatalogPublisher(engine)
            with engine.begin() as connection:
                candidate = repository.lock_review_candidate(
                    connection,
                    args.run_id,
                    args.record_id,
                    args.rule_version,
                    include_private_media=True,
                )
                result = publisher.publish(
                    candidate,
                    actor="Eduardo",
                    reason=args.reason,
                    idempotency_key=args.idempotency_key,
                    expected_etag=args.etag,
                    published_etag=publisher.current_etag(
                        str(candidate["source"]),
                        str(candidate["record_id"]),
                        connection=connection,
                    ),
                    connection=connection,
                )
        else:
            from app.modules.catalog.application.publisher import CatalogPublisher

            with engine.begin() as connection:
                result = CatalogPublisher(engine).withdraw(
                    args.public_id,
                    actor="Eduardo",
                    reason=args.reason,
                    idempotency_key=args.idempotency_key,
                    expected_etag=args.etag,
                    connection=connection,
                )
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
    except ReviewConflict:
        sys.stdout.write(json.dumps({"code": "etag_conflict"}) + "\n")
        return 2
    except PublicationConflict as error:
        known_conflicts = {
            "candidate_not_eligible",
            "candidate_not_found",
            "candidate_etag_or_cover_invalid",
            "etag_conflict",
            "review_etag_stale",
            "published_etag_stale",
            "required_fields_missing",
            "cover_invalid",
        }
        code = str(error) if str(error) in known_conflicts else "decision_conflict"
        sys.stdout.write(json.dumps({"code": code}) + "\n")
        return 2
    except IdempotencyConflict:
        sys.stdout.write(json.dumps({"code": "idempotency_key_reused"}) + "\n")
        return 2
    except GameNotFound:
        sys.stdout.write(json.dumps({"code": "published_game_not_found"}) + "\n")
        return 1
    except ReviewNotFound:
        sys.stdout.write(json.dumps({"code": "review_not_found"}) + "\n")
        return 1
    except (PackageError, OSError):
        sys.stdout.write(json.dumps({"code": "invalid_package"}) + "\n")
        return 2
    except ValueError:
        sys.stdout.write(json.dumps({"code": "invalid_request"}) + "\n")
        return 2
    except Exception:
        sys.stdout.write(json.dumps({"code": "ingestion_unavailable"}) + "\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
