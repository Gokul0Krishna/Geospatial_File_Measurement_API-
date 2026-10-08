# Geospatial File Measurement API

A backend service (FastAPI, no frontend) that accepts a **Shapefile (`.zip`)** or a **KML** file,
extracts every feature, and returns **area** (polygons) and **length** (lines) with correct
coordinate-system handling.

* Never measures in degrees: every feature is reprojected to its UTM zone (UPS near the poles) first,
  and an independent **geodesic** value on the WGS84 ellipsoid is stored next to it as a cross-check.
* Handles geographic *and* projected sources (a Web Mercator file at 60°N is reported with its true
  area, not the ~4× inflated planar one).
* One bad feature never fails a file; unsupported geometries are reported, not crashed on.
* Built to be robust under failure: crash-safe retries, soft deletes, backpressure, zip-bomb and
  size-cap protection, a sweeper for stuck or orphaned data.

---

## 1. Setup

### Local, zero infrastructure (SQLite, inline processing)

Requires Python 3.11+. GDAL, GEOS and PROJ all ship inside the Python wheels; nothing to install system-wide.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make dev                      # = uvicorn app.asgi:app --reload --port 8000
```

Open <http://localhost:8000/docs> (interactive OpenAPI), or try the bundled samples:

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
curl http://localhost:8000/api/files/<id>/measurements/
make smoke                    # uploads all samples to a running instance and checks the numbers
```

Config comes from environment variables or a `.env` file; `.env.example` lists every setting.

### Full production-shaped stack (Docker)

```bash
docker compose -f docker/docker-compose.yml up --build      # http://localhost:8080/docs
docker compose -f docker/docker-compose.yml up --scale worker=3
```

nginx → API → Postgres, Redis ← arq workers, plus a one-shot `migrate` service
(`alembic upgrade head`). In this mode uploads return `202 PENDING` and workers process them.

### Without Docker, but with workers

```bash
export PROCESSING_MODE=worker REDIS_URL=redis://localhost:6379/0
make dev            # terminal 1
make worker         # terminal 2  (arq app.workers.settings.WorkerSettings)
```

### Tests

```bash
make test           # 119 tests on SQLite (a Redis-backed test skips itself)
make lint           # ruff
TEST_DATABASE_URL=postgresql+asyncpg://u:p@localhost/test pytest        # same suite on Postgres
TEST_REDIS_URL=redis://localhost:6379/15 pytest tests/integration/test_worker_mode_redis.py
```

---

## 2. API

Interactive docs at `/docs`. All errors are RFC 7807 `application/problem+json` carrying a `request_id`
that is also returned in the `X-Request-ID` header (send your own to correlate).

| Method & path | Purpose |
|---|---|
| `POST /api/files/` | Upload a `.zip` (Shapefile) or `.kml`. **201** with the final status in inline mode, **202** `PENDING` in worker mode. |
| `GET /api/files/{id}/` | File info and processing status. |
| `GET /api/files/{id}/measurements/` | Per-feature measurements (paginated). |
| `GET /api/files/{id}/features/` | Per-feature geometry, CRS, properties and measurement (paginated). |
| `GET /api/files/` | List files, newest first (paginated). |
| `POST /api/files/{id}/retry/` | Re-run a `FAILED` file. |
| `DELETE /api/files/{id}/` | Delete a file and its features (asynchronous, **202**). |
| `GET /health`, `GET /ready` | Liveness; readiness (database, storage, Redis in worker mode). |

