"""Customer read gaps: real public CLI routing with synthetic Core responses."""

import copy
import json

import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.commands import alerts, budget, plan, account
from solwyn_cli.main import app
from test_alerts_command import EVENT, WINDOW
from test_budget_set_command import FakeClient
from test_plan_command import BILLING_STATUS

runner = CliRunner()
JWT = auth.JwtCredential(
    access_token="synthetic-access", refresh_token="synthetic-refresh"
)
KEY = auth.ProjectKeyCredential(api_key="sk_proj_" + "a1" * 32, source="environment")


@pytest.fixture(autouse=True)
def reads(monkeypatch):
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(
        FakeClient,
        "current_credential",
        property(lambda self: self.credential),
        raising=False,
    )
    for module in (alerts, budget, plan, account):
        monkeypatch.setattr(module, "SolwynClient", FakeClient)
        monkeypatch.setattr(module, "resolve_credential", lambda: JWT)


def invoke(*args):
    return runner.invoke(app, ["--json", "--project", "proj_alpha", *args])


def test_activity_cross_project_empty_continued_page_and_filters():
    event = copy.deepcopy(EVENT)
    event.update(project_id="proj_other", event_type="fired")
    FakeClient.responses = [
        httpx.Response(200, json=dict(events=[], next_cursor="next", window=WINDOW)),
        httpx.Response(200, json=dict(events=[event], next_cursor=None, window=WINDOW)),
    ]
    result = invoke(
        "alerts", "activity", "--range", "7d", "--event-type", "fired", "--all"
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["events"][0]["project_id"] == "proj_other"
    assert data["next_cursor"] is None
    requests = FakeClient.instances[0].requests
    assert requests[0] == (
        "GET",
        "/api/v1/account/alerts/activity",
        {"params": {"range": "7d", "event_type": "fired"}},
    )
    assert requests[1][2]["params"]["cursor"] == "next"


@pytest.mark.parametrize("fail,exit_code", [(False, 0), (True, 1)])
def test_pending_data_first_health_exit(fail, exit_code):
    event = copy.deepcopy(EVENT)
    event["delivery"].update(status="retrying", next_attempt_at="2026-10-14T01:00:00Z")
    event["delivery"]["legs"][0]["status"] = "pending"
    FakeClient.responses = [
        httpx.Response(200, json=dict(events=[event], next_cursor=None))
    ]
    result = invoke("alerts", "pending", *(["--fail-if-open"] if fail else []))
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["events"][0]["delivery"]["status"] == "retrying"
    if fail:
        assert json.loads(result.stderr)["code"] == "open_alert_deliveries"


@pytest.mark.parametrize("calls", [0, 12])
def test_uncounted_is_separate_advisory_read(calls):
    FakeClient.responses = [
        httpx.Response(
            200,
            json=dict(
                project_id="proj_alpha",
                uncounted_calls=calls,
                uncounted_tokens=42,
                last_reported_at=None,
            ),
        )
    ]
    result = invoke("budget", "uncounted")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["uncounted_calls"] == calls
    assert "unpriced upper bound" in result.stderr
    assert (
        FakeClient.instances[0].requests[0][1]
        == "/api/v1/projects/proj_alpha/budget/uncounted"
    )


def catalog():
    return dict(
        currency="usd",
        tax_behavior="exclusive",
        tax_code="txcd_synthetic",
        tiers={
            "team": dict(
                tier="team",
                display_name="Team",
                description="Synthetic",
                most_popular=False,
                prices=dict(month=dict(amount_cents=9900, currency="usd"), year=None),
                overage=dict(
                    included_requests=500000,
                    published_rate="$0.01",
                    unit_amount_decimal_cents="1",
                ),
                limits=BILLING_STATUS["limits"],
                capabilities=BILLING_STATUS["capabilities"],
            )
        },
    )


@pytest.mark.parametrize("credential", [JWT, KEY])
def test_plan_list_accepts_both_auths_preserves_units(monkeypatch, credential):
    monkeypatch.setattr(plan, "resolve_credential", lambda: credential)
    FakeClient.responses = [httpx.Response(200, json=catalog())]
    result = invoke("plan", "list")
    assert result.exit_code == 0, result.output
    assert {
        k: v for k, v in json.loads(result.stdout).items() if k != "schema"
    } == catalog()
    assert FakeClient.instances[0].requests == [("GET", "/api/v1/billing/catalog", {})]


@pytest.mark.parametrize(
    "args",
    [
        ("plan", "--help"),
        ("plan", "list", "--help"),
        ("account", "phone", "show", "--help"),
    ],
)
def test_new_help_never_reads(args):
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert FakeClient.instances == []


def test_phone_show_inspects_sms_prerequisites():
    payload = dict(
        state="verified",
        phone_number="+12025550100",
        verified=True,
        verified_at=None,
        pending_phone_number=None,
        verification_expires_at=None,
        resend_available_at=None,
        sms_alerts_consented_at=None,
        sms_alerts_disclosure_version=None,
    )
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = invoke("account", "phone", "show")
    assert result.exit_code == 0, result.output
    assert {
        k: v for k, v in json.loads(result.stdout).items() if k != "schema"
    } == payload


def test_budget_rules_last_fired_percent_mapping_and_null(monkeypatch):
    from solwyn_cli.commands import budget_rules
    from test_budget_set_command import _response

    monkeypatch.setattr(budget_rules, "SolwynClient", FakeClient)
    monkeypatch.setattr(budget_rules, "resolve_credential", lambda: JWT)
    fired = copy.deepcopy(EVENT)
    fired.update(
        event_type="fired",
        rule_key="old-limit-key",
        rule_type="budget_threshold",
        metadata_json={"percent": 80},
    )
    FakeClient.responses = [
        httpx.Response(200, json=_response()),
        httpx.Response(200, json=dict(events=[fired], window=WINDOW)),
    ]
    result = invoke("budget", "rules", "list", "--with-last-fired")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["last_fired"]["budget:80"]["id"] == fired["id"]
    assert data["last_fired"]["forecast:125"] is None
    selectors = FakeClient.instances[0].requests[1][2]["json"]["rules"]
    assert selectors[0]["percent"] == 80


def test_account_last_fired_retains_lifecycle():
    from test_alerts_command import ACCOUNT

    fire = copy.deepcopy(EVENT)
    fire.update(
        project_id=None,
        rule_key="account-one",
        rule_type="account_spend_threshold",
        event_type="fired",
    )
    paused = dict(
        fire, id="paused", event_type="paused", emitted_at="2026-10-01T00:00:00Z"
    )
    FakeClient.responses = [
        httpx.Response(200, json=ACCOUNT),
        httpx.Response(200, json=dict(events=[paused, fire], window=WINDOW)),
    ]
    result = invoke("alerts", "account", "show", "--with-last-fired")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["last_fired"]["id"] == fire["id"]
    assert data["latest_event"]["event_type"] == "paused"


def test_identity_requires_full_me_and_shows_verified(monkeypatch):
    from solwyn_cli.commands import whoami

    monkeypatch.setattr(whoami, "SolwynClient", FakeClient)
    monkeypatch.setattr(whoami, "resolve_credential", lambda: JWT)
    me = dict(
        user_id="user-synthetic",
        email="user@example.invalid",
        email_verified=False,
        first_name="Synthetic",
        marketing_emails_subscribed=False,
        account_id="acct-synthetic",
        display_name="Synthetic",
        subscription_tier="free",
        alert_email_override=None,
    )
    acct = dict(
        id="acct-synthetic",
        display_name="Synthetic",
        subscription_tier="free",
        alert_email_override=None,
        seat_limit=1,
        seats_used=1,
        your_role="owner",
    )
    FakeClient.responses = [
        httpx.Response(200, json=me),
        httpx.Response(200, json=acct),
        httpx.Response(200, json=[]),
    ]
    result = invoke("whoami")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["user"]["email_verified"] is False


@pytest.mark.parametrize("route", ["activity", "pending"])
def test_all_later_page_failure_never_prints_complete_data(route):
    from solwyn_cli.errors import ApiError

    page = dict(events=[], next_cursor="continuation")
    if route == "activity":
        page["window"] = WINDOW
    FakeClient.responses = [
        httpx.Response(200, json=page),
        ApiError(
            status_code=503,
            code="service_busy",
            message="Unavailable",
            hint="Retry",
            detail=None,
        ),
    ]
    result = invoke("alerts", route, "--all")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "service_busy"


@pytest.mark.parametrize(
    "route,flags",
    [("activity", ["--range", "365d"]), ("activity", ["--event-type", "triggered"])],
)
def test_invalid_activity_query_is_local(route, flags):
    result = invoke("alerts", route, *flags)
    assert result.exit_code == 2
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [("alerts", "activity"), ("alerts", "pending"), ("account", "phone", "show")],
)
def test_new_account_reads_reject_project_key_before_http(monkeypatch, args):
    for module in (alerts, account):
        monkeypatch.setattr(module, "resolve_credential", lambda: KEY)
    result = invoke(*args)
    assert result.exit_code == 3
    assert not any(client.requests for client in FakeClient.instances)
    assert KEY.api_key.get_secret_value() not in result.output


