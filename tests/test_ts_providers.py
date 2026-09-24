"""Imagery providers: selection, credentials, and the STAC mapping.

The network is not available to this suite and should not be. Every vendor call
goes through a `Transport`, so these tests substitute a fake one carrying
response bodies shaped like the real catalogues. That is what makes the client
testable at all: an imagery integration that can only be exercised against a
live vendor is one that is never exercised, because running the suite would
need credentials, network and somebody's quota.

What is verified here is the part that is ours: selection, credential
handling, paging, and the mapping from STAC item properties onto `Scene`. What
is not verified is that the vendors return what their documentation says --
that needs one live call, and `terrashield providers --check` makes it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from terrashield import providers
from terrashield.domain import Aoi, AoiKind, Constellation, Sensor
from terrashield.geo import rectangle
from terrashield.providers import (
    CredentialError, ProviderUnavailable, StacProvider, preset,
)
from terrashield.providers.credentials import Credentials
from terrashield.providers.transport import Response, TransportError

AOI = Aoi("AOI-1", "org-1", "Mundra", rectangle((69.70, 22.84), 4000, 3200),
          AoiKind.PORT)


class FakeTransport:
    """Records requests, replays queued responses."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}),
                           "body": json.loads(body) if body and
                           body.startswith(b"{") else body})
        if not self.queued:
            raise AssertionError(f"unexpected request: {method} {url}")
        nxt = self.queued.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(payload: dict) -> Response:
    return Response(200, json.dumps(payload).encode(), {})


def s2_item(item_id="S2A_43QBA_20260401_0_L2A", when="2026-04-01T05:42:11.024Z",
            **props) -> dict:
    """A Sentinel-2 L2A item shaped like Earth Search returns one."""
    base = {
        "datetime": when,
        "eo:cloud_cover": 12.4,
        "view:off_nadir": 3.2,
        "view:sun_elevation": 61.7,
        "sat:relative_orbit": 133,
        "sat:orbit_state": "descending",
        "gsd": 10,
        "platform": "sentinel-2a",
    }
    base.update(props)
    return {"id": item_id, "type": "Feature", "collection": "sentinel-2-l2a",
            "properties": base,
            "assets": {
                "red": {"href": "https://example.invalid/B04.tif"},
                "scl": {"href": "https://example.invalid/SCL.tif"}}}


def make(name="earth-search", *responses, env=None):
    spec = preset(name)
    transport = FakeTransport(*responses)
    creds = Credentials.from_env(spec.name, spec.auth, env=env or {},
                                 transport=transport)
    return StacProvider(preset=spec, credentials=creds,
                        transport=transport), transport


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_the_default_provider_is_free_and_needs_no_account():
    spec = preset(providers.DEFAULT_PRESET)
    assert spec.free and not spec.needs_account


def test_the_provider_is_chosen_by_one_environment_variable():
    assert providers.configured_name({}) == "earth-search"
    assert providers.configured_name(
        {"TERRASHIELD_PROVIDER": "cdse"}) == "cdse"


def test_mission_names_resolve_to_the_vendor_that_carries_them():
    for alias in ("sentinel", "sentinel-2", "aws"):
        assert preset(alias).name == "earth-search"
    assert preset("copernicus").name == "cdse"
    assert preset("SENTINEL").name == "earth-search", "case insensitive"


def test_an_unknown_provider_names_the_known_ones():
    with pytest.raises(KeyError, match="earth-search"):
        preset("nonesuch")


def test_synthetic_stays_one_word_rather_than_a_separate_code_path():
    provider = providers.resolve("synthetic")
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30))
    assert scenes, "the modelled estate still serves scenes offline"


def test_only_free_providers_are_available_before_any_key_is_added():
    assert providers.available(env={}) == [
        "synthetic", "earth-search", "planetary-computer"]


def test_adding_a_key_makes_that_vendor_available_with_no_code_change():
    with_key = providers.available(env={"TERRASHIELD_PLANET_API_KEY": "abc"})
    assert "planet" in with_key


def test_status_names_the_exact_variables_still_needed():
    report = providers.status(env={})
    planet = next(p for p in report["providers"] if p["name"] == "planet")
    assert planet["ready"] is False
    assert planet["missing"] == ["TERRASHIELD_PLANET_API_KEY"]

    cdse = next(p for p in report["providers"] if p["name"] == "cdse")
    assert cdse["free"] and cdse["needs_account"]
    assert set(cdse["missing"]) == {"TERRASHIELD_CDSE_CLIENT_ID",
                                    "TERRASHIELD_CDSE_CLIENT_SECRET"}


def test_status_never_echoes_a_secret():
    env = {"TERRASHIELD_PLANET_API_KEY": "super-secret-value"}
    blob = json.dumps(providers.status(env=env))
    assert "super-secret-value" not in blob


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_an_open_catalogue_sends_no_auth_header():
    spec = preset("earth-search")
    creds = Credentials.from_env(spec.name, spec.auth, env={})
    assert creds.complete
    assert creds.headers() == {}


