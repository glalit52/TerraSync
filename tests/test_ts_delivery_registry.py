"""Alert delivery and model governance.

Delivery is mostly about what must *not* leave the platform, and about failure
being visible: an alert that silently failed to send is worse than one that was
never configured, because the operator believes someone was told.

The registry is about being able to answer, in year two, "this alert was wrong
-- what else did that version produce?"
"""

from __future__ import annotations

import json
import time

import pytest

from terrashield.delivery import (
    ALLOWED_FIELDS, MAX_ATTEMPTS, Channel, Destination, DeliveryError,
    Dispatcher, Outcome, redact, sign, verify,
)
from terrashield.providers.transport import Response, TransportError
from terrashield.registry import (
    MIN_ACTIVATION_F1, Registry, RegistryError, Stage, digest_inputs,
)

ALERT = {
    "id": "alert-9", "aoi_id": "IN-KCH-SECTOR", "priority": 1,
    "severity": "high", "rule": "Construction or new track in a remote sector",
    "summary": "3,300 m2 structure at 23.90N 70.10E",
    "evidence_id": "ev-77", "copilot_note": "internal reasoning",
    "boundary": [[70.1, 23.9]],
}


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        nxt = self.responses.pop(0) if self.responses else Response(200, b"{}")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def webhook(**kw):
    kw.setdefault("secret", "s3cret")
    return Destination(kw.pop("id", "d1"), Channel.WEBHOOK,
                       kw.pop("target", "https://hooks.example/ts"), **kw)


def dispatcher(*destinations, transport=None):
    return Dispatcher(list(destinations), transport=transport or Fake(),
                      base_url="https://ts.example", sleep=lambda s: None)


# ---------------------------------------------------------------------------
# What may leave the platform
# ---------------------------------------------------------------------------

def test_the_payload_carries_references_and_never_content():
    """A webhook endpoint is usually the least protected surface a customer
    operates. Intelligence must not move into it."""
    payload = redact(ALERT, "https://ts.example")
    for leaked in ("summary", "evidence_id", "copilot_note", "boundary"):
        assert leaked not in payload, f"{leaked} must not cross the boundary"
    assert payload["alert_id"] == "alert-9"
    assert payload["url"].startswith("https://ts.example")


def test_redaction_is_a_whitelist_so_new_fields_do_not_leak_by_default():
    """A blacklist fails open: the next field added to an alert would be
    forwarded to every webhook and nobody would notice."""
    payload = redact({**ALERT, "newly_added_sensitive_field": "secret"})
    assert "newly_added_sensitive_field" not in payload
    assert set(payload) <= ALLOWED_FIELDS | {"note"}


def test_the_payload_explains_why_it_is_thin():
    payload = redact(ALERT)
    assert "behind the audit log" in payload["note"]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def test_a_delivery_is_signed_and_verifiable():
    transport = Fake()
    dispatcher(webhook(), transport=transport).dispatch(ALERT)
    call = transport.calls[0]
    header = call["headers"]["X-TerraShield-Signature"]
    assert verify(call["body"], header, "s3cret")


def test_the_wrong_secret_does_not_verify():
    transport = Fake()
    dispatcher(webhook(), transport=transport).dispatch(ALERT)
    call = transport.calls[0]
    assert not verify(call["body"],
                      call["headers"]["X-TerraShield-Signature"], "wrong")


def test_a_captured_delivery_cannot_be_replayed_later():
    """The timestamp is inside the signed material, not beside it."""
    body = b'{"alert_id":"a"}'
    stale = sign(body, "s3cret", int(time.time()) - 9999)
    assert not verify(body, stale, "s3cret")
    assert verify(body, sign(body, "s3cret", int(time.time())), "s3cret")


def test_a_tampered_body_does_not_verify():
    now = int(time.time())
    header = sign(b'{"priority":4}', "s3cret", now)
    assert not verify(b'{"priority":1}', header, "s3cret")


def test_a_malformed_signature_header_is_refused_not_crashed():
    for junk in ("", "garbage", "t=abc,v1=x", "v1=only"):
        assert verify(b"{}", junk, "s3cret") is False


def test_an_unsigned_webhook_destination_is_refused():
    """An unsigned webhook cannot be told from anything else that can reach
    the endpoint."""
    with pytest.raises(DeliveryError, match="signing secret"):
        dispatcher(Destination("d", Channel.WEBHOOK, "https://x",
                               secret="")).dispatch(ALERT)


