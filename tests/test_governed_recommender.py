import copy
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "tests/fixtures/recommender-reference.json").read_text(encoding="utf-8")
)["cases"]
URL = "http://recommender.test/api/v1/recommend"
GEN = "/api/v1/governed/recommender/recommend"
REVIEW = "/api/v1/review-cases"


def monitor_policy(client, db, **overrides):
    policy = json.loads(
        (ROOT / "policies/recommender-monitor.draft-1.json").read_bytes()
    )
    policy.update(window=1, min_selected_per_group=2)
    policy.update(overrides)
    path = db.with_suffix(".json")
    path.write_text(json.dumps(policy), encoding="utf-8")
    client.app.state.settings.recommender_monitor_policy_path = path
    return path


def monitored_response(pool_share):
    response = copy.deepcopy(CASES[0]["upstream_response"])
    response["fairness_metrics"]["pool_low_visibility_share"] = pool_share
    return response


def monitor_rows(db):
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(
            "SELECT window_json, control_json, completed_json "
            "FROM recommender_monitor_observations ORDER BY sequence"
        ).fetchall()


@pytest.fixture
def system(tmp_path):
    database = tmp_path / "test.sqlite"
    key = "a" * 32
    settings = Settings(
        database=database,
        recommender_upstream_base_url="http://recommender.test",
        recommender_hmac_key=key,
        recommender_policy_path=ROOT / "policies/recommender-fairness.draft-1.json",
        admin_verifier_base_url="http://search.test",
    )
    with respx.mock as mock, TestClient(create_app(settings)) as client:
        verifier = mock.get("http://search.test/internal/auth/validate-admin-token/")
        yield client, mock.post(URL), verifier, database, key


def test_monitor_escalates_preserves_streak_and_recovers_through_http(system):
    client, upstream, _, db, _ = system
    monitor_policy(client, db)
    upstream.respond(200, json=monitored_response(0.2))
    assert client.post(GEN, json={}).status_code == 200
    upstream.respond(200, json=monitored_response(0.8))
    assert [client.post(GEN, json={}).status_code for _ in range(2)] == [200, 200]
    upstream.respond(200, json=CASES[1]["upstream_response"])
    assert client.post(GEN, json={}).status_code == 200
    upstream.respond(200, json=monitored_response(0.8))
    held = client.post(GEN, json={})
    assert held.status_code == 202
    assert set(held.json()) == {"review_id", "review_token"}
    upstream.respond(200, json=monitored_response(0.2))
    assert [client.post(GEN, json={}).status_code for _ in range(4)] == [
        202,
        202,
        202,
        200,
    ]

    completed = [json.loads(row[2]) for row in monitor_rows(db)]
    assert completed[3]["metrics"]["status"] == "not_evaluable"
    assert completed[3]["transition"] is None
    assert completed[4]["transition"] == "out_of_threshold"
    assert completed[4]["from"] == "automatic"
    assert completed[4]["to"] == "mandatory_review"
    assert completed[7]["transition"] == "back_within_threshold"
    assert completed[7]["from"] == "mandatory_review"
    assert completed[7]["to"] == "automatic"
    with closing(sqlite3.connect(db)) as connection:
        saved = connection.execute(
            """
            SELECT a.reason_codes_json, ra.metadata_json FROM assessments a
            JOIN review_cases r USING (assessment_id)
            JOIN recommender_audit ra USING (assessment_id) WHERE r.case_id = ?
            """,
            (held.json()["review_id"],),
        ).fetchone()
    assert json.loads(saved[0]) == ["FAIRNESS_MONITOR_REVIEW"]
    assert json.loads(saved[1])["monitor_sequence"] == 5


@pytest.mark.parametrize(
    ("variant", "status", "reason"),
    [
        ("baseline", 200, "BASELINE_WITHOUT_FAIRNESS"),
        ("disabled", 202, "FAIRNESS_DISABLED"),
        ("weakened", 202, "FAIRNESS_WEAKENED"),
    ],
)
def test_active_policy_only_permits_exact_baseline(system, variant, status, reason):
    client, upstream, _, db, _ = system
    monitor_policy(client, db)
    client.app.state.settings.recommender_policy_path = (
        ROOT / Settings().recommender_policy_path
    )
    case = CASES[0] if variant == "weakened" else CASES[2]
    doc = copy.deepcopy(case["upstream_response"])
    rerank = doc["parameters"]["rerank"]
    if variant == "disabled":
        rerank["max_per_institution"] = 3
    if variant == "weakened":
        rerank["fairness_mode"] = "fair"
    upstream.respond(200, json=doc)

    response = client.post(GEN, json=case["request"])

    assert response.status_code == status
    if variant == "baseline":
        assert response.json() == doc
        assert reason in response.headers["X-Governance-Notices"].split(",")
    else:
        assert set(response.json()) == {"review_id", "review_token"}
    assert not monitor_rows(db)
    with closing(sqlite3.connect(db)) as connection:
        (version,) = connection.execute("SELECT version FROM policies").fetchone()
        (reasons,) = connection.execute(
            "SELECT reason_codes_json FROM assessments"
        ).fetchone()
    assert version == "reco-draft-3"
    assert reason in json.loads(reasons)


