# The analyst console

`frontend/` is the TerraShield analyst console: a React single-page application
built to static files and served by the API process at `/console/`. It replaces
nothing yet. The original page at `/` keeps working, so the console can be
adopted screen by screen.

It reads the same HTTP contract the original page and the CLI use. Two read-only
routes were added for it (`/api/sites/{id}/observations` and `/api/providers`);
nothing else on the server changed.

## Running it

### Against a local API, with hot reload

```bash
pip install -e ".[dev]"
bash scripts/build_demo.sh terrashield.db          # the demo estate, ~10 minutes
```

```bash
TERRASHIELD_TOKEN=devtoken terrashield --db terrashield.db --org org-sensegrass-demo serve
```

```bash
cd frontend && npm ci && npm run dev
```

Open <http://localhost:5173/console/> and choose **Token** with `devtoken`.
Vite proxies `/api` to `127.0.0.1:8787` (override with `TERRASHIELD_API`), so the
browser stays on one origin exactly as in production.

The demo estate's data runs from March to August 2026. The analysis window in
the top bar defaults to the last 365 days, which covers it.

### Built, served by the API

```bash
cd frontend && npm ci && npm run build
```

```bash
terrashield --db terrashield.db serve
```

Then open <http://127.0.0.1:8787/console/>. The build in `frontend/dist` is
static and makes no outbound request: no CDN, no web font, no tile server. It
can be built on a connected machine and carried into an air-gapped site.

## How it fits together

| Layer | Where | Notes |
|---|---|---|
| Static serving | `api.py` `console_file`, `CONSOLE_CSP` | Path traversal refused, extension allow-list, client routes fall back to `index.html`, hashed assets cached as immutable |
| HTTP client | `src/api/client.ts` | Same-origin fetch, bearer token in `sessionStorage`, a 401 drops the session |
| Types | `src/api/types.ts` | Written against responses captured from a running server |
| Data hooks | `src/api/hooks.ts` | One TanStack Query hook per route; every period-aware hook reads the global window |
| Auth and period | `src/state/` | Sign-in, sign-up and service token; the analysis window shared by every screen |
| Map | `src/components/WorldMap.tsx` | d3-geo over bundled Natural Earth 1:50m outlines; pan, zoom, draw |
| Orbit | `src/lib/orbit.ts` | SGP4 via satellite.js, in the browser |

### Content Security Policy

The console is served with a stricter policy than the original page:
`script-src 'self'` with no `'unsafe-inline'`, `connect-src 'self'`, and
`frame-ancestors 'none'`. Styles keep `'unsafe-inline'` because the chart
library positions elements with style attributes.

## Screens

| Screen | Routes it reads | What it is for |
|---|---|---|
| Command | `/api/sites`, `/api/alerts`, `/api/changes`, `/api/health` | Estate status: KPIs, map, priority feed, weekly activity, site status |
| Global map | `/api/sites`, `/api/sites/{id}`, `/api/changes`, `/api/objects` | Layer toggles, severity filter, zoom to site |
| Alert queue | `/api/alerts`, `/api/evidence/{id}`, `POST /api/reviews/{id}` | Triage with keyboard review: `j`/`k` move, `c` confirm, `r` reject, `e` escalate |
| Sites | `/api/sites`, `POST /api/sites` | List; create by drawing a polygon or pasting GeoJSON |
| Site page | site, timeline, observations, projection, events, imagery, objects, fields, graph | Eight tabs, one per question an analyst asks of a place |
| Change explorer | `/api/changes`, `/api/imagery` | Filters, before/after scene metadata, change mask in place, review |
| Evidence | `/api/evidence/{id}`, `/api/evidence-path/{node}` | Artefacts with hashes, measurements, gaps, path to the sources |
| Evidence graph | `/api/sites/{id}/graph?node&depth` | Force layout of the subgraph, evidence path highlighted |
| Copilot | `POST /api/copilot` | Cited answers; declined questions are shown as declined |
| Asset intelligence | `/api/changes?limit=5000`, `/api/sites/{id}`, `/api/fields/at` | Location report, portfolio screening from a CSV, event-footprint triage |
| Maritime | `/api/objects`, `/api/sites/{id}/observations` | Vessel detections per pass at port sites |
| Orbit (SDA) | none (client-side) | Ground tracks and overflight windows from pasted TLEs |
| Watchlists, Rules, Reports | their routes | Configuration views; watchlists can be created |
| Sensing limits | `/api/sensing` | What each platform can resolve, and why |
| Data sources | `/api/providers` | Which providers are configured; variable names only |
| Integrations | none | API reference, a read-only request console, webhook verification |
| Admin & audit | `/api/me`, `/api/health`, `/api/audit` | Session, permissions, audit chain |