@pytest.mark.parametrize(
    "payload",
    [
        dict(project_id="proj_alpha", uncounted_calls=0, uncounted_tokens=0),
        dict(
            project_id="proj_alpha",
            uncounted_calls=0,
            uncounted_tokens=0,
            last_reported_at=None,
            spend=0,
        ),
        dict(
            project_id="wrong",
            uncounted_calls=0,
            uncounted_tokens=0,
            last_reported_at=None,
        ),
    ],
)
def test_uncounted_invalid_response_fails_closed(payload):
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = invoke("budget", "uncounted")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "invalid_uncounted_response" in result.stderr


def test_uncounted_503_never_becomes_zero():
    from solwyn_cli.errors import error_from_response

    response = httpx.Response(
        503,
        json={"detail": "Budget backend temporarily unavailable; retry"},
        request=httpx.Request(
            "GET",
            "https://api.example.invalid/api/v1/projects/proj_alpha/budget/uncounted",
        ),
    )
    FakeClient.responses = [error_from_response(response)]
    result = invoke("budget", "uncounted")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "unpriced upper bound" in result.stderr
    assert "No reservation" not in result.stderr


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(unexpected=True),
        lambda p: p["tiers"]["team"]["capabilities"]["analytics"].update(
            unexpected=True
        ),
        lambda p: p["tiers"]["team"]["prices"]["month"].pop("currency"),
        lambda p: p["tiers"]["team"]["overage"].update(unit_amount_decimal_cents=None),
    ],
)
def test_catalog_nested_malformed_fails_closed(mutate):
    payload = copy.deepcopy(catalog())
    mutate(payload)
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = invoke("plan", "list")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_plan_catalog_response"