def test_a_missing_key_fails_with_the_variable_name_in_the_message():
    spec = preset("planet")
    creds = Credentials.from_env(spec.name, spec.auth, env={})
    with pytest.raises(CredentialError, match="TERRASHIELD_PLANET_API_KEY"):
        creds.headers()


def test_an_api_key_attaches_in_the_vendors_own_header_format():
    spec = preset("planet")
    creds = Credentials.from_env(spec.name, spec.auth,
                                 env={"TERRASHIELD_PLANET_API_KEY": "k123"})
    assert creds.headers() == {"Authorization": "api-key k123"}

    spec = preset("maxar")
    creds = Credentials.from_env(spec.name, spec.auth,
                                 env={"TERRASHIELD_MAXAR_API_KEY": "k456"})
    assert creds.headers() == {"Maxar-API-Key": "k456"}


def test_oauth2_exchanges_the_secret_for_a_token_and_caches_it():
    spec = preset("cdse")
    transport = FakeTransport(ok({"access_token": "tok-1", "expires_in": 3600}))
    creds = Credentials.from_env(
        spec.name, spec.auth,
        env={"TERRASHIELD_CDSE_CLIENT_ID": "id",
             "TERRASHIELD_CDSE_CLIENT_SECRET": "sec"},
        transport=transport)

    assert creds.headers() == {"Authorization": "Bearer tok-1"}
    assert creds.headers() == {"Authorization": "Bearer tok-1"}
    assert len(transport.calls) == 1, "the token is cached, not refetched"

    sent = transport.calls[0]
    assert sent["method"] == "POST"
    assert b"grant_type=client_credentials" in sent["body"]


def test_a_token_is_refreshed_before_it_expires_in_flight():
    spec = preset("cdse")
    transport = FakeTransport(ok({"access_token": "tok-1", "expires_in": 30}),
                              ok({"access_token": "tok-2", "expires_in": 3600}))
    creds = Credentials.from_env(
        spec.name, spec.auth,
        env={"TERRASHIELD_CDSE_CLIENT_ID": "id",
             "TERRASHIELD_CDSE_CLIENT_SECRET": "sec"},
        transport=transport)
    #: 30s of life is inside the refresh margin, so the second call must not
    #: reuse it -- a token that dies mid-request is an outage that looks random.
    assert creds.headers()["Authorization"] == "Bearer tok-1"
    assert creds.headers()["Authorization"] == "Bearer tok-2"


def test_rejected_credentials_say_what_the_vendor_said():
    spec = preset("cdse")
    transport = FakeTransport(
        TransportError("HTTP 401", 401, '{"error":"invalid_client"}'))
    creds = Credentials.from_env(
        spec.name, spec.auth,
        env={"TERRASHIELD_CDSE_CLIENT_ID": "id",
             "TERRASHIELD_CDSE_CLIENT_SECRET": "bad"},
        transport=transport)
    with pytest.raises(CredentialError, match="invalid_client"):
        creds.headers()


# ---------------------------------------------------------------------------
# Search and mapping
# ---------------------------------------------------------------------------

def test_a_stac_item_becomes_a_scene_the_pipeline_understands():
    provider, _ = make("earth-search", ok({"features": [s2_item()]}))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2,))
    assert len(scenes) == 1
    scene = scenes[0]
    assert scene.id == "S2A_43QBA_20260401_0_L2A"
    assert scene.aoi_id == "AOI-1"
    assert scene.constellation is Constellation.SENTINEL_2
    assert scene.sensor is Sensor.OPTICAL
    assert scene.acquired_at == datetime(2026, 4, 1, 5, 42, 11, 24000,
                                         tzinfo=timezone.utc)
    assert scene.cloud_pct == pytest.approx(12.4)
    assert scene.sun_elevation_deg == pytest.approx(61.7)
    assert scene.off_nadir_deg == pytest.approx(3.2)
    assert scene.gsd_m == 10


def test_the_search_asks_for_the_aoi_bbox_and_the_window():
    provider, transport = make("earth-search", ok({"features": []}))
    provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                    (Constellation.SENTINEL_2,))
    body = transport.calls[0]["body"]
    assert body["collections"] == ["sentinel-2-l2a"]
    assert body["datetime"].startswith("2026-04-01T00:00:00Z/2026-04-30")
    min_lon, min_lat, max_lon, max_lat = body["bbox"]
    assert min_lon < 69.70 < max_lon and min_lat < 22.84 < max_lat