# ---------------------------------------------------------------------------
# Delivery outcomes
# ---------------------------------------------------------------------------

def test_a_successful_delivery_is_recorded():
    result = dispatcher(webhook(), transport=Fake(Response(200, b"ok"))
                        ).dispatch(ALERT)[0]
    assert result.outcome is Outcome.DELIVERED
    assert result.attempts == 1


def test_a_server_error_is_retried_then_recorded_as_failed():
    transport = Fake(*[TransportError("boom", 500, "")] * MAX_ATTEMPTS)
    result = dispatcher(webhook(), transport=transport).dispatch(ALERT)[0]
    assert result.outcome is Outcome.FAILED
    assert result.attempts == MAX_ATTEMPTS


def test_a_client_error_is_not_retried():
    """Repeating a request the receiver called wrong produces the same answer
    and spends its rate limit."""
    transport = Fake(TransportError("bad request", 400, ""))
    result = dispatcher(webhook(), transport=transport).dispatch(ALERT)[0]
    assert result.outcome is Outcome.FAILED
    assert result.attempts == 1
    assert len(transport.calls) == 1


def test_a_rate_limit_is_retried_even_though_it_is_a_client_error():
    transport = Fake(TransportError("slow down", 429, ""), Response(200, b"ok"))
    result = dispatcher(webhook(), transport=transport).dispatch(ALERT)[0]
    assert result.outcome is Outcome.DELIVERED
    assert result.attempts == 2


def test_a_low_priority_alert_is_suppressed_with_the_reason():
    result = dispatcher(webhook(min_priority=1)).dispatch(
        {**ALERT, "priority": 3})[0]
    assert result.outcome is Outcome.SUPPRESSED
    assert "below this destination's threshold" in result.detail


def test_a_disabled_destination_is_suppressed_not_attempted():
    transport = Fake()
    result = dispatcher(webhook(enabled=False), transport=transport
                        ).dispatch(ALERT)[0]
    assert result.outcome is Outcome.SUPPRESSED
    assert transport.calls == []


def test_email_says_plainly_that_it_did_not_send():
    """A half-working mailer that silently drops is the failure this module
    exists to prevent."""
    result = dispatcher(Destination("m", Channel.EMAIL, "ops@example.com")
                        ).dispatch(ALERT)[0]
    assert result.outcome is Outcome.FAILED
    assert "no mail is sent and none is claimed to be" in result.detail


def test_every_destination_gets_its_own_result():
    results = dispatcher(webhook(id="a"), webhook(id="b", min_priority=1),
                         transport=Fake(Response(200, b""), Response(200, b""))
                         ).dispatch(ALERT)
    assert {r.destination_id for r in results} == {"a", "b"}


def test_the_summary_foregrounds_failures():
    """Silent failure is the risk: an alert that did not send looks exactly
    like a quiet week."""
    transport = Fake(*[TransportError("boom", 503, "")] * MAX_ATTEMPTS)
    d = dispatcher(webhook(), transport=transport)
    d.dispatch(ALERT)
    summary = d.summary()
    assert summary["failed"] == 1
    assert summary["failures"]
    assert "nobody was told" in summary["note"]


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    r = Registry()
    r.register("change-detector", "v1", "change", by="lalit@example.com")
    return r


def test_an_unmeasured_version_cannot_be_activated(registry):
    """Promoting one is how a detector silently gets worse, and the failure is
    invisible precisely because nobody measured it."""
    with pytest.raises(RegistryError, match="no recorded evaluation"):
        registry.activate("change-detector", "v1")


def test_an_evaluation_requires_the_test_set_it_was_measured_on(registry):
    with pytest.raises(RegistryError, match="test set"):
        registry.record_evaluation("change-detector", "v1", "  ", 0.9, 0.8, 50)


def test_an_evaluation_requires_samples(registry):
    with pytest.raises(RegistryError, match="at least one sample"):
        registry.record_evaluation("change-detector", "v1", "set", 0.9, 0.8, 0)