def test_latest_selector_chunk_cap_and_empty_nulls():
    from solwyn_cli.commands._alert_reads import AlertRuleLookup, request_latest_events

    selectors = [
        AlertRuleLookup(
            rule_key=f"rule-{i}", project_id="proj_alpha", rule_type="scoped_budget"
        )
        for i in range(201)
    ]
    FakeClient.responses = [
        httpx.Response(200, json=dict(events=[], window=WINDOW)) for _ in range(3)
    ]
    client = FakeClient(api_url="https://api.example.invalid", credential=JWT)
    result, window = request_latest_events(client=client, rules=selectors)
    assert len(result) == 201
    assert all(r.last_fired is None and r.latest_event is None for r in result)
    assert [len(r[2]["json"]["rules"]) for r in client.requests] == [100, 100, 1]
    assert window is not None


def test_last_fired_opt_out_no_extra_requests(monkeypatch):
    from solwyn_cli.commands import budget_rules
    from test_budget_set_command import _response

    monkeypatch.setattr(budget_rules, "SolwynClient", FakeClient)
    monkeypatch.setattr(budget_rules, "resolve_credential", lambda: JWT)
    FakeClient.responses = [httpx.Response(200, json=_response())]
    result = invoke("budget", "rules", "list")
    assert result.exit_code == 0
    assert len(FakeClient.instances[0].requests) == 1
    assert "last_fired" not in json.loads(result.stdout)