def test_monitor_excludes_ineligible_responses(system):
    client, upstream, _, db, _ = system
    monitor_policy(client, db)
    scenarios = {"RR03": 202, "RR04": 202, "RR06": 404, "RR09": 403}
    for case in CASES:
        if case["id"] in scenarios:
            upstream.respond(case["upstream_status"], json=case["upstream_response"])
            assert client.post(GEN, json={}).status_code == scenarios[case["id"]]
    assert not monitor_rows(db)


def test_monitor_reopens_partial_window_and_serializes_parallel_requests(system):
    client, upstream, _, db, _ = system
    monitor_policy(client, db, window=4)
    upstream.respond(200, json=monitored_response(0.2))
    assert client.post(GEN, json={}).status_code == 200
    settings = client.app.state.settings
    with TestClient(create_app(settings)) as restarted, ThreadPoolExecutor(3) as pool:
        responses = list(pool.map(lambda _: restarted.post(GEN, json={}), range(3)))
    assert [response.status_code for response in responses] == [200] * 3
    rows = monitor_rows(db)
    assert [json.loads(row[0])["requests"] for row in rows] == [1, 2, 3, 0]
    assert [row[2] is not None for row in rows] == [False, False, False, True]
    assert json.loads(rows[-1][2])["counts"] == {
        "selected_protected": 16,
        "selected_reference": 64,
        "candidates_protected": 80,
        "candidates_reference": 320,
    }


def test_monitor_rolls_back_when_assessment_write_fails(system):
    client, upstream, _, db, _ = system
    monitor_policy(client, db, escalate_after=1)
    upstream.respond(200, json=monitored_response(0.8))
    with closing(sqlite3.connect(db)) as connection:
        connection.executescript(
            """CREATE TRIGGER reject_audit BEFORE INSERT ON recommender_audit
               BEGIN SELECT RAISE(ABORT, 'test failure'); END;"""
        )
    assert client.post(GEN, json={}).status_code == 403
    assert not monitor_rows(db)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM assessments").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM review_cases").fetchone() == (
            0,
        )
        assert connection.execute("SELECT count(*) FROM policies").fetchone() == (0,)


def test_monitor_config_cannot_change_without_new_version(system):
    client, upstream, _, db, _ = system
    path = monitor_policy(client, db, escalate_after=1)
    upstream.respond(200, json=monitored_response(0.8))
    assert client.post(GEN, json={}).status_code == 202
    monitor_policy(client, db, window=2)
    assert client.post(GEN, json={}).status_code == 403
    assert len(monitor_rows(db)) == 1
    monitor_policy(client, db, window=2, version="monitor-2")
    assert client.post(GEN, json={}).status_code == 202
    assert json.loads(monitor_rows(db)[1][0])["requests"] == 1
    path.unlink()
    assert client.post(GEN, json={}).status_code == 403
    assert len(monitor_rows(db)) == 2


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_reference_through_http_and_review(system, case, caplog):
    client, upstream, verifier, db, key = system
    status = case["upstream_status"]
    response = case["upstream_response"]
    upstream.respond(status, json=response)
    actual = client.post(GEN, json=case["request"])
    exp = case["expected"]
    assert (
        actual.status_code
        == {"permit": 200, "retain": 202, "deny": 403, "client_error": status}[
            exp["verdict"]
        ]
    )
    assert actual.headers["cache-control"] == "no-store"
    with closing(sqlite3.connect(db)) as con:
        (metadata,) = con.execute(
            "SELECT metadata_json FROM recommender_audit"
        ).fetchone()
        (reasons,) = con.execute("SELECT reason_codes_json FROM assessments").fetchone()
    if status == 404:
        assert actual.json() == response
        (unknown,) = response["detail"]["unknown_author_ids"]
        assert json.loads(metadata)["unknown_author_hmac"] == [
            hmac.digest(
                key.encode(), b"recommender-author:" + unknown.encode(), "sha256"
            ).hex()
        ]
        assert unknown not in metadata + caplog.text
    else:
        assert json.loads(reasons) == exp["reason_codes"]
        for p in response["team"] + response["recommendations"]:
            assert p["author_id"] not in metadata + caplog.text
            if p["name"]:
                assert p["name"] not in metadata + caplog.text
    if exp["verdict"] != "retain":
        return

    assert set(actual.json()) == {"review_id", "review_token"}
    review_id = actual.json()["review_id"]
    case_url = f"{REVIEW}/{review_id}"
    poll = f"/api/v1/governed/recommender/reviews/{review_id}"
    requester = {"Authorization": f"Bearer {actual.json()['review_token']}"}
    admin = {"X-Admin-Token": "test-admin"}
    assert client.get(poll).status_code == 403
    assert client.get(poll, headers=requester).status_code == 202
    assert client.get(case_url).status_code == 401
    verifier.respond(403)
    assert client.get(case_url, headers=admin).status_code == 403
    verifier.respond(503)
    assert client.get(case_url, headers=admin).status_code == 503
    verifier.respond(204)
    view = client.get(case_url, headers=admin)
    assert view.status_code == 200
    assert view.json()["reason_codes"] == exp["reason_codes"]
    assert "name" not in view.json()["recommendation"]["recommendations"][0]
    edit = {"outcome": exp["decision"]}
    if exp["decision"] == "modify":
        edit["retained_ranks"] = [
            p["rank"]
            for p in response["recommendations"]
            if p["rank"] not in exp["removed_ranks"]
        ]
        invalid = {**edit, "retained_ranks": list(reversed(edit["retained_ranks"]))}
        assert (
            client.post(
                f"{case_url}/resolution", headers=admin, json=invalid
            ).status_code
            == 422
        )
    assert (
        client.post(f"{case_url}/resolution", headers=admin, json=edit).status_code
        == 422
    )
    edit["justification"] = "Synthetic policy review"
    assert (
        client.post(f"{case_url}/resolution", headers=admin, json=edit).status_code
        == 201
    )
    assert (
        client.post(f"{case_url}/resolution", headers=admin, json=edit).status_code
        == 200
    )
    released = client.get(poll, headers=requester)
    assert released.status_code == (403 if exp["decision"] == "reject" else 200)
    if exp["decision"] == "modify":
        assert [p["rank"] for p in released.json()["recommendations"]] == edit[
            "retained_ranks"
        ]
        assert released.json()["governance"]["jaccard_divergence"] == 0.05


