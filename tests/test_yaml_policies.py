import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.qa import InvalidPolicyError, PolicyVersionConflictError
from app.infrastructure.json_policy_repository import PolicyFileRepository
from app.infrastructure.policy_files import load_qa_sampling_policy
from app.infrastructure.sqlite_assessment_repository import SqliteAssessmentRepository
from app.main import create_qa_assessor

_POLICIES = Path(__file__).resolve().parents[1] / "policies"
_QA_JSON = _POLICIES / "search-qa-review.draft-1.json"
_QA_YAML = _QA_JSON.with_suffix(".yaml")
_SAMPLE_JSON = _POLICIES / "qa-retrospective-sampling.v1.json"
_SAMPLE_YAML = _SAMPLE_JSON.with_suffix(".yaml")
_GENERATED = {
    "status": "success",
    "qa_pairs": [{"question": "Synthetic question", "answer": "Synthetic answer"}],
    "source_docs": [{"article_id": "synthetic-article"}],
}


def test_active_yaml_policy_has_the_same_values_and_hash_as_legacy_json(tmp_path):
    settings = Settings(_env_file=None)
    assert settings.qa_policy_path == _QA_YAML.relative_to(_POLICIES.parent)
    assert (
        PolicyFileRepository(_QA_YAML).load() == PolicyFileRepository(_QA_JSON).load()
    )
    database = tmp_path / "assessments.sqlite3"
    original = create_qa_assessor(_QA_JSON, database).execute(_GENERATED)
    migrated = create_qa_assessor(_QA_YAML, database).execute(_GENERATED)

    with sqlite3.connect(database) as connection:
        policies = connection.execute(
            "SELECT snapshot_json, sha256 FROM policies"
        ).fetchall()

    assert migrated.assessment == original.assessment
    assert len(policies) == 1
    snapshot, digest = policies[0]
    assert digest == hashlib.sha256(snapshot.encode()).hexdigest()
    reopened = SqliteAssessmentRepository(database)
    assert reopened.get(original.assessment_id) == original
    assert reopened.get(migrated.assessment_id) == migrated


def test_conflicting_yaml_version_preserves_existing_decision_after_reopening(tmp_path):
    database = tmp_path / "assessments.sqlite3"
    original = create_qa_assessor(_QA_JSON, database).execute(_GENERATED)
    changed = tmp_path / "changed.yml"
    changed.write_text(
        _QA_YAML.read_text(encoding="utf-8").replace("3", "4"), encoding="utf-8"
    )

    with pytest.raises(PolicyVersionConflictError, match="without a new version"):
        create_qa_assessor(changed, database).execute(_GENERATED)

    assert SqliteAssessmentRepository(database).get(original.assessment_id) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM assessments").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM policies").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM review_cases").fetchone() == (
            1,
        )


@pytest.mark.parametrize(
    "text",
    [
        "policy_id: test\nversion: v1\nversion: v2\nexpected_pairs: 3\n",
        "policy_id: test\nversion: v1\nexpected_pairs: true\n",
        "policy_id: test\nversion: v1\nexpected_pairs: '3'\n",
        "policy_id: &value test\nversion: *value\nexpected_pairs: 3\n",
        "policy_id: !unexpected test\nversion: v1\nexpected_pairs: 3\n",
        "policy_id: test\nversion: v1\nexpected_pairs: 3\n---\nother: 4\n",
        "- invalid root\n",
        "1: non-string key\n",
    ],
)
def test_yaml_rejects_invalid_or_ambiguous_documents(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(InvalidPolicyError, match="Invalid policy file"):
        PolicyFileRepository(path).load()


def test_sampling_migration_preserves_selection_and_rejects_unquoted_version(tmp_path):
    settings = Settings(_env_file=None)
    assert settings.qa_sampling_policy_path == _SAMPLE_YAML.relative_to(
        _POLICIES.parent
    )
    original = load_qa_sampling_policy(_SAMPLE_JSON)
    migrated = load_qa_sampling_policy(_SAMPLE_YAML)
    population = tuple(f"synthetic-assessment-{item}" for item in range(137))
    assert migrated == original
    assert migrated.select(population, 7) == original.select(population, 7)
    assert len(migrated.select(population, 7)) == 14

    invalid = tmp_path / "sampling.yaml"
    invalid.write_text("version: 1\npercent: 10\nminimum: 10\n", encoding="utf-8")
    with pytest.raises(InvalidPolicyError, match="Invalid sampling policy"):
        load_qa_sampling_policy(invalid)
