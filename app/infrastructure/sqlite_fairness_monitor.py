import json
import sqlite3
from dataclasses import asdict

from app.domain.fairness_monitor import (
    MonitorControl,
    MonitorPolicy,
    MonitorWindow,
    SelectionCounts,
)

MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommender_monitor_observations (
    sequence INTEGER PRIMARY KEY,
    monitor_version TEXT NOT NULL,
    recommender_version TEXT NOT NULL,
    window_json TEXT NOT NULL,
    control_json TEXT NOT NULL,
    completed_json TEXT
);
CREATE TRIGGER IF NOT EXISTS monitor_observations_no_update
BEFORE UPDATE ON recommender_monitor_observations
BEGIN SELECT RAISE(ABORT, 'Monitor observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS monitor_observations_no_delete
BEFORE DELETE ON recommender_monitor_observations
BEGIN SELECT RAISE(ABORT, 'Monitor observations are append-only'); END;
"""


def observe_recommendation(
    connection: sqlite3.Connection,
    version: str,
    policy: MonitorPolicy,
    recommender_version: str,
    counts: SelectionCounts,
) -> tuple[bool, int]:
    prior = connection.execute(
        "SELECT monitor_version, recommender_version, window_json, control_json "
        "FROM recommender_monitor_observations ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    window, control = MonitorWindow(), MonitorControl()
    if prior is not None:
        previous_control = json.loads(prior[3])
        control = MonitorControl(previous_control["level"])
        if prior[:2] == (version, recommender_version):
            state = json.loads(prior[2])
            window = MonitorWindow(
                state["requests"], SelectionCounts(**state["counts"])
            )
            control = MonitorControl(**previous_control)
    window_after, metrics = window.record(counts, policy)
    control_after = control
    completed = None
    if metrics is not None:
        control_after, reason = control.observe(metrics.status, policy)
        completed = json.dumps(
            {
                "counts": asdict(window.counts.add(counts)),
                "metrics": asdict(metrics),
                "from": control.level,
                "to": control_after.level,
                "transition": reason,
            }
        )
    (sequence,) = connection.execute(
        "INSERT INTO recommender_monitor_observations "
        "(monitor_version, recommender_version, window_json, control_json, "
        "completed_json) VALUES (?, ?, ?, ?, ?) RETURNING sequence",
        (
            version,
            recommender_version,
            json.dumps(asdict(window_after)),
            json.dumps(asdict(control_after)),
            completed,
        ),
    ).fetchone()
    return "mandatory_review" in (control.level, control_after.level), sequence
