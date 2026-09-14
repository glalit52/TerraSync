# The PRD, section by section, and what exists

`TerraShield_AI.docx` is a 56-section product definition. This is an honest
accounting of it: what is built, what is deliberately scoped out for the MVP,
and what is missing.

The MVP scope the PRD itself sets (section 41) is **AI Change Intelligence**,
with a must-have list of thirteen items. All thirteen exist. Phase 2 and later
material is mostly not built, and is marked so.

## Core product (sections 1–9)

| Section | Status | Where |
|---|---|---|
| 1–4 Detect, monitor, compare, explain, alert | Built | `pipeline.run_day` runs all five per AOI per day |
| 5–6 Users and personas | Partly | Roles and permissions exist (`rbac.py`); no per-persona interface |
| 7 Global intelligence map | Partly | The console has an estate map and per-site pages; no tiled basemap or zoom-to-object |
| 8 Area of interest | Built | Polygon, rectangle, circle, coordinates, GeoJSON/KML-shaped upload (`geo.rings_from_geojson`, `terrashield enroll --geojson`) |
| 9 Watchlists | Built | `Watchlist`, stored and served; no per-watchlist scheduling yet |

## Data (sections 10–12)

| Section | Status | Notes |
|---|---|---|
| 10 Satellite data engine | Built as a seam | `catalog.Provider` is the interface; `SyntheticProvider` is the demo implementation. Sentinel-2, Sentinel-1 and Landsat-9 are modelled with real revisit cadence, orbital phasing and track repeat. Commercial VHR is modelled as tasked-only. |
| 11 Multi-sensor fusion | Partly | Optical and SAR both work end to end, and the pipeline falls back to SAR when optical is cloud-bound. They are used in *alternation*, not fused into a single joint product. Thermal is a hook only. |
| 12 Geospatial context layers | Not built | No DEM, OSM, land cover or weather layers. The water band is provider-supplied scene classification, which is the one context layer the change engine genuinely needs. |

## The engines (sections 13–22)

| Section | Status | Where |
|---|---|---|
| 13 Object detection | Built | `detect.py` — multi-scale background suppression, robust thresholding, connected components, classification against real object dimensions. 14 classes. Classical, not learned; `PROFILES` is the seam a fine-tuned detector replaces. |
| 14 Change detection | Built | `change.py` — co-registration, radiometric normalisation, MAD noise floor, morphological opening, typed classification. Sensor-aware: log-ratio and multi-looking for SAR. Objects arriving and leaving are counted as activity, not filed as change events: a container terminal turns over forty ships a fortnight and filing each one buries the two warehouses that went up. |
| 15 Time-series intelligence | Partly | `/api/timeline` and the console timeline carry acquisitions, changes and scores on one axis, including rejected scenes. No multi-image temporal model. |
| 16 Pattern of life | Built | `baseline.py` — robust medians with a weekday term, tied to the AOI's geometry fingerprint. |
| 17 Anomaly detection | Built | `anomaly.py` — weighted deviation, soft-maximum combination, score and confidence kept separate. Scores both same-day excursions and sustained upward trends, because a build-up gradual enough that no single day is unusual is precisely what a same-day test cannot see. Infrastructure counts are excluded from the activity score: buildings do not come and go between passes, so their count's variation is the detector, not the ground. |
| 18 Risk scoring | Built | `risk.py` — all five dimensions the PRD names, kept as five numbers. Persistence is measured backwards over the comparisons that have already covered a place, because a pipeline running a day at a time has no future to look into. |
| 19 Alert engine | Built | `alerts.py` — declarative rules, dashboard/email/webhook channels declared per rule. Channel *delivery* is not implemented; the alert records where it should have gone. |
| 20 Alert prioritisation | Built | Four bands from the composite risk score, with deduplication and per-rule suppression windows. |
| 21 AI copilot | Built, grounded | `copilot.py` parses a question into a typed query and answers from the store with citations. No language model: see the module docstring for why it is arranged this way round. |
| 22 Evidence-based AI | Built | `evidence.py` — every finding carries a hashed bundle. Packs verify. |

## Modules and interface (sections 23–30)

| Section | Status | Notes |
|---|---|---|
| 23 Report generator | Built | Daily, weekly and site reports in markdown (`reports.py`), each leading with coverage. |
| 24 Maritime module | Partly | Vessel detection, port activity and arrival/departure counts work. No AIS ingest, therefore no AIS/satellite discrepancy detection — that is the module's whole point and it needs a data source this build does not have. |
| 25 Critical infrastructure | Built | `AoiKind` drives per-site-type change weighting; damage, inundation and construction are typed change classes. |
| 26 Disaster intelligence | Partly | Inundation and water-extent change are measured and demonstrated at the dam. No fire or wind-damage classes. |
| 27–30 Dashboard | Partly | The console has the overview metrics, an estate map, the alert queue, site intelligence pages, coverage charts, the anomaly strip and a working before/after/change-mask viewer with a drag divider. It has no live tiled map, no object browser and no admin screens. |