Controls the role cannot use are disabled or replaced with a sentence saying so,
using the permissions from `/api/me`. The server still enforces every one of
them; the console only avoids offering what will be refused.

## Asset intelligence

The monitoring record read from the point of view of a place someone owns,
insures or is responsible for. Three tabs, all computed in the browser from
TerraShield's own findings:

- **Location report.** A coordinate and a radius in; out come the monitored site
  and named field it falls in, every finding within the radius with its distance,
  severity, review state and evidence link, and an observed-change score. Prints
  to PDF.
- **Portfolio screening.** A CSV of assets (columns matched by name: `lon`/`lng`,
  `lat`, optional `id`, `name`, `value`/`tiv`) is ranked by observed change
  nearby, with the share of portfolio value near high or critical findings, and
  exports back to CSV. The file never leaves the browser.
- **Event footprint.** Outline a flood extent, cyclone track or fire perimeter;
  the findings and portfolio assets inside it are listed, by default limited to
  damage and water types.

Three rules hold across all three, because they are where this kind of product
misleads:

1. **Unobserved is not unchanged.** An asset outside every monitored site has no
   findings because nothing looked there, and is labelled "not monitored", ranked
   below observed assets with the same score.
2. **The score is observed change, not risk.** It sums severity × confidence ×
   proximity (confirmed findings 1.5×, rejected ones excluded) and maps the sum to
   0–100. The full method is printed under every score. There is no hazard model,
   flood zone, valuation or loss estimate behind it.
3. **Truncation is reported.** `/api/changes` now takes `limit` (default 500,
   maximum 5000) and returns `truncated`. A screening run that hit the limit says
   so, rather than reporting the rest of the estate as quiet.

## Integrating another system

1. **Authenticate.** Interactive users sign in with `POST /api/auth/login`.
   Services use a static token configured on the server with `TERRASHIELD_TOKEN`
   and `--org`. Send `Authorization: Bearer <token>`.
2. **Read the queue.** `GET /api/alerts?status=pending&max_priority=2`, then
   `GET /api/evidence/{evidence_id}` for each alert you forward.
3. **Receive alerts instead of polling.** Configure a webhook destination.
   Deliveries carry references only and are signed:
   `X-TerraShield-Signature: t=<unix>,v1=<hex>`, an HMAC-SHA256 over
   `"<t>." + body`. Reject anything older than 300 seconds. Python and Node.js
   verifiers are on the Integrations screen; `delivery.verify` is the reference.
4. **Write back decisions** with `POST /api/reviews/{id}` and
   `{"status": "confirmed" | "rejected" | "escalated", "note": "..."}`. Every
   review is recorded in the audit chain under the token's actor.
5. **Put TLS in front.** nginx, Caddy or the facility gateway. The server speaks
   plain HTTP on localhost by design.

## What is not built

Stated so nobody reads a screen as more than it is.

- **Imagery pixels in the browser.** The API serves findings and geometry, not
  chips. The change viewer shows the mask outline in place and the scene
  metadata. Serving chips needs an evidence-artefact route that applies the
  same `imagery.read` check and audit entry as the CLI.
- **AIS correlation over HTTP.** `ais.py` exists; the Maritime screen documents
  the route it needs.
- **The orbit service.** The Orbit screen computes geometry from TLEs in the
  browser. Catalogue maintenance, manoeuvre detection and conjunction screening
  are PRD FR-8 and need a server.
- **Rule editing, report generation and user management in the console.**
  Available from the CLI today.
- **The copilot's own window.** Copilot questions are answered over periods
  relative to today (for example "the last 30 days"), not the console's analysis
  window. Against the demo estate, whose data ends in August 2026, that can
  return zeros for recent periods. Ask over a longer relative period ("in the
  last 365 days"). Explicit dates in the question are not parsed today: "between
  2026-04-01 and 2026-06-01" falls back to the last 30 days.