def test_last_fired_key_gate_precedes_budget_request(monkeypatch):
    from solwyn_cli.commands import budget_rules

    monkeypatch.setattr(budget_rules, "SolwynClient", FakeClient)
    monkeypatch.setattr(budget_rules, "resolve_credential", lambda: KEY)
    result = invoke("budget", "rules", "list", "--with-last-fired")
    assert result.exit_code == 3
    assert not any(client.requests for client in FakeClient.instances)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("email_verified"),
        lambda p: p.update(unexpected_private="private-canary"),
    ],
)
def test_identity_missing_and_unexpected_fields_fail_closed(monkeypatch, mutate):
    from solwyn_cli.commands import whoami

    monkeypatch.setattr(whoami, "SolwynClient", FakeClient)
    monkeypatch.setattr(whoami, "resolve_credential", lambda: JWT)
    me = dict(
        user_id="user-synthetic",
        email="user@example.invalid",
        email_verified=True,
        first_name="Synthetic",
        marketing_emails_subscribed=False,
        account_id="acct-synthetic",
        display_name="Synthetic",
        subscription_tier="free",
        alert_email_override=None,
    )
    mutate(me)
    FakeClient.responses = [httpx.Response(200, json=me)]
    result = invoke("whoami")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_identity_response"
    assert "private-canary" not in result.output


@pytest.mark.parametrize(
    "name,args,payload",
    [
        (
            "alerts-activity",
            ["alerts", "activity"],
            dict(events=[EVENT], next_cursor=None, window=WINDOW),
        ),
        (
            "alerts-pending",
            ["alerts", "pending"],
            dict(events=[EVENT], next_cursor=None),
        ),
        (
            "budget-uncounted",
            ["budget", "uncounted"],
            dict(
                project_id="proj_alpha",
                uncounted_calls=0,
                uncounted_tokens=0,
                last_reported_at=None,
            ),
        ),
        ("plan-catalog", ["plan", "list"], catalog()),
        ("account-phone", ["account", "phone", "show"], dict(state="none")),
    ],
)
def test_new_cli_json_output_goldens(name, args, payload):
    from pathlib import Path
    from golden.harness import assert_matches_golden

    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert_matches_golden(result.stdout, Path("tests/golden") / f"{name}-json.json")


def test_catalog_yearly_units_and_free_null_overage():
    payload = copy.deepcopy(catalog())
    tier = payload["tiers"]["team"]
    tier["prices"]["year"] = dict(
        amount_cents=100001, monthly_display_cents=8333, currency="usd"
    )
    tier["overage"]["unit_amount_decimal_cents"] = "0.123456"
    payload["tiers"]["free"] = dict(
        tier,
        tier="free",
        display_name="Free",
        prices=dict(month=None, year=None),
        overage=None,
    )
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = invoke("plan", "list")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["tiers"]["team"]["prices"]["year"] == {
        "amount_cents": 100001,
        "monthly_display_cents": 8333,
        "currency": "usd",
    }
    assert data["tiers"]["team"]["overage"]["unit_amount_decimal_cents"] == "0.123456"
    assert data["tiers"]["free"]["overage"] is None
    assert data["tiers"]["free"]["prices"] == {"month": None, "year": None}