## Platform (sections 31–40)

| Section | Status | Notes |
|---|---|---|
| 31 Architecture | Followed in shape, not in stack | Same stages, same order. Deliberately stdlib-only: see `04-deploying-for-real.md`. |
| 32 Technology stack | Not followed | The PRD asks for Next.js, FastAPI, PostGIS, PyTorch. None is used. The reason is section 39's own requirement — on-premise and eventually air-gapped — which a dependency-free core makes a copy rather than a procurement. `store/schema.py` keeps all SQL in one module so PostGIS is a rewrite of one file. |
| 33 Data pipeline | Built | `pipeline.py`, in the PRD's order, idempotent per day. |
| 34 Model strategy | Partly | The classical engines are the "existing model" tier. `detect.PROFILES` and `change._classify` are the seams a fine-tuned or foundation model plugs into. The proprietary tier — baselines, site history, analyst feedback — is built. |
| 35 Data model | Built | Every entity in the PRD's list exists except `Inference`, which is folded into the evidence bundle. |
| 36–37 Object and change schemas | Built | Every named field, plus a few the PRD omits: resolution caveats, obscured fraction, registration shift, noise floor. |
| 38 Human in the loop | Built | Confirm, reject, escalate, with the verdict stored against the finding under a named actor and written to the audit chain. Feedback is *captured* in the shape a training set needs; no retraining loop exists. |
| 39 Security | Mostly built | RBAC, tenant isolation enforced in SQL, a hash-chained audit log covering reads as well as writes, bearer auth that refuses to start unauthenticated. No SSO, no MFA, no encryption at rest — those are deployment concerns, and a single-file SQLite database is designed to sit on an encrypted volume. |
| 40 API | Built | Every endpoint listed, plus `/api/timeline`, `/api/evidence/{id}`, `/api/audit` and `/api/me`. `POST /api/analysis` reads stored results rather than triggering a run: tasking imagery costs money per square kilometre, and that should not happen because someone sent a POST. |

## MVP checklist (section 41)

| Must have | Status |
|---|---|
| User authentication | Bearer tokens with per-request identity; SSO is the seam |
| Map | Estate map in the console |
| AOI creation | `terrashield enroll`, GeoJSON upload, validation at the door |
| Satellite imagery | `Provider` seam, three modelled constellations |
| Historical imagery | 60-day lookback, full scene catalogue |
| Image comparison | The change viewer, with a drag divider |
| Change detection | `change.py` |
| Basic object detection | `detect.py` |
| Watchlists | `Watchlist` |
| Alerts | `alerts.py` with prioritisation |
| Timeline | `/api/timeline` and the console |
| AI summaries | `copilot.py`, `reports.py`, `anomaly.headline` |
| Evidence viewer | `terrashield explain`, `/api/evidence/{id}`, console evidence panels |

## A choice worth flagging, not a gap

A structure that stays put is re-confirmed on every subsequent comparison, and
each confirmation raises a fresh alert once the rule's suppression window has
elapsed. On the Kutch sector that is one alert every six days for a building
that is not going anywhere.

That is deliberate for a sector under watch — "still there, and here is this
week's evidence for it" is a row an analyst may want — but it is a defensible
place to disagree. The alternative is to scale the suppression window by
persistence, so that the better established a finding is the less often it
re-alerts, on the grounds that alerting exists to say something new. That would
be a few lines in `alerts.evaluate`, and the right way to settle it is with an
analyst watching their own queue for a fortnight rather than by argument.

## Not built, and worth naming

- **AIS ingest**, and therefore the AIS/satellite discrepancy detection that is
  the maritime module's reason to exist (section 24).
- **Thermal**, beyond a hook in the sensor model (section 11).
- **Context layers** — DEM, OSM, land cover, weather (section 12).
- **Learned models.** The detector is classical. It is a baseline to beat, and
  `evaluate.py` is how you would tell whether a learned model beat it.
- **Knowledge graph, multi-source fusion, private deployments** — Phase 3 and 4
  (sections 45–46).
- **Channel delivery.** Rules declare email and webhook; nothing sends.
- **A tiled basemap.** The console draws geometry, not imagery basemaps.
