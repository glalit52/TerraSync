# Measured performance

PRD section 54 asks for detection precision, recall, false-positive rate and
confidence calibration. These are those numbers. They are produced by a
repeatable command, not asserted:

```bash
terrashield evaluate --from 2026-02-01 --to 2026-03-31 --scenes 3
```

**Read the caveat first.** Every number here is measured against the synthetic
estate in `sites.py`, through the sensor model in `sensors.py`. That makes them
meaningful as a *regression baseline* and as evidence that the engines recover
real structure from pixels rather than reporting facts they were handed. It does
not make them a prediction of performance on a customer's imagery. Re-run the
same measurement against labelled customer scenes before quoting any of it.

## Object detection

Optical scenes only, three per site, February–March 2026. Recall is measured
against objects the sensor could actually resolve — at least 2.5 pixels in the
long dimension and 1.5 across — because scoring a 10 m scene for failing to find
cars produces a number that says nothing about the detector.

| Site | Precision | Recall | F1 | False positives per km² per look |
|---|---:|---:|---:|---:|
| Mundra Port | 0.87 | 0.77 | 0.81 | 0.107 |
| Bhadla Solar Park | 1.00 | 0.95 | 0.98 | 0.000 |
| Rann of Kutch sector | 0.17 | 0.50 | 0.25 | 0.093 |
| Sardar Sarovar | 1.00 | 0.67 | 0.80 | 0.000 |
| **Total** | **0.85** | **0.81** | **0.83** | **0.055** |

### Why Kutch looks terrible, and why that is the useful row

The salt flat has roughly one resolvable object in it — a single 40 × 26 m
observation post. Against a denominator of one, a single false positive takes
precision to 0.5 and two take it to 0.33. Precision is simply the wrong summary
for near-empty terrain.

**False positives per square kilometre per look** is the number to use there,
and it is 0.093: about one spurious detection for every eleven square kilometres
imaged. Over the 18 km² sector that is one or two per pass for an analyst to
dismiss. That is a workload, which is a thing a customer can price; "precision
0.17" is not.

It is also the row that matters most commercially. A border sector is the
hardest false-positive environment there is, because there is nothing there to
be right about, and a detector that invents structures on empty ground will
drown a real deployment whatever it scores at a busy port.

## Confidence calibration

Does a confidence of 0.8 mean right about eight times in ten?

| Confidence band | n | Observed correct | Midpoint | Gap |
|---|---:|---:|---:|---:|
| 0.35 – 0.50 | 3 | 0.00 | 0.42 | −0.42 |
| 0.50 – 0.65 | 11 | 0.82 | 0.57 | +0.24 |
| 0.65 – 0.80 | 21 | 0.76 | 0.73 | +0.04 |
| 0.80 – 1.00 | 33 | 1.00 | 0.91 | +0.09 |

The top three bands are well calibrated or slightly conservative, which is the
right direction to be wrong in. The bottom band is not, and that is precisely
why `detect.DEFAULT_MIN_CONFIDENCE` is 0.50 — the band below it is almost
entirely noise.

That threshold was chosen by sweeping it, not by taste:

| Minimum confidence | Precision | Recall | F1 | FP/km² |
|---:|---:|---:|---:|---:|
| 0.35 | 0.69 | 0.82 | 0.75 | 0.142 |
| 0.45 | 0.82 | 0.81 | 0.81 | 0.071 |
| **0.50** | **0.85** | **0.81** | **0.83** | **0.055** |
| 0.55 | 0.91 | 0.74 | 0.81 | 0.027 |

At 0.50 precision rises by sixteen points and false positives more than halve,
while recall falls by one point — everything discarded was noise. Push to 0.55
and real detections start going with it.

## The neighbourhood a detection is compared against

`detect.edge_drop` asks whether a candidate is sharper than its surroundings,
which requires deciding what "surroundings" means. The code grows a ring
outward from the component's boundary starting one cell out, so the shell
immediately adjacent to the object is included in the comparison.

That is untidy — the adjacent shell is partly the object's own soft edge, and
excluding it to make a clean annulus is the more defensible-sounding choice.
It measures worse. Same command, same window, the only difference being
whether the adjacent shell is in the ring:

| Neighbourhood | Precision | Recall | F1 | FP/km² |
|---|---:|---:|---:|---:|
| **From one cell out** | **0.85** | **0.81** | **0.83** | **0.055** |
| Clean annulus | 0.85 | 0.78 | 0.81 | 0.055 |

The three points of recall are entirely at Mundra (0.77 → 0.74) and Sardar
Sarovar (0.67 → 0.50), and nothing at all at Bhadla or Kutch — the two sites
with water and the two without. Against water the adjacent shell is dark and
carries most of the contrast; remove it and the sharpest edge in the scene is
the one no longer being measured.

