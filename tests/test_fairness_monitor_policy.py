import json
from pathlib import Path

import pytest

from app.domain.fairness_monitor import MonitorPolicy
from app.infrastructure.fairness_monitor_policy import load_monitor_policy

POLICY = (
    Path(__file__).resolve().parents[1]
    / "policies"
    / "recommender-monitor.draft-1.json"
)


def test_loads_versioned_draft_policy():
    version, policy = load_monitor_policy(POLICY)

    assert version == "reco-monitor-draft-1"
    assert policy == MonitorPolicy(100, 30, 0.8, (-0.1, 0.1), "pool", 974 / 7621, 3, 3)


def test_loads_alternative_population_reference(tmp_path):
    document = json.loads(POLICY.read_bytes())
    document.update(version="population-test", reference="population")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    version, policy = load_monitor_policy(path)

    assert version == "population-test"
    assert policy.reference == "population"


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": ""},
        {"version": "draft 1"},
        {"window": "100"},
        {"window": True},
        {"window": 0},
        {"di_min": float("nan")},
        {"spd_range": [0.1, -0.1]},
        {"spd_range": ["-0.1", 0.1]},
        {"population_share": 1.0},
        {"reference": "invalid"},
        {"escalate_after": -1},
        {"unexpected": 1},
    ],
)
def test_rejects_invalid_policy(tmp_path, overrides):
    document = json.loads(POLICY.read_bytes())
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document | overrides), encoding="utf-8")

    with pytest.raises(ValueError, match=r"validation error|Monitor|Invalid monitor"):
        load_monitor_policy(path)


def test_rejects_missing_policy(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_monitor_policy(tmp_path / "missing.json")


def test_rejects_duplicate_policy_field(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"window": 100, "window": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate policy field: window"):
        load_monitor_policy(path)
