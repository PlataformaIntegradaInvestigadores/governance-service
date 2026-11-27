import json
from pathlib import Path

import pytest

from app.domain.fairness_monitor import (
    MonitorControl,
    MonitorPolicy,
    MonitorWindow,
    SelectionCounts,
    evaluate_window,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "fairness-monitor.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["id"] for vector in VECTORS])
def test_reference_metrics_and_transitions(vector):
    params = vector["params"]
    policy = MonitorPolicy(**{**params, "spd_range": tuple(params["spd_range"])})
    control = MonitorControl()
    transitions = []
    for index, (window, expected) in enumerate(
        zip(vector["windows"], vector["expected"]["windows"], strict=True), 1
    ):
        counts = SelectionCounts(
            window["S_p"], window["S_np"], window["C_p"], window["C_np"]
        )
        metrics = evaluate_window(counts, policy)
        assert metrics.status == expected["status"]
        assert metrics.reason == expected.get("reason")
        assert metrics.di == expected.get("DI")
        assert metrics.spd == expected.get("SPD")
        assert metrics.protected_rate == expected.get("rate_protected")
        assert metrics.reference_rate == expected.get("rate_reference")
        assert metrics.protected_interval == (
            tuple(expected["wilson95_protected"])
            if "wilson95_protected" in expected
            else None
        )
        assert metrics.reference_interval == (
            tuple(expected["wilson95_reference"])
            if "wilson95_reference" in expected
            else None
        )
        previous = control.level
        control, reason = control.observe(metrics.status, policy)
        assert control.level == expected["level_after"]
        if reason:
            transitions.append(
                {
                    "window": index,
                    "from": previous,
                    "to": control.level,
                    "reason": reason,
                }
            )
    assert transitions == vector["expected"]["transitions"]
    assert control.level == vector["expected"]["final_level"]


def test_incomplete_window_does_not_evaluate_and_full_window_resets():
    policy = MonitorPolicy(100, 30, 0.8, (-0.1, 0.1), "pool", 0.13, 3, 3)
    observation = SelectionCounts(1, 19, 5, 95)
    window = MonitorWindow()
    for _ in range(99):
        window, metrics = window.record(observation, policy)
        assert metrics is None
    assert window.requests == 99
    reset, metrics = window.record(observation, policy)
    assert reset == MonitorWindow()
    assert metrics is not None
    assert metrics.di == 1.0


@pytest.mark.parametrize("counts", [(-1, 0, 0, 0), (2, 0, 1, 0), (0, 2, 0, 1)])
def test_rejects_impossible_counts(counts):
    with pytest.raises(ValueError, match=r"counts|candidate groups"):
        SelectionCounts(*counts)


@pytest.mark.parametrize(
    "override",
    [
        {"window": 0},
        {"di_min": float("nan")},
        {"di_min": -1.0},
        {"spd_range": (0.1, -0.1)},
        {"reference": "unknown"},
        {"population_share": 1.0},
    ],
)
def test_rejects_invalid_policy(override):
    params = {**VECTORS[0]["params"], **override}
    with pytest.raises(ValueError, match=r"counts|thresholds"):
        MonitorPolicy(**params)