This is recorded because the original code arrived at the wider ring by
accident rather than by design, and a later cleanup removed it as obviously
redundant. It was not redundant. `detect.EDGE_RING_INNER` now names it, so
narrowing it again is a decision with a number attached.

## Change detection

Scored by whether the scripted events in `world.py` are recovered, in
`tests/test_ts_recovery.py`. Every one of these is an assertion that runs in CI:

| Event | Truth | Recovered |
|---|---|---|
| Bhadla array block commissioned 2026-03-19 | 228,800 m² | Found, typed `new_structure`, >80,000 m² above the cloud-free portion, confidence ≥ 0.70, severity ≥ medium |
| Sardar Sarovar drawdown from 2026-04-07 | ~1.8 km² of exposed bank | Found, >500,000 m², explanation names the water transition |
| Kutch building through the monsoon | 44 × 30 m, appears 2026-08-11 | Found on SAR, typed structural, in a window with ≤2 usable optical scenes |
| Mundra vessel turnover | dozens per fortnight | Routed to `movements`, counted as arrivals and departures, never filed as change |

And seven negative assertions, each covering a failure that was observed and
fixed during development:

- A new photovoltaic block is **not** reported as inundation. Panels are as dark
  as water in the visible bands; the provider's scene-classification band is
  what separates them. Before that fix it was reported as 11.7 hectares of
  flooding, at high severity, on a desert energy site.
- A Sentinel-2 / Landsat-9 pair is **refused**, not differenced.
- A SAR pair from two ground tracks is **refused**, not differenced.
- Co-registration recovers a deliberate two-cell shift exactly.
- Cloud in one scene only does **not** decide where the other scene sits.
- The Sardar Sarovar pair of 2026-02-26 / 2026-03-30 reports **nothing**.
- A flat search surface yields **no shift** rather than the argmin of noise.

### The registration failure those last three pin

This one is worth spelling out, because it is the most expensive class of false
positive in change detection and it does not look like a bug in review.

Co-registration scored cloud as though it were ground. Cloud is bright, it is
in one scene and not the other, and it does not move with the terrain, so it
adds a large near-constant term to every candidate shift. On a Landsat-9 pair
over Sardar Sarovar with 6% cloud before and 29% after, the seven candidate row
shifts scored between 0.12108 and 0.12186 — a spread of 0.06%. The minimum of
that is noise. It was applied as a two-row shift, and a scene displaced by two
rows puts a bright rim along one side of every static structure and a dark rim
along the other.

The result was five changes at a site where nothing had been built: four of
them hugging the edges of the dam wall and the powerhouse, one of them reported
at **high severity** as "a uniform engineered surface has replaced a varied
natural one". It is a dam. It had been there the whole time.

The function already computed the number that exposes this — it returned a
registration quality of 0.0057, meaning the best alignment was half a percent
better than no alignment at all, and its own docstring called that suspicious.
Nothing read it.

| Configuration | Exact | Within 1 cell | Worse than no shift |
|---|---:|---:|---:|
| No mask, no guard | 64/81 | 79/81 | **3/81** |
| Cloud-masked only | 70/81 | 80/81 | 2/81 |
| **Cloud-masked + quality guard** | 62/81 | 78/81 | **0/81** |

Measured over 81 same-sensor, same-track pairs across the four sites, against
the geolocation error the sensor model actually injected. The guard costs eight
exact matches — all SAR pairs left with a one-cell residual that the 3×3
multi-look already softens — and removes every case where the pipeline made a
pair *worse* than leaving it alone. That is the right side of the trade, because
a scene displaced on no evidence does not produce a slightly worse answer; it
produces confident structures along every edge in the scene.

## Coverage, which is the number that governs all the others

| Site and season | Acquisitions | Usable | Longest gap |
|---|---:|---:|---:|
| Bhadla, Jan–Mar (dry) | 39 | 31 (79%) | 6 days |
| Mundra, Jun–Sep (monsoon) | 53 | 24 (45%) | 6 days |

In the monsoon window at Mundra, 21 of 24 usable acquisitions are Sentinel-1
and 3 are Sentinel-2. Optical monitoring of a monsoon coast delivers roughly one
usable look a month for four months of the year.

This is the single most important number in the document, and it is not a
detection metric. It means a customer buying optical-only monitoring of a
monsoon site is buying a service that stops working in June. It is why the
pipeline falls back to SAR, why `change.py` has a separate log-ratio operator
and multi-looking for it, and why every report leads with coverage before
findings.

## What is not measured

- **Latency.** Nothing is benchmarked as a service-level objective. Indicative
  only: a 36 km² site at 10 m takes a few seconds per scene end to end in pure
  Python, and that is a number to replace with numpy and a work queue rather
  than to quote.
- **Change-detection precision as a rate.** The recovery tests assert that
  specific known events are found and specific known confusions are not. There
  is no labelled change corpus large enough to give a precision figure, which
  would need hand-labelled change polygons across many scenes.
- **Anything at all on real imagery.** See the caveat at the top.
