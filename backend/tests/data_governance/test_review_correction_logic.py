import json
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.data_governance.adapters.postgres_repository import (
    InvalidCorrection,
    PostgresRepository,
    ReviewConflict,
)


class Result:
    def __init__(self, scalar=None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar


class Connection:
    def __init__(self, evidence_id) -> None:
        self.evidence_id = evidence_id
        self.correction = None

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split()).lower()
        params = dict(parameters or {})
        if "select evidence_id from data_governance.staging_records" in normalized:
            return Result(self.evidence_id)
        if "select coalesce(max(revision)" in normalized:
            return Result(1)
        if "insert into data_governance.review_corrections" in normalized:
            self.correction = params
        return Result()


class Engine:
    def __init__(self, evidence_id) -> None:
        self.connection = Connection(evidence_id)
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def begin(self):
        try:
            yield self.connection
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


class ReviewRepository(PostgresRepository):
    def __init__(self, engine, candidate) -> None:
        super().__init__(engine)
        self.candidate = candidate

    def lock_review_candidate(
        self,
        connection,
        run_id,
        record_id,
        rule_version="editorial-v1",
        *,
        include_private_media=False,
    ):
        return self.candidate

    def _review_candidate(self, connection, run_id, record_id, rule_version):
        correction = connection.correction
        field = correction["field"]
        value = json.loads(correction["value"])
        revised = {
            **self.candidate,
            "values": {**self.candidate["values"], field: value},
            "etag": correction["resulting_etag"],
            "lineage": {
                **self.candidate["lineage"],
                field: {
                    "source": "human-review",
                    "actor": correction["actor"],
                    "reason": correction["reason"],
                    "previous_etag": correction["previous_etag"],
                    "resulting_etag": correction["resulting_etag"],
                },
            },
            "corrections": [
                *self.candidate["corrections"],
                {
                    "id": correction["id"],
                    "field": field,
                    "value": value,
                    "previous_value": json.loads(correction["previous_value"]),
                },
            ],
        }
        return revised


def _candidate(run_id, evidence_id):
    return {
        "run_id": run_id,
        "rule_version": "editorial-v1",
        "evidence_id": evidence_id,
        "etag": '"etag-current"',
        "values": {"title": "Título original"},
        "lineage": {"title": {"source": "retroachievements"}},
        "corrections": [],
    }


def test_human_correction_appends_previous_value_reason_and_etags() -> None:
    run_id, evidence_id = uuid4(), uuid4()
    engine = Engine(evidence_id)
    repository = ReviewRepository(engine, _candidate(run_id, evidence_id))

    corrected = repository.correct_review(
        run_id,
        "42",
        "title",
        "Título corrigido",
        "Corrigir erro editorial",
        '"etag-current"',
    )

    params = engine.connection.correction
    assert params is not None
    assert json.loads(params["previous_value"]) == "Título original"
    assert json.loads(params["value"]) == "Título corrigido"
    assert params["actor"] == "Eduardo"
    assert params["reason"] == "Corrigir erro editorial"
    assert params["previous_etag"] == '"etag-current"'
    assert params["resulting_etag"] == corrected["etag"]
    assert corrected["values"]["title"] == "Título corrigido"
    assert corrected["lineage"]["title"]["source"] == "human-review"
    assert corrected["corrections"][0]["previous_value"] == "Título original"
    assert engine.committed is True


def test_stale_correction_etag_conflicts_without_appending() -> None:
    run_id, evidence_id = uuid4(), uuid4()
    engine = Engine(evidence_id)
    repository = ReviewRepository(engine, _candidate(run_id, evidence_id))

    with pytest.raises(ReviewConflict, match="etag_conflict"):
        repository.correct_review(
            run_id,
            "42",
            "title",
            "Outro título",
            "Tentativa obsoleta",
            '"old-etag"',
        )

    assert engine.connection.correction is None
    assert engine.rolled_back is True


