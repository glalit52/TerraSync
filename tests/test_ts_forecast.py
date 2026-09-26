"""Projection, and the guardrails that make it defensible.

This module crosses a line the rest of the product holds deliberately, so the
tests are mostly about what it refuses and how it speaks, not about arithmetic.
A projection that quietly becomes a prediction is the failure mode.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from terrashield.baseline import Observation
from terrashield.forecast import (
    MAX_HORIZON_FRACTION, MIN_FIT_QUALITY, MIN_OBSERVATIONS,
    Projection, ProjectionRefused, horizon_limit, project, try_project,
)

AOI = "IN-KCH-SECTOR"
METRIC = "building_count"


def series(values, start=date(2026, 4, 1), step=5, metric=METRIC):
    return [Observation(AOI, metric, start + timedelta(days=i * step), float(v))
            for i, v in enumerate(values)]


RISING = series([4, 5, 7, 8, 10, 11, 13, 14])
LAST = RISING[-1].when


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------

def test_too_little_history_is_refused_rather_than_guessed():
    with pytest.raises(ProjectionRefused, match=str(MIN_OBSERVATIONS)):
        project(AOI, METRIC, series([4, 5, 7]), date(2026, 5, 1))


def test_scatter_is_refused_rather_than_fitted():
    """A line through noise projects noise, and looks exactly like a trend."""
    noisy = series([5, 19, 2, 17, 4, 20, 3, 18])
    with pytest.raises(ProjectionRefused, match="scatter"):
        project(AOI, METRIC, noisy, date(2026, 5, 20))


def test_a_horizon_beyond_the_record_is_refused():
    with pytest.raises(ProjectionRefused, match="does not reach"):
        project(AOI, METRIC, RISING, date(2027, 6, 1))


def test_the_cap_is_a_fraction_of_the_observed_span():
    span = (RISING[-1].when - RISING[0].when).days
    reach = int(span * MAX_HORIZON_FRACTION)
    project(AOI, METRIC, RISING, LAST + timedelta(days=reach - 1))
    with pytest.raises(ProjectionRefused):
        project(AOI, METRIC, RISING, LAST + timedelta(days=reach + 5))


def test_a_horizon_in_the_past_is_a_lookup_not_a_projection():
    with pytest.raises(ProjectionRefused, match="lookup"):
        project(AOI, METRIC, RISING, date(2026, 4, 2))


def test_a_refusal_can_be_returned_as_data_for_the_interface():
    result = try_project(AOI, METRIC, series([1, 2]), date(2026, 5, 1))
    assert isinstance(result, dict)
    assert result["is_projection"] is False
    assert "refused" in result


def test_an_unknown_confidence_level_is_rejected():
    with pytest.raises(ValueError, match="confidence"):
        project(AOI, METRIC, RISING, LAST + timedelta(days=5), confidence=73)


# ---------------------------------------------------------------------------
# The interval
# ---------------------------------------------------------------------------

def test_a_projection_is_a_band_and_not_a_number():
    """There is no point-estimate field to read on its own."""
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10))
    payload = p.to_dict()
    assert "band" in payload
    assert payload["band"]["low"] < payload["band"]["high"]
    for forbidden in ("value", "estimate", "prediction", "expected", "midpoint"):
        assert forbidden not in payload, f"{forbidden} invites reading a point"


def test_the_band_widens_the_further_it_reaches():
    near = project(AOI, METRIC, RISING, LAST + timedelta(days=3))
    far = project(AOI, METRIC, RISING, LAST + timedelta(days=15))
    assert far.band.width > near.band.width


def test_more_confidence_means_a_wider_band():
    horizon = LAST + timedelta(days=10)
    narrow = project(AOI, METRIC, RISING, horizon, confidence=50)
    wide = project(AOI, METRIC, RISING, horizon, confidence=95)
    assert wide.band.width > narrow.band.width
    assert wide.band.low < narrow.band.low
    assert wide.band.high > narrow.band.high


def test_a_clean_trend_projects_in_the_direction_it_is_going():
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10))
    assert p.slope_per_day > 0
    assert p.band.low > RISING[-1].value, "a rising series projects upward"
    assert p.fit_quality > 0.9


def test_a_falling_series_projects_downward():
    falling = series([20, 18, 17, 15, 13, 12, 10, 9])
    p = project(AOI, METRIC, falling, falling[-1].when + timedelta(days=10))
    assert p.slope_per_day < 0
    assert p.band.high < falling[-1].value


def test_a_flat_series_is_the_case_projection_handles_best():
    """Zero variance is a perfect fit, not a failed one -- refusing it would
    reject the one series where the projection is most trustworthy."""
    flat = series([9, 9, 9, 9, 9, 9, 9, 9])
    p = project(AOI, METRIC, flat, flat[-1].when + timedelta(days=10))
    assert p.fit_quality == 1.0
    assert p.slope_per_day == pytest.approx(0.0, abs=1e-9)
    assert p.band.low == pytest.approx(9.0, abs=0.01)
    assert p.band.high == pytest.approx(9.0, abs=0.01)


def test_only_the_requested_metric_is_used():
    mixed = RISING + series([100, 200, 300, 400, 500, 600, 700, 800],
                            metric="vessel_count")
    p = project(AOI, METRIC, mixed, LAST + timedelta(days=10))
    assert p.observations == len(RISING)
    assert p.band.high < 50, "the other metric must not contaminate the fit"


# ---------------------------------------------------------------------------
# How it speaks
# ---------------------------------------------------------------------------

def test_the_headline_is_conditional_and_never_asserts_the_future():
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10))
    text = p.headline().lower()
    assert "if the observed trend continues" in text
    for forbidden in (" will ", "predicts", "forecast to be", "is going to",
                      "expected to reach", "certain"):
        assert forbidden not in text, f"{forbidden!r} asserts rather than projects"


def test_the_headline_states_the_interval_and_its_confidence():
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10), confidence=90)
    text = p.headline()
    assert "90% interval" in text
    assert re.search(r"between [\d.]+ and [\d.]+", text)


def test_every_serialised_projection_carries_its_assumption():
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10))
    payload = p.to_dict()
    assert payload["is_projection"] is True
    assert "not a model of cause" in payload["assumption"]
    assert payload["observations"] == len(RISING)
    assert payload["observed_from"] == RISING[0].when.isoformat()


def test_the_evidence_of_the_projection_travels_with_it():
    """A reader must be able to see what the line was fitted to."""
    p = project(AOI, METRIC, RISING, LAST + timedelta(days=10))
    payload = p.to_dict()
    for key in ("fit_quality", "observations", "observed_from", "observed_to",
                "slope_per_day", "days_ahead"):
        assert key in payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_horizon_limit_reports_how_far_the_data_reaches():
    limit = horizon_limit(RISING, METRIC)
    assert limit is not None
    project(AOI, METRIC, RISING, limit)
    with pytest.raises(ProjectionRefused):
        project(AOI, METRIC, RISING, limit + timedelta(days=5))


def test_horizon_limit_is_none_when_there_is_not_enough_history():
    assert horizon_limit(series([1, 2, 3]), METRIC) is None