@pytest.mark.parametrize(
    "body",
    [
        {"detail": [{"input": "sensitive-422", "msg": "Invalid"}]},
        {"detail": "sensitive-422"},
    ],
)
def test_422_is_forwarded_but_body_not_audited(system, body, caplog):
    client, upstream, _, db, _ = system
    upstream.respond(422, json=body)
    result = client.post(GEN, json={})
    assert result.status_code == 422
    assert result.json() == body
    with closing(sqlite3.connect(db)) as con:
        (audit,) = con.execute("SELECT metadata_json FROM recommender_audit").fetchone()
    assert "sensitive-422" not in audit + caplog.text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_upstream_network_failure_is_denied(system, failure):
    client, upstream, _, _, _ = system
    upstream.mock(side_effect=failure)
    assert client.post(GEN, json={}).status_code == 403


def test_invalid_contract_is_denied(system):
    client, upstream, _, db, _ = system
    upstream.respond(200, json={"recommendations": []})
    assert client.post(GEN, json={}).status_code == 403
    with closing(sqlite3.connect(db)) as con:
        (reasons,) = con.execute("SELECT reason_codes_json FROM assessments").fetchone()
    assert json.loads(reasons) == ["UPSTREAM_CONTRACT_INVALID"]


def test_unsupported_upstream_status_is_denied(system):
    client, upstream, _, _, _ = system
    upstream.respond(500, json={"detail": "synthetic private detail"})
    result = client.post(GEN, json={})
    assert result.status_code == 403
    assert "synthetic private detail" not in result.text


def test_accepts_empty_list_with_empty_metrics(system):
    client, upstream, _, _, _ = system
    doc = copy.deepcopy(CASES[0]["upstream_response"])
    doc["fairness_metrics"] = {}
    doc["recommendations"] = []
    doc["diagnostics"] = dict.fromkeys(doc["diagnostics"], 0)
    upstream.respond(200, json=doc)
    assert client.post(GEN, json=CASES[0]["request"]).json() == doc


def test_no_configuration_denies_recommendations():
    with TestClient(create_app(Settings())) as client:
        assert client.post(GEN, json={}).status_code == 403


def test_recommender_configuration_requires_database_and_key():
    with pytest.raises(ValueError, match="require a database and HMAC key"):
        Settings(recommender_upstream_base_url="http://recommender.test")


