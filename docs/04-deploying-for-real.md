# What changes when the imagery is real

The demo runs on modelled imagery. This is what a real deployment replaces, in
the order it matters.

## 1. The imagery provider

One module. `catalog.Provider` is a three-method interface:

```python
class Provider(Protocol):
    def search(self, aoi, start, end, constellations) -> list[Scene]: ...
    def fetch(self, aoi, scene, gsd_m=None) -> Raster: ...
    def masks(self, aoi, scene, raster) -> SceneMasks: ...
```

`search` is a STAC query. Sentinel Hub, Planetary Computer, Planet and Maxar
all expose one, and the `Scene` fields map onto STAC item properties almost
directly — `eo:cloud_cover`, `view:off_nadir`, `view:sun_elevation`,
`sat:relative_orbit`.

`fetch` reads pixels for the AOI's bounding box. In production this is a
windowed read from a cloud-optimised GeoTIFF, which is exactly what COG is for.

`masks` returns the quality bands that ship with the product — Sentinel-2's
scene classification layer, Landsat's `QA_PIXEL`, Planet's usable-data mask.
Do not re-derive these from brightness. The engines used to, and the result was
a new solar block reported as eleven hectares of inundation on a desert energy
site, because photovoltaic panels are as dark as water in the visible bands.

Nothing above this interface changes. The pipeline cannot tell a synthetic
provider from a real one, which is the point of it being a seam.

## 2. Retune the thresholds, and measure

Every threshold in `detect.py` and `change.py` was tuned against the synthetic
estate. They will not transfer unchanged to a customer's terrain, sensors and
object mix. The retuning procedure is the one `evaluate.py` already implements:

1. Label a few dozen scenes across the customer's sites.
2. Run `evaluate.score_scene` over them.
3. Sweep `detect.DEFAULT_MIN_CONFIDENCE` and `change.SIGMA`, and take the F1
   optimum — the same sweep that chose the current 0.50 is recorded in
   `03-measured-performance.md`.
4. Check `detect.calibration_report`. If a confidence of 0.8 is not right about
   eight times in ten, fix that before anything else: an analyst learns within
   a week whether the numbers mean anything, and if they do not, they start
   ignoring all of them.

## 3. Replace the detector, keep the interface

The classical detector is a baseline to beat. A fine-tuned YOLO-class model or
a geospatial foundation model will beat it on real imagery, and should replace
the body of `detect.detect`.

What must not change is the shape of the output. `DetectionResult` carries
`skipped` — the classes the sensor cannot support, with reasons — and that is
not a property of the model, it is a property of the ground sample distance. A
learned model that quietly reports zero vehicles from a 10 m scene has told the
analyst something false by implication.

## 4. Scale the storage

SQLite is right for a single site estate and for an accreditation conversation,
and wrong for a national one. `store/schema.py` holds every table definition
and `store/repo.py` every query, so the move to PostgreSQL with PostGIS is a
rewrite of two files. Two things change materially:

- **Geometry becomes geometry.** Today it is GeoJSON text plus precomputed
  bounding-box columns, because there is no spatial index without PostGIS. With
  PostGIS, `aois_intersecting` becomes a real `ST_Intersects` with a GiST index.
- **Tenant isolation gets a second layer.** It is currently enforced in the SQL
  of every query. Add row-level security so that a future query written outside
  the repository cannot bypass it.

## 5. Scale the compute

The engines are pure Python and process a 36 km² site in a few seconds per
scene. That is fine for hundreds of AOIs and wrong for thousands.

The honest fix is not to micro-optimise the loops. It is to replace
`raster.py`'s inner operations with numpy, keeping the same function
signatures, and to move `pipeline.run_day` onto a work queue — Celery or
Temporal, as the PRD suggests — with one task per AOI-day. Both are mechanical
because the pipeline is already idempotent per day: re-running a date range
rewrites the same rows rather than duplicating them, so retries are free.

Note what should *not* be changed for speed: the multi-scale sweep in
`detect.py` and the morphological opening in `change.py`. Both were added to fix
specific, measured failures — large objects invisible to a single background
window, and SAR speckle producing a starfield of false change.

## 6. Security work a customer will ask for

Built: RBAC, tenant isolation in SQL, a hash-chained audit log covering imagery
reads and copilot interactions, bearer auth that refuses to start without
credentials.

Not built, and all deployment-shaped:

- **SSO and MFA.** `api.serve`'s `token_map` is the seam; replace it with an
  OIDC verifier.
- **Encryption at rest.** The database is one file. Put it on an encrypted
  volume; that is the whole answer for on-premise.
- **Key management and retention.** `retention.configure` exists as a
  permission with nothing behind it.
- **Air-gap.** Already close: the core has no dependencies, so an air-gapped
  install is a copy of a directory plus a Python interpreter. What it needs is
  an imagery delivery path — physical media, or a one-way diode — and a
  `Provider` that reads from a local archive rather than an API.

## Identity, and what to replace

`accounts.py` provides sign-up, sign-in and sessions so a pilot can start
without an identity provider. It is not an identity provider, and a government
deployment will not use it as one. What it does not do, deliberately:

- no email verification, so an address is unproven until someone checks it;
- no password reset, which needs a mail path this system does not have;
- no MFA, no OAuth, no SAML, no SCIM provisioning;
- no lockout or rate limiting on repeated failures — that belongs at the edge,
  where the source address is visible, not in the application.

The seam is `Accounts.resolve_session`. Everything above it takes
`(org, actor, role)` and does not care how they were established, so federating
to a customer directory means replacing that one method and the three routes in
`_PUBLIC_ROUTES`. Those routes are in a separate registry precisely so that
"what can be reached without signing in" is a list you can read rather than a
property you have to derive from the routing table.

When you do federate, delete the sign-up route rather than guarding it. An
unused registration endpoint is unused attack surface, and `serve()` already
takes `allow_signup=False` for that case.

Two things it does get right and that a replacement must keep: session tokens
are stored as SHA-256 digests and never as tokens, so a stolen database yields
no usable sessions; and every login failure returns one identical status and
message, with the unknown-address path still performing a dummy verify so it
does not answer faster either.

## Areas and fields

An **area** (`Aoi`) is the unit of tasking, coverage and baselines: it is what
imagery is ordered over and what pattern of life is learned for. A **field** is
a named sub-polygon of one — a parcel, a berth, a sector, a block — and exists
so a finding reads "Block D" instead of a pair of coordinates.

Fields are deliberately not areas. Promoting every parcel to an AOI would
multiply the imagery bill by the number of parcels and build each baseline from
a fraction of the observations, which is both more expensive and less accurate.

A field whose boundary falls outside its area is refused at creation. Imagery
is only ever fetched for the AOI footprint, so such a field would sit in the
interface looking monitored and never once produce a finding — the worst
available way for a monitoring system to be wrong, because nothing about it
looks broken.

`Store.field_at(lon, lat)` is what turns a change centroid into a field name.
It filters on the bbox columns in SQL and runs point-in-polygon in Python, the
same trade `aois_intersecting` makes, and for the same reason: there is no
spatial index without PostGIS. At the number of fields one area holds this is
not the bottleneck; at national scale it is, and the fix is the PostGIS move
described above.