### Upload

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
```

```json
{
  "id": "01a117d6-aef6-7c38-92ce-fb645e3ff2e0",
  "filename": "survey.kml",
  "source_format": "kml",
  "size_bytes": 1837,
  "status": "COMPLETED",
  "feature_count": 6,
  "processed_features": 6,
  "crs": "EPSG:4326",
  "error": null,
  "warnings": [],
  "summary": {
    "by_status": {"MEASURED": 3, "NOT_APPLICABLE": 1, "UNSUPPORTED": 1, "EMPTY": 1},
    "by_geometry_type": {"Polygon": 1, "MultiPolygon": 1, "LineString": 1, "Point": 1, "GeometryCollection": 1},
    "total_area_m2":   {"utm": 2667826.77, "geodesic": 2664657.13},
    "total_length_m":  {"utm": 4636.78,    "geodesic": 4634.07}
  },
  "created_at": "2026-10-07T19:28:27.900105Z",
  "updated_at": "2026-10-07T19:28:27.933108Z",
  "completed_at": "2026-10-07T19:28:27.932029Z"
}
```

In worker mode the same call returns `202` with `"status": "PENDING"` and `feature_count: null`;
poll `GET /api/files/{id}/` until it is `COMPLETED` or `FAILED` (`error` explains why).

### Measurements

`GET /api/files/{id}/measurements/?limit=2` (trimmed):

```json
{
  "file_id": "01a117d6-aef6-7c38-92ce-fb645e3ff2e0",
  "method": "utm",
  "crs": "EPSG:4326",
  "count": 2,
  "next_cursor": "eyJhZnRlciI6MX0",
  "items": [
    {
      "feature_index": 0, "geometry_type": "Polygon", "status": "MEASURED", "reason": null,
      "measurement": {"type": "area", "value": 865213.47, "unit": "m2", "method": "utm", "crs": "EPSG:32643"},
      "warnings": []
    },
    {
      "feature_index": 1, "geometry_type": "MultiPolygon", "status": "MEASURED", "reason": null,
      "measurement": {"type": "area", "value": 1802613.30, "unit": "m2", "method": "utm", "crs": "EPSG:32643"},
      "warnings": []
    }
  ]
}
```

(The real response also carries the file `summary`, omitted here for space.)

| Query parameter | Meaning |
|---|---|
| `method` | `utm` (default; planar in the feature's UTM zone) or `geodesic` (ellipsoidal). Both are stored, so switching is free. |
| `limit`, `cursor` | Keyset pagination. Default 100, max 1000 (max 100 with geometry). Follow `next_cursor` until it is `null`. |
| `status` | Only features with this per-feature status (e.g. `UNSUPPORTED`). |
| `include_geometry` | Also return each GeoJSON geometry (in the file's CRS). Pages are additionally capped at 2 MiB of geometry. |

`GET /api/files/{id}/features/` returns the same items plus `crs`, `properties` and `geometry`
(`include_geometry` defaults to `true` there). A file that is not `COMPLETED` answers **409**
(`Retry-After` while it is still running).

### Per-feature status

| Status | Meaning | `measurement` |
|---|---|---|
| `MEASURED` | Area/length computed | yes |
| `REPAIRED` | Invalid polygon (e.g. self-intersecting) fixed with `make_valid`, then measured; `reason` explains | yes |
| `NOT_APPLICABLE` | Points | `null` |
| `UNSUPPORTED` | e.g. a `GeometryCollection` mixing points and lines | `null` |
| `EMPTY` | Null/empty geometry | `null` |
| `INVALID` | Invalid and not repairable | `null` |
| `ERROR` | Reprojection/decoding failed (e.g. coordinates outside the declared CRS's range) | `null` |

Feature warnings: `LARGE_EXTENT` (> 3° wide; UTM distortion grows with extent), `SPANS_MULTIPLE_UTM_ZONES`,
`POSSIBLE_ANTIMERIDIAN_CROSSING`, `POLAR_REGION`. File warnings: `CRS_ASSUMED:<crs>`, `NO_FEATURES`.

File status: `PENDING → PROCESSING → COMPLETED | FAILED`, plus `DELETING` (invisible to clients).

### Error examples

```json
{"type": "urn:geo-measure:problem:unprocessable-upload", "title": "Unprocessable upload", "status": 422,
 "detail": "Shapefile has no .prj file, so its coordinate reference system is unknown. ...",
 "instance": "/api/files/", "request_id": "…"}