def test_ranks_cannot_be_used_to_modify_a_qa_case(system, monkeypatch):
    from types import SimpleNamespace

    from app.application.resolve_review import ReviewCases
    from app.domain.resolution import (
        InvalidRecommendationResolutionError,
        ReviewOutcome,
    )
    from app.domain.review import RetainedQaContent, RetainedQaPair
    from app.domain.supervision import Supervisor

    client, _, _, _, _ = system
    reviews = client.app.state.review_cases
    assert isinstance(reviews, ReviewCases)
    content = RetainedQaContent((RetainedQaPair("Question", "Answer"),), ())
    monkeypatch.setattr(reviews, "_case", lambda _: SimpleNamespace(content=content))
    with pytest.raises(InvalidRecommendationResolutionError):
        reviews.resolve(
            "case",
            Supervisor("admin", "Admin"),
            ReviewOutcome.MODIFY,
            justification="Synthetic",
            retained_ranks=(1,),
        )


def test_concentration_review_keeps_best_ranked_profiles(system):
    client, upstream, verifier, _, _ = system
    doc = copy.deepcopy(CASES[0]["upstream_response"])
    ordinary = [p for p in doc["recommendations"] if not p["low_visibility"]]
    for profile in ordinary[:4]:
        profile["institution"] = "synthetic shared institution"
    upstream.respond(200, json=doc)
    response = client.post(GEN, json=CASES[0]["request"])
    assert response.status_code == 202
    verifier.respond(204)
    dropped = ordinary[3]["rank"]
    ranks = [p["rank"] for p in doc["recommendations"] if p["rank"] != dropped]
    result = client.post(
        f"{REVIEW}/{response.json()['review_id']}/resolution",
        headers={"X-Admin-Token": "test-admin"},
        json={
            "outcome": "modify",
            "retained_ranks": ranks,
            "justification": "Synthetic",
        },
    )
    assert result.status_code == 201


def test_concentration_correction_rejects_when_remaining_list_misses_quota(system):
    client, upstream, verifier, _, _ = system
    doc = copy.deepcopy(CASES[0]["upstream_response"])
    protected = [p for p in doc["recommendations"] if p["low_visibility"]]
    for profile in protected:
        profile["institution"] = "synthetic shared institution"
    ordinary = [p for p in doc["recommendations"] if not p["low_visibility"]]
    ordinary[0]["institution"] = "synthetic shared institution"
    doc["parameters"]["rerank"]["min_protected_share"] = 0.24
    upstream.respond(200, json=doc)
    response = client.post(GEN, json=CASES[0]["request"])
    assert response.status_code == 202
    verifier.respond(204)
    result = client.post(
        f"{REVIEW}/{response.json()['review_id']}/resolution",
        headers={"X-Admin-Token": "test-admin"},
        json={"outcome": "reject", "justification": "Synthetic quota failure"},
    )
    assert result.status_code == 201


def test_policy_missing_fails_before_contacting_upstream(system, tmp_path):
    client, upstream, _, _, _ = system
    client.app.state.settings.recommender_policy_path = tmp_path / "missing.json"
    assert client.post(GEN, json={}).status_code == 403
    assert not upstream.called


def test_audit_failure_rolls_back_case_and_assessment(system):
    client, upstream, _, db, _ = system
    upstream.respond(200, json=CASES[2]["upstream_response"])
    with closing(sqlite3.connect(db)) as con:
        con.execute(
            "CREATE TRIGGER fail_recommender_audit BEFORE INSERT ON recommender_audit "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    assert client.post(GEN, json=CASES[2]["request"]).status_code == 403
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT * FROM review_cases").fetchall() == []
        assert con.execute("SELECT * FROM assessments").fetchall() == []


@pytest.mark.parametrize(
    "mutation", ["rank", "share", "concentration", "protected_attribute"]
)
def test_additional_policy_branches(system, mutation):
    client, upstream, _, db, _ = system
    doc = copy.deepcopy(CASES[0]["upstream_response"])
    if mutation == "rank":
        doc["recommendations"][0]["rank"] = 2
    elif mutation == "share":
        doc["fairness_metrics"]["low_visibility_share"] = 0.99
    elif mutation == "protected_attribute":
        del doc["recommendations"][0]["low_visibility"]
    else:
        for profile in doc["recommendations"]:
            profile["institution"] = "same synthetic institution"
        doc["diagnostics"]["cap_relaxations"] = 0
    upstream.respond(200, json=doc)
    response = client.post(GEN, json=CASES[0]["request"])
    assert response.status_code == (202 if mutation == "concentration" else 403)
    with closing(sqlite3.connect(db)) as con:
        (reasons,) = con.execute("SELECT reason_codes_json FROM assessments").fetchone()
    assert json.loads(reasons) == [
        {
            "rank": "UPSTREAM_INCONSISTENT",
            "share": "UPSTREAM_INCONSISTENT",
            "concentration": "INSTITUTION_CONCENTRATION",
            "protected_attribute": "UPSTREAM_CONTRACT_INVALID",
        }[mutation]
    ]
