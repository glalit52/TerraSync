"""What the sensor can and cannot resolve.

The gap between what a customer asks for and what physics permits is where
geospatial products lose credibility, so the answers that disappoint are
tested as carefully as the ones that sell.
"""

from __future__ import annotations

import pytest

from terrashield.sensing import (
    GSD_M, PIXELS_REQUIRED, TARGETS, Platform, Task, assess,
    capability_matrix, cheapest_platform, explain, platforms_for,
)


# ---------------------------------------------------------------------------
# The answers nobody wants to hear
# ---------------------------------------------------------------------------

def test_people_are_not_visible_from_orbit_at_any_resolution():
    """Including the sharpest commercial optical that exists."""
    for platform in (Platform.SENTINEL_2, Platform.PLANETSCOPE,
                     Platform.SKYSAT, Platform.WORLDVIEW):
        verdict = assess("person", Task.DETECT, platform)
        assert not verdict.feasible, f"{platform} must not claim to see people"
        assert "four-hundredth of a pixel" in verdict.reason


def test_people_are_resolvable_from_a_uav_and_that_carries_obligations():
    verdict = assess("person", Task.DETECT, Platform.UAV)
    assert verdict.feasible
    assert "privacy" in explain("person")


def test_drones_in_flight_are_not_detectable_from_orbit():
    for platform in (Platform.SENTINEL_2, Platform.SKYSAT, Platform.WORLDVIEW):
        verdict = assess("drone_in_flight", Task.DETECT, platform)
        assert not verdict.feasible
        assert "RF, acoustic or radar" in verdict.reason


def test_the_drone_answer_points_at_what_can_be_seen_instead():
    """A refusal that names the alternative is a capability; one that does not
    is just a no."""
    assert "drone_ground_station" in explain("drone_in_flight")
    assert cheapest_platform("drone_ground_station") is Platform.SKYSAT


def test_a_moving_object_cannot_be_tracked_between_satellite_passes():
    """Revisit, not resolution, is the binding constraint."""
    verdict = assess("car", Task.TRACK, Platform.WORLDVIEW)
    assert not verdict.feasible
    assert "revisit" in verdict.reason
    assert "counting a population" in verdict.reason


def test_a_stationary_object_can_be_tracked_where_resolution_allows():
    """Tracking a vessel means re-identifying the same one between passes,
    which needs more pixels than merely detecting that a vessel is present.
    A 45 m beam is 4.5 pixels at Sentinel's 10 m -- enough to find, not enough
    to tell one ship from another."""
    assert assess("container_ship", Task.DETECT, Platform.SENTINEL_2).feasible
    assert not assess("container_ship", Task.TRACK, Platform.SENTINEL_2).feasible
    assert assess("container_ship", Task.TRACK, Platform.PLANETSCOPE).feasible


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def test_a_verdict_shows_its_working():
    verdict = assess("car", Task.DETECT, Platform.SKYSAT)
    assert verdict.feasible
    assert verdict.pixels_across == pytest.approx(1.8 / 0.5)
    assert verdict.pixels_needed == PIXELS_REQUIRED[Task.DETECT]
    assert "pixels" in verdict.reason


def test_classification_is_harder_than_detection():
    """Finding something and knowing what it is are different asks."""
    assert assess("car", Task.DETECT, Platform.SKYSAT).feasible
    assert not assess("car", Task.CLASSIFY, Platform.SKYSAT).feasible
    assert assess("car", Task.CLASSIFY, Platform.UAV).feasible


def test_a_car_is_invisible_to_free_imagery_and_that_is_the_sales_conversation():
    assert not assess("car", Task.DETECT, Platform.SENTINEL_2).feasible
    assert cheapest_platform("car") is Platform.SKYSAT


def test_large_infrastructure_works_on_free_imagery():
    for target in ("runway", "container_ship", "storage_tank",
                   "transport_aircraft", "large_structure"):
        assert assess(target, Task.DETECT, Platform.SENTINEL_2).feasible, target


def test_the_cheapest_working_platform_is_recommended_not_the_sharpest():
    """Coarser is cheaper. Recommending WorldView for a runway is how an
    imagery bill becomes a surprise."""
    assert cheapest_platform("runway") is Platform.SENTINEL_2
    assert cheapest_platform("solar_array") is Platform.LANDSAT
    assert cheapest_platform("building") is Platform.PLANETSCOPE


def test_platforms_for_is_ordered_coarsest_first():
    options = platforms_for("container_ship")
    gsds = [GSD_M[p] for p in options]
    assert gsds == sorted(gsds, reverse=True)


# ---------------------------------------------------------------------------
# SAR
# ---------------------------------------------------------------------------

def test_sar_sees_structures_but_not_soft_targets():
    assert assess("large_structure", Task.DETECT, Platform.SENTINEL_1).feasible
    verdict = assess("person", Task.DETECT, Platform.SENTINEL_1)
    assert not verdict.feasible


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

def test_the_matrix_covers_every_target_and_platform():
    rows = capability_matrix(Task.DETECT)
    assert len(rows) == len(TARGETS)
    for row in rows:
        for platform in GSD_M:
            assert platform.value in row


def test_the_matrix_marks_the_impossible_targets_as_such():
    rows = {r["target"]: r for r in capability_matrix(Task.DETECT)}
    assert rows["person"]["possible_from_orbit"] is False
    assert rows["drone_in_flight"]["possible_from_orbit"] is False
    assert rows["car"]["possible_from_orbit"] is True
    #: and the only route to them is the UAV column
    assert rows["person"]["cheapest"] == "uav"


def test_an_unknown_target_lists_the_known_ones():
    with pytest.raises(KeyError, match="car"):
        assess("battleship", Task.DETECT, Platform.SENTINEL_2)
    with pytest.raises(KeyError, match="car"):
        explain("battleship")


def test_every_target_dimension_is_plausible():
    """A wrong dimension here silently changes every verdict downstream."""
    for name, target in TARGETS.items():
        assert 0.1 <= target.smallest_m <= 200, name
        assert target.length_m >= target.width_m or name in {"transport_aircraft"}
