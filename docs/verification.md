# Verification log

Environment: Debian 12 container, Python 3.11.2, SQLite 3.40.1, Flask 3.1.3,
gunicorn 23.0.0. Docker CLI/daemon and container runtime are **not** available
in this sandbox (no `/var/run/docker.sock`, no privileges to start `dockerd`),
so the Compose stack was validated structurally (YAML + configuration), and
the same image command line was exercised with gunicorn directly (see
"Live server"). The Compose launch itself remains unverified here.

## Automated tests

```
python3 -m pytest -q     ->  63 passed
```

Covered behaviours:

* closest valid g/r pair wins; 30:00 boundary is paired, 30:01 is not;
* an invalidated primary exposure falls back to the alternate pair;
* losing every candidate pair drops that grid/night, and the grid flips to
  `needs_more` once it falls below two usable nights;
* restoring an exposure restores the original closest pair;
* one exposure covering three grids rechecks exactly those three grid/night
  pairs (and leaves untouched rows byte-identical);
* imports are all-or-nothing: a contradicting row aborts the batch, leaving no
  partial exposure;
* two writers on separate connections toggling *different* exposures both
  commit, and the resulting coverage reflects both changes;
* one failing correction plus one succeeding correction: the failure is
  reported, the success commits, and a fresh reader sees the committed state;
* HTTP: upload, error reporting, toggle redirect + coverage flash, CSRF
  rejection, JSON API, health endpoint.

Photometry / comparison (this round, 36 new tests):

* parser rejects `nan`/`inf`/non-numeric magnitudes, non-positive or
  non-integer photometry versions, missing columns and blank key fields;
* first five columns are the key: same key + same value is stored once,
  same key + different value is a row-level error;
* exposure that does not exist or does not cover the grid rejects the whole
  batch; a valid row before the bad one is rolled back too;
* conflicting magnitude against stored data rejects the batch and leaves every
  stored magnitude byte-identical;
* comparison uses the exposures from the *current* `coverage` pair and the
  highest common photometry version with all four magnitudes;
* an incomplete newer version falls back to the last complete shared version;
* no common version / unpaired night / same night / unknown night produce a
  "暂不可比" reason instead of a number;
* invalidating the g exposure of the pair switches to the alternate pair; the
  comparison then becomes not-comparable, and importing photometry for the new
  pair makes it comparable again (version choice re-run);
* web page renders the comparison entry, upload form, exposures, version,
  both g−r values and the signed delta.

## End-to-end script

`python3 scripts/verify_colour_comparison.py` on a temp DB loaded from
`samples/survey_coverage.csv` + `samples/photometry*.csv`:

```
import v1+partial v2: {'inserted': 7, 'skipped': 0}
[v2 未齐 -> 应回退 v1]  version=1  N1 g-r=0.5000  N2 g-r=0.6000  Δ=+0.1000
import complete v2: {'inserted': 1, 'skipped': 3}
[v2 已齐 -> 应用 v2]    version=2  N1 g-r=0.7000  N2 g-r=0.8500  Δ=+0.1500
[换配对后 -> 应暂不可比] 找不到可用共同版号（四次曝光的星等不齐），暂不可比
import v3 for new pair: {'inserted': 4, 'skipped': 0}
[新配对补齐 v3 -> 应可用] version=3 N1(g=E1001/r=E1002) g-r=0.7000  N2 g-r=0.4500  Δ=-0.2500
conflict batch: 整批退回；已入库星等保持: 20.1
```

## Live server (gunicorn, 2 workers x 2 threads, real HTTP + CSRF)

Started as the Dockerfile would run it (port 8080, `/data`-style DB dir):

1. `/healthz` -> 200; form upload of `samples/survey_coverage.csv` with the
   CSRF token -> `新增曝光 8 次，新增覆盖关系 12 条`.
2. Uploaded `samples/photometry.csv` on `/grids/G01` -> `新增 7 行`.
3. `GET /grids/G01?target=T001&first=N2026-10-01&second=N2026-10-02`:
   version 1, N1 E1003/E1004 g−r 0.5000, N2 E1101/E1102 g−r 0.6000,
   Δ = +0.1000.
4. Uploaded `samples/photometry_complete_v2.csv` -> `新增 1 行，跳过同键同值 3 行`;
   re-fetch -> version 2, g−r 0.7000 / 0.8500, Δ = +0.1500.
5. `POST /exposures/3/validity valid=0` (E1003) with the comparison URL as
   `next` -> `曝光 3 已改为已剔除`; re-fetch -> 暂不可比（找不到共同版号）,
   because the night now pairs E1001/E1002.
6. Uploaded `samples/photometry_alt_pair.csv` -> `新增 4 行`; re-fetch ->
   version 3, E1001/E1002 g−r 0.7000, N2 g−r 0.4500, Δ = −0.2500.
7. Restored E1003 -> comparison back to E1003/E1004 version 2, Δ = +0.1500.
8. `samples/photometry_conflict.csv` -> `测光导入被拒绝，已入库的星等未改动` and
   the DB still holds 20.1 (not 99.999). Live `nan` magnitude and version `0`
   were rejected at parse time; an exposure not covering the grid was rejected
   with `不存在或未覆盖网格 G02`.

## Compose validation

* `docker-compose.yml` parsed and checked: service `coverage-review` builds `.`,
  publishes `8080:8080`, sets `COVERAGE_DB=/data/coverage.sqlite3`, mounts the
  named volume `coverage-data` at `/data`, injects `SECRET_KEY`.
* `Dockerfile` uses the same command line exercised above (`gunicorn ... wsgi:app`),
  a `/healthz` healthcheck, and a non-root `survey` user owning `/data`.

## Not verified here

* `docker compose up --build` and volume persistence across container restarts
  were not executed (no Docker daemon / container privileges in this sandbox).
* No real login/audit actor separation (unchanged from the previous round).
* Large photometry batches are still row-by-row (fine for the tens-of-thousands
  scale); no bulk path yet.
