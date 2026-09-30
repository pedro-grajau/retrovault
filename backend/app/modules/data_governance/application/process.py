"""Processamento privado e idempotente de uma execução preservada."""

from uuid import UUID

from app.modules.data_governance.domain.normalization import (
    RULE_FINGERPRINTS,
    RULE_VERSION,
    candidate_from_evidence,
    reconcile,
)
from app.modules.data_governance.ports.repository import ProcessingRepository


class RunNotFound(ValueError):
    pass


class UnknownRuleVersion(ValueError):
    pass


def process_run(
    repository: ProcessingRepository, run_id: UUID, rule_version: str = RULE_VERSION
) -> dict[str, object]:
    if rule_version not in RULE_FINGERPRINTS:
        raise UnknownRuleVersion("unknown_rule_version")
    with repository.processing_guard(run_id, rule_version):
        previous = repository.processing_summary(run_id, rule_version)
        if previous is not None:
            return previous
        rows = repository.processing_inputs(run_id)
        if rows is None:
            raise RunNotFound("run_not_found")
        candidates = [candidate_from_evidence(row, rule_version) for row in rows]
        peers = repository.processing_peers(run_id, rule_version)
        reconcile(candidates + peers)
        repository.save_processing(
            run_id, rule_version, RULE_FINGERPRINTS[rule_version], candidates
        )
        result = repository.processing_summary(run_id, rule_version)
        assert result is not None
        return result