```

| Status | When |
|---|---|
| 400 | Not a zip / not KML, unsafe or incomplete archive, bad cursor |
| 404 | Unknown file |
| 409 | File not `COMPLETED`; retry of a non-`FAILED` file |
| 413 | Over the size cap (also enforced on chunked uploads) |
| 415 | Not `.zip` / `.kml` |
| 422 | Shapefile without `.prj` (strict mode), invalid parameters |
| 429 / 503 | Too many of your files in progress / service saturated (both send `Retry-After`) |

---

## 3. Architecture

```
app/
├── main.py, asgi.py          app factory + lifespan (wiring), ASGI entrypoint
├── core/                     config, ids (UUIDv7), logging, exceptions (RFC 7807), pagination
├── api/                      routes (files, health), deps, middleware, presenters
├── schemas/                  Pydantic request/response models and enums
├── models/                   SQLAlchemy models: UploadedFile, Feature
├── db/                       engine/session, column types (the only DB-specific code), alembic migrations
├── repositories/             ALL SQL lives here (file_repo, feature_repo)
├── services/                 upload, processing, lifecycle (delete/retry/sweeper), purge
├── geo/                      PURE logic: crs, geometry, measure, readers/  (no DB, no HTTP)
├── storage/                  Storage interface + local-disk implementation
├── workers/                  arq tasks/settings + job dispatchers (inline | arq)
└── utils/zip_safety.py       archive validation and extraction
tests/  scripts/  samples/  docker/
```

Dependencies point inward: `routes → services → repositories / geo / storage`. `geo/` imports
nothing from the rest of the app, so the measurement logic is unit-tested with plain shapely objects.

### File-processing flow

1. **Upload.** Backpressure check (global and per-client in-flight caps). The body is counted *as bytes
   arrive* by an ASGI wrapper (Starlette spools multipart bodies to disk before a handler runs, so a
   handler-level check alone cannot stop a chunked upload from filling the disk) and cut off with 413 at the
   cap. It is then copied to a staging file in chunks, hashed, with a second cap (tighter for KML).
2. **Validate cheaply.** Magic bytes; KML must contain `<kml` and no `<!ENTITY`; a shapefile zip is inspected
   *without extracting*: zip-slip paths, duplicate names, encrypted members, entry count, declared size,
   per-entry compression ratio, exactly one complete `.shp/.shx/.dbf` set, `.prj` present (strict mode).
3. **Store + enqueue.** The staged file is atomically moved into storage, a `PENDING` row is inserted
   (the blob is removed again if that fails), and the job is dispatched. If the queue is down the upload is
   rolled back and the client gets 503.
4. **Process (inline or in an arq worker).** Claim the file with a conditional `UPDATE`, wipe any rows left
   by an earlier attempt, extract only the needed shapefile parts under fixed names, and run a cheap
   "can GDAL open this" check so bad files fail in seconds. Then stream features through Arrow record batches
   (single pass, bounded memory). Per batch: decode WKB, measure, and insert features **plus** the progress
   update in one transaction. Feature-count and vertex budgets are enforced before/while reading.
5. **Complete.** One final `UPDATE` sets `COMPLETED` with feature count, CRS and aggregate totals.
   Clients can never see partial data: the API only serves features of `COMPLETED` files.

### Measurement flow

1. Classify each geometry; flatten homogeneous `GeometryCollection`s (KML `<MultiGeometry>`) into `Multi*`;
   drop Z.
2. Validate; repair invalid polygons with `make_valid` and keep only the polygonal part.
3. Reproject the batch to WGS84, in one vectorised call (with a per-geometry fallback so one bad geometry
   cannot sink a batch).
4. Pick each feature's UTM zone from the centre of its extent; group features by zone; reproject each group
   and compute `shapely.area` / `shapely.length`.
5. Independently compute the geodesic value (pyproj `Geod`, holes subtracted, per-ring) and store both.
6. Roll the numbers into the file's `summary` totals as batches stream by (so reads stay O(1)).

### CRS handling

* **KML** is WGS84 by specification. **Shapefile** CRS comes from the `.prj`. No readable CRS → rejected
  (`REQUIRE_PRJ=true`, default) or assumed (`REQUIRE_PRJ=false`, flagged with a `CRS_ASSUMED:` warning).
* All transformers use `always_xy=True` (EPSG:4326 is lat/lon by authority definition; without this the axes
  silently swap) and are cached per thread.
* **Always reproject, even when the source is projected.** A "projected" CRS can be non-metric (feet) or
  badly non-equal-area: Web Mercator inflates areas by 1/cos²(lat), about 4× at 60°N.
* Target CRS: UTM (EPSG:326xx north / 327xx south) from the feature's extent centre; UPS (EPSG:32661/32761)
  beyond 84°N / 80°S. Longitudes wrap at the antimeridian.
* Sanity checks: a "geographic" file whose coordinates are outside ±180/±90 is flagged `ERROR` (the declared CRS
  is wrong); non-finite reprojection results are `ERROR`.
* Measured accuracy: UTM vs geodesic agree to within ~0.05–0.12 % for features a few degrees from the central
  meridian (UTM's scale factor), and the service warns when a feature is wide or crosses zones.

### Reliability model

* **Idempotent, crash-safe retries:** claim → wipe-and-redo → per-batch transactions → single completion
  `UPDATE`. A crash mid-file leaves invisible partial rows; the retry wipes them and redoes the file. The
  composite primary key `(file_id, feature_index)` makes any accidental duplicate insert a hard error.
* **Retries only for transient errors** (exponential backoff, capped); corrupt files fail immediately with a
  user-facing message. Job deadline recorded as a clear `FAILED` reason. `FAILED` doubles as the dead-letter
  state and can be re-run with `POST …/retry/`.
* **Cancellation:** the per-batch progress `UPDATE` only matches while the file is `PROCESSING`, so a delete
  stops the job at the next batch boundary and the worker purges the data itself.
* **Soft delete:** `DELETE` flips the file to `DELETING` (instantly invisible); idle files are purged in the
  background; a file being processed is purged by its worker. Nothing is unlinked under a reader.
* **Sweeper** (arq cron, or a loop in the API in inline mode; idempotent, safe to run on many hosts): fails
  files stuck in `PENDING`/`PROCESSING`, purges `DELETING` files past a grace period and `FAILED` files past
  retention, and removes stale staging files and orphaned blobs.

---

## 4. Design decisions and alternatives

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Reader | **pyogrio** (GDAL) + Arrow batches for both formats | GeoPandas/Fiona; pyshp + fastkml | One code path, single-pass streaming, bounded memory. Pure-Python parsers avoid GDAL but need two parsers and are less complete. |
| KML layers | Iterate **all** GDAL layers (each `<Folder>` is one) | Read the default layer | Reading only the first layer silently drops most real-world KML. `feature_index` runs across layers. |
| Measurement CRS | **UTM** per feature + stored **geodesic** | One global equal-area CRS; geodesic only | The brief asks for a projected CRS; UTM is accurate and familiar. Geodesic is the verifiable ground truth, and `?method=` exposes both. A local equal-area CRS would be better for very large features (see future scope). |
| Projected sources | Always reproject | Trust the source CRS | See "CRS handling": trusting Web Mercator is wrong by 4× at 60°N. |
| Processing | `inline` **or** arq worker, one code path | Celery; FastAPI `BackgroundTasks`; sync only | arq is async-native and light. `BackgroundTasks` has no retries or durability. The inline switch lets a reviewer run everything with no Redis. |
| Retry semantics | **Wipe-and-redo**, commit per batch | Upsert keyed on feature index | Simpler, always correct; also avoids `ON CONFLICT` vs `ON DUPLICATE KEY` differences between databases. |
| Geometry storage | **WKB** bytes in the source CRS (`BYTEA` / `LONGBLOB`) | PostGIS geometry; GeoJSON in JSONB | WKB is compact and round-trips with shapely for free; GeoJSON is built only at the API edge. **PostGIS is not used**: no spatial queries are required yet and it would make the suite depend on a PostGIS server. The storage type lives in one module (`db/types.py`), so adding PostGIS later is a contained migration. |
| Database | **PostgreSQL** (tested); SQLite for local runs | MySQL | See below. |
| Measurements | Computed once at processing time, persisted (both methods) | Compute on every GET | Reads stay cheap and consistent; the cost is a little storage. |
| Pagination | **Keyset** cursors, hard caps, byte budget on geometry pages | Offset/limit | Offset degrades as tables grow; keyset cost is independent of depth. |
| IDs | **UUIDv7** | UUIDv4, serial ints | Time-ordered (keyset on the PK alone), non-guessable, index-friendly. |
| Errors | RFC 7807 problem+json | Ad-hoc JSON | Standard, machine-readable, with request-id correlation. |
| Size cap | ASGI wrapper counting bytes + staged-copy check | `Content-Length` check only | Chunked uploads have no Content-Length; Starlette spools bodies before handlers run. |

**MySQL instead of Postgres?** Queries are ORM-level and portable, but it is not a one-line change:
async driver (`asyncmy`), `LONGBLOB` (already mapped in `db/types.py`), `utf8mb4`, `max_allowed_packet`
(inserts are chunked by bytes), DDL that auto-commits, and no PostGIS upgrade path. The schema and types are
prepared (`pip install -e ".[mysql]"`), but **MySQL has not been run against this code**; treat it as unverified.

---

## 5. Security and limits (all configurable, see `.env.example`)

Upload cap 100 MiB (KML 50 MiB); zip entry/size/ratio limits; feature budget 1 M and vertex budget 10 M;
KML entity declarations rejected; filenames are display-only and never used in paths; extraction writes fixed
names so archive paths can't influence disk layout; non-root container user; nginx rate limits uploads.
**No authentication** is implemented (out of scope); per-client limits key on the client IP, so run behind a
trusted proxy (`FORWARDED_ALLOW_IPS`) and add API-key/JWT auth before exposing this publicly.

---

## 6. What has been verified

| Area | Status |
|---|---|
| Unit + integration suite (119 tests) on SQLite | passing |
| Same suite on **PostgreSQL 16**; Alembic `upgrade → check → downgrade → upgrade` | passing (columns map to `bytea`, `jsonb`, `double precision`, `timestamptz`) |
| Worker mode against a **real Redis**: API + `arq` CLI worker as separate processes, `scripts/smoke_test.py`; plus a Redis-backed pytest | passing |
| Inline mode end-to-end over HTTP with uvicorn | passing |
| Mutation checks: removing wipe-and-redo or the cancellation check makes the matching tests fail | confirmed |
| `ruff` | clean |
| **MySQL** | **not run** |
| **Dockerfile, docker-compose, nginx.conf** | **written and syntax-checked only; never built or started** |

Tests cover, among others: known-answer areas/lengths against an independent geodesic calculation, hole
subtraction, projected sources, repair/unsupported/empty geometries, zip-slip/zip-bomb/duplicate entries,
chunked-upload cutoff, pagination and caps, backpressure, crash-then-retry without duplicates, delete during
processing, the sweeper, and the arq retry/backoff/deadline policy.

---

## 7. Known limitations

* UTM distortion grows with extent. Features wider than ~3° or spanning zones get warnings; use
  `method=geodesic` for those.
* KML has no random access, so its feature count is only known after a full parse; the vertex budget is
  enforced while streaming instead.
* Local-disk storage must be shared between API and workers (one host, or a shared volume) until an S3
  backend exists.
* GeoJSON geometry is returned in the file's own CRS (stated in `crs`) with Z preserved, not reprojected.
* Geometries are never snapped or simplified; invalid polygons are repaired only for measurement, the stored
  geometry stays as uploaded.

## 8. Learnings

* **pyproj's `Geod.geometry_area_perimeter` is orientation-dependent for holes**: on a test polygon it
  *added* the hole (1,080,263 m²) instead of subtracting it (864,210 m²). Rings are now measured individually
  with `abs()` and subtracted; a regression test pins this.
* **GDAL's KML driver exposes every `<Folder>` as its own layer** and emits placeholder style columns; both
  need handling or data silently disappears or clutters properties.
* **Starlette parses multipart bodies before your handler runs**, so size limits must live at the ASGI layer.
* **RFC 7807 members must be protected from extension members**: an `extra={"status": …}` once overwrote the
  HTTP status in the response body. Caught by a test and fixed.
* Web Mercator is a measurement trap, and EPSG:4326 axis order is a classic bug; both have dedicated tests.
* Making processing retry-safe (wipe-and-redo, per-batch transactions, conditional state transitions) was
  simpler and more portable than making it resumable.

## 9. Future scope

* PostGIS (spatial indexing, bbox/intersects queries, in-database measurement) and an S3/MinIO storage backend.
* Webhook/callback on completion (needs SSRF protection, signing and retries), so clients need not poll.
* More inputs/outputs: KMZ, GeoJSON, GeoPackage; CSV/GeoJSON export of measurements.
* Authentication (API keys/JWT), per-key quotas, and a CI matrix that includes MySQL.
* Zone-aware measurement for very large features (split at UTM boundaries, or a per-feature local
  Lambert azimuthal equal-area CRS), with explicit antimeridian handling.
* Streaming KML parsing for very large files; resumable/presigned uploads.
* Metrics (Prometheus) and distributed tracing (OpenTelemetry).