def test_correction_rejects_commercial_fields_before_persistence() -> None:
    repository = PostgresRepository(engine=None)

    with pytest.raises(InvalidCorrection, match="invalid_correction"):
        repository.correct_review(
            uuid4(),
            "42",
            "price",
            "99.90",
            "Campo comercial proibido",
            '"etag-current"',
        )


@pytest.mark.parametrize(
    "items",
    [["item"] * 101, ["x" * 2501, "y" * 2500]],
)
def test_included_items_correction_has_count_and_aggregate_text_bounds(items) -> None:
    repository = PostgresRepository(engine=None)

    with pytest.raises(InvalidCorrection, match="invalid_correction"):
        repository.correct_review(
            uuid4(),
            "42",
            "included_items",
            items,
            "Valor excessivo",
            '"etag-current"',
        )


class CandidateResult:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class RevalidatedCandidateConnection:
    def __init__(self, run_id, evidence_id, other_issue=None) -> None:
        output = BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="PNG")
        self.rows = {
            "from data_governance.staging_records sr": [
                {
                    "evidence_id": evidence_id,
                    "state": "quarantine",
                    "ambiguous_identity": False,
                    "source": "retroachievements",
                    "source_record_id": "42",
                    "payload_hash": "f" * 64,
                    "captured_at": datetime(2026, 9, 30, tzinfo=UTC),
                    "source_version": "portfolio-v1",
                    "app_version": "test",
                }
            ],
            "from data_governance.staging_values": [
                {"attribute_path": "platform", "normalized_value": "SNES"}
            ],
            "from data_governance.review_corrections": [
                {
                    "id": uuid4(),
                    "field": "title",
                    "value": "Título corrigido",
                    "previous_value": None,
                    "revision": 1,
                    "actor": "Eduardo",
                    "reason": "Completar campo obrigatório",
                    "previous_etag": '"old"',
                    "resulting_etag": '"new"',
                    "corrected_at": datetime(2026, 9, 30, tzinfo=UTC),
                }
            ],
            "from data_governance.media_rights mr": [
                {
                    "media_path": "cover.png",
                    "role": "box_art",
                    "storage_right": "confirmed",
                    "publication_right": "confirmed",
                    "attribution": "RetroAchievements",
                    "eligible": True,
                    "content_hash": "a" * 64,
                    "content": output.getvalue(),
                }
            ],
            "from data_governance.attribute_origins ao": [
                {
                    "attribute_path": "platform",
                    "source": "retroachievements",
                    "source_version": "portfolio-v1",
                    "source_record_id": "42",
                    "actor": "Eduardo",
                    "captured_at": datetime(2026, 9, 30, tzinfo=UTC),
                }
            ],
            "from data_governance.quarantine_issues": [
                {
                    "field": "title",
                    "code": "required_invalid",
                    "cause": "Campo obrigatório ausente ou vazio.",
                },
                *([other_issue] if other_issue else []),
            ],
        }

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split()).lower()
        for marker, rows in self.rows.items():
            if marker in normalized:
                return CandidateResult(rows)
        raise AssertionError(f"Unexpected review query: {normalized}")


@pytest.mark.parametrize(
    ("other_issue", "expected_state"),
    [
        (None, "review"),
        (
            {"field": "identity", "code": "ambiguous_match", "cause": "Match pendente."},
            "quarantine",
        ),
    ],
)
def test_correction_revalidation_resolves_required_field_but_keeps_other_issue(
    other_issue, expected_state
) -> None:
    run_id, evidence_id = uuid4(), uuid4()
    connection = RevalidatedCandidateConnection(run_id, evidence_id, other_issue)

    candidate = PostgresRepository(engine=None)._review_candidate(
        connection, run_id, "42", "editorial-v1"
    )

    assert candidate["values"]["title"] == "Título corrigido"
    assert not any(
        issue["field"] == "title" and issue["code"] == "required_invalid"
        for issue in candidate["issues"]
    )
    assert candidate["state"] == expected_state
    if other_issue:
        assert other_issue in candidate["issues"]
