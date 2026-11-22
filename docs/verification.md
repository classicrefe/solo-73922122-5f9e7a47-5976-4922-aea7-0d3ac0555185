# Verification log

Environment: Debian container, Python 3.11.2, SQLite 3.40.1, Flask 3.1.3,
gunicorn 27.0.1. Docker is not installed in this container, so the Compose
stack was reviewed statically and the same command line was exercised with
gunicorn directly (see "Live server").

## Automated tests

```
python3 -m pytest -q     ->  27 passed
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

## Multi-grid invalidation run

`python3 scripts/verify_multi_grid_invalidation.py` on a temp DB loaded from
`samples/survey_coverage.csv`:

```
exposure 2 E1002 (N2026-10-01) covers grids: G01, G02, G03
before: {'G01': ('paired', 'E1003/E1004'), 'G02': ('paired', 'E1001/E1002'), 'G03': ('paired', 'E1001/E1002')}
invalidate changed: True affected grids: ['G01', 'G02', 'G03']
after : {'G01': ('paired', 300), 'G02': ('unpaired', None), 'G03': ('unpaired', None)}
  G01: usable_nights=2 needs_more=False
  G02: usable_nights=0 needs_more=True
  G03: usable_nights=0 needs_more=True
restored: {'G01': ('paired', 'E1003/E1004'), 'G02': ('paired', 'E1001/E1002'), 'G03': ('paired', 'E1001/E1002')}
```

G01 keeps the night through its spare pair; G02 and G03 lose it and become
待补测. Restoring the exposure restores all three pairings.

## Live server (gunicorn, 2 workers x 2 threads)

* `/healthz` -> 200, `/` -> 200.
* Uploaded the sample CSV through the browser form (with CSRF token).
* Fired two concurrent `POST /exposures/<id>/validity` requests for two
  different exposures from separate clients. Both returned 200, both exposures
  ended `valid=0`, and the affected coverage rows (G01/N2026-10-02, G02 and G03
  on N2026-10-01) were all `unpaired` — i.e. both corrections are reflected.

## Repair tool

`scripts/repair_coverage.py --check` detected a hand-corrupted coverage row;
`--db ... ` rebuilt it; a second `--check` reported `coverage cache OK`.

## Not verified here

* `docker compose up` was not executed (no Docker daemon in this environment).
  The Dockerfile/compose file were reviewed by hand: the app listens on 8080,
  the DB lives on a named volume at `/data`, `SECRET_KEY` is injected from the
  environment, and the container healthcheck hits `/healthz`.