def test_a_measured_version_activates_and_keeps_its_score(registry):
    registry.record_evaluation("change-detector", "v1", "kutch-jul", 0.85, 0.81, 120)
    model = registry.activate("change-detector", "v1", by="lalit@example.com")
    assert model.stage is Stage.ACTIVE
    assert model.activated_by == "lalit@example.com"
    assert model.best.f1 == pytest.approx(0.8298, abs=1e-3)
    assert model.best.test_set == "kutch-jul"


def test_a_version_scoring_near_zero_is_refused(registry):
    """That is the range of a model wired up wrong, not one that is weak."""
    registry.record_evaluation("change-detector", "v1", "kutch-jul", 0.04, 0.03, 120)
    with pytest.raises(RegistryError, match=f"{MIN_ACTIVATION_F1:.2f}"):
        registry.activate("change-detector", "v1")


def test_activating_a_new_version_retires_the_old_one(registry):
    registry.record_evaluation("change-detector", "v1", "s", 0.85, 0.81, 100)
    registry.activate("change-detector", "v1")
    registry.register("change-detector", "v2")
    registry.record_evaluation("change-detector", "v2", "s", 0.91, 0.86, 100)
    registry.activate("change-detector", "v2")

    assert registry.versions["change-detector:v1"].stage is Stage.RETIRED
    assert registry.active("change-detector").version == "v2"


def test_withdrawing_a_version_enumerates_exactly_what_it_touched(registry):
    """What a withdrawal needs: listing what it produced rather than guessing
    from dates."""
    registry.record_evaluation("change-detector", "v1", "s", 0.85, 0.81, 100)
    registry.activate("change-detector", "v1")
    for i in range(3):
        registry.record_inference("change-detector", "v1", "IN-KCH-SECTOR",
                                  digest_inputs("s2:a", "s2:b"),
                                  finding_id=f"chg-{i}")

    registry.withdraw("change-detector", "v1", "false positives on cloud edges")
    assert registry.affected_findings("change-detector", "v1") == [
        "chg-0", "chg-1", "chg-2"]


def test_a_withdrawal_needs_a_reason(registry):
    with pytest.raises(RegistryError, match="needs a reason"):
        registry.withdraw("change-detector", "v1", "   ")


def test_a_withdrawn_version_cannot_be_reactivated(registry):
    registry.record_evaluation("change-detector", "v1", "s", 0.85, 0.81, 100)
    registry.withdraw("change-detector", "v1", "wrong")
    with pytest.raises(RegistryError, match="withdrawn"):
        registry.activate("change-detector", "v1")


def test_recording_anything_about_an_unregistered_version_is_refused(registry):
    with pytest.raises(RegistryError, match="not registered"):
        registry.record_evaluation("nope", "v1", "s", 0.9, 0.8, 10)
    with pytest.raises(RegistryError, match="not registered"):
        registry.record_inference("nope", "v1", "aoi", "digest")


def test_registering_the_same_version_twice_is_idempotent(registry):
    first = registry.register("change-detector", "v1")
    again = registry.register("change-detector", "v1")
    assert first is again
    assert len(registry.versions) == 1


def test_input_digests_are_stable_and_order_independent():
    """The provider can reproduce any scene from its id, so the digest is
    enough to rerun the inference without holding imagery."""
    assert digest_inputs("s2:a", "s2:b") == digest_inputs("s2:b", "s2:a")
    assert digest_inputs("s2:a") != digest_inputs("s2:c")


def test_health_reports_a_version_in_findings_that_nobody_registered(registry):
    """Exactly the case where provenance has already failed."""
    report = registry.health(seen_versions=["change-detector:v1", "v-legacy"])
    assert report["unregistered_in_use"] == ["v-legacy"]
    assert report["healthy"] is False


def test_health_reports_a_withdrawn_version_still_present_in_findings(registry):
    registry.record_evaluation("change-detector", "v1", "s", 0.85, 0.81, 100)
    registry.withdraw("change-detector", "v1", "wrong")
    report = registry.health(seen_versions=["change-detector:v1"])
    assert report["withdrawn_but_present_in_findings"] == ["change-detector:v1"]
    assert report["healthy"] is False


def test_health_is_clean_when_everything_is_registered_and_measured(registry):
    registry.record_evaluation("change-detector", "v1", "s", 0.85, 0.81, 100)
    registry.activate("change-detector", "v1")
    report = registry.health(seen_versions=["change-detector:v1"])
    assert report["healthy"] is True
    assert report["active"] == ["change-detector:v1"]