def test_the_orbit_carries_its_direction_so_sar_pairs_can_be_refused():
    """Differencing two SAR tracks compares viewing geometries, not ground."""
    provider, _ = make("earth-search", ok({"features": [s2_item()]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_2,))[0]
    assert scene.orbit == "133-des"


def test_an_unknown_cloud_fraction_is_treated_as_cloudy_not_clear():
    """Unknown must fail safe. A scene wrongly treated as clear gets
    differenced and produces findings out of weather."""
    item = s2_item()
    del item["properties"]["eo:cloud_cover"]
    provider, _ = make("earth-search", ok({"features": [item]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_2,))[0]
    assert scene.cloud_pct == 100.0


def test_sar_is_never_reported_as_cloudy():
    item = s2_item("S1A_IW_GRDH_20260402", "2026-04-02T01:12:00Z")
    item["properties"].pop("eo:cloud_cover")
    provider, _ = make("earth-search", ok({"features": [item]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_1,))[0]
    assert scene.sensor is Sensor.SAR
    assert scene.cloud_pct == 0.0


def test_a_sun_zenith_is_converted_to_an_elevation():
    item = s2_item()
    del item["properties"]["view:sun_elevation"]
    item["properties"]["view:sun_zenith"] = 28.3
    provider, _ = make("earth-search", ok({"features": [item]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_2,))[0]
    assert scene.sun_elevation_deg == pytest.approx(61.7)


def test_an_item_with_no_timestamp_is_dropped_rather_than_guessed():
    """Everything downstream is indexed by time; a scene without one is not
    a scene."""
    item = s2_item()
    del item["properties"]["datetime"]
    provider, _ = make("earth-search", ok({"features": [item, s2_item("good")]}))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2,))
    assert [s.id for s in scenes] == ["good"]


def test_results_come_back_in_time_order():
    provider, _ = make("earth-search", ok({"features": [
        s2_item("c", "2026-04-20T05:00:00Z"),
        s2_item("a", "2026-04-02T05:00:00Z"),
        s2_item("b", "2026-04-11T05:00:00Z")]}))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2,))
    assert [s.id for s in scenes] == ["a", "b", "c"]


def test_paging_follows_the_next_link_until_it_stops():
    page1 = {"features": [s2_item("one", "2026-04-02T05:00:00Z")],
             "links": [{"rel": "next",
                        "href": "https://earth-search.aws.element84.com/v1/search",
                        "body": {"token": "page-2"}}]}
    page2 = {"features": [s2_item("two", "2026-04-12T05:00:00Z")], "links": []}
    provider, transport = make("earth-search", ok(page1), ok(page2))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2,))
    assert [s.id for s in scenes] == ["one", "two"]
    assert transport.calls[1]["body"]["token"] == "page-2"


def test_paging_stops_on_an_empty_page_even_with_a_next_link():
    """Some catalogues always emit `next`. Following it forever is a way to
    spend a quota on nothing."""
    loop = {"features": [], "links": [{"rel": "next", "href": "https://x/search"}]}
    provider, transport = make("earth-search", ok(loop))
    assert provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                           (Constellation.SENTINEL_2,)) == []
    assert len(transport.calls) == 1


def test_a_constellation_the_vendor_does_not_carry_is_skipped_quietly():
    provider, transport = make("earth-search")
    assert provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                           (Constellation.COMMERCIAL_VHR,)) == []
    assert transport.calls == [], "no request for a mission it cannot serve"


def test_a_vendor_failure_names_the_vendor_and_the_reason():
    provider, _ = make("earth-search",
                       TransportError("HTTP 503", 503, "upstream unavailable"))
    with pytest.raises(ProviderUnavailable, match="Earth Search"):
        provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                        (Constellation.SENTINEL_2,))


def test_asset_hrefs_resolve_by_preference_because_vendors_disagree():
    """`red` on Earth Search is `B04` on Copernicus, for the same band."""
    provider, _ = make("earth-search")
    item = s2_item()
    assert provider.asset_href(item, ("red", "B04")).endswith("B04.tif")
    assert provider.asset_href(item, ("B04", "red")).endswith("B04.tif")
    assert provider.asset_href(item, ("nothing_here",)) == ""


def test_searching_two_missions_queries_each_collection():
    provider, transport = make(
        "earth-search",
        ok({"features": [s2_item("s2", "2026-04-02T05:00:00Z")]}),
        ok({"features": [s2_item("s1", "2026-04-03T01:00:00Z")]}))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2, Constellation.SENTINEL_1))
    assert len(scenes) == 2
    asked = [c["body"]["collections"][0] for c in transport.calls]
    assert asked == ["sentinel-2-l2a", "sentinel-1-grd"]


# ---------------------------------------------------------------------------
# Pixels
# ---------------------------------------------------------------------------

def test_reading_pixels_without_the_extra_says_what_to_install():
    """The catalogue works without it, so the message must not read as fatal."""
    from terrashield.providers.stac import PixelsUnavailable
    try:
        import rasterio  # noqa: F401, PLC0415
        pytest.skip("rasterio is installed; the guarded path cannot be reached")
    except ImportError:
        pass

    provider, _ = make("earth-search", ok({"features": [s2_item()]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_2,))[0]
    with pytest.raises(PixelsUnavailable, match="terrashield\\[imagery\\]"):
        provider.fetch(AOI, scene)
