# Research Archive

Research Archive is the public evaluation site for the autonomous cache-recovery experiment. It is a normal research-collection interface, not an action script or coverage oracle.

## Preview

```powershell
python -m evaluation.cache_recovery.research_archive.preview `
  --host 0.0.0.0 `
  --port 8765 `
  --run-id preview
```

Open the printed `/start?run_id=...` URL. The preview process stays in the foreground until `Ctrl+C` is pressed.

## Site definition and manifests

`sites/research_archive.py` contains the data-only site definition: routes,
resource families, fixed reference parameter cases, behaviors, payload
producers, and file roots. `server.py` is generic and accepts any valid
`SiteDefinition`.

The 29 interaction-dependent resource families use the neutral `<journey>.<schema>.<ordinal>` convention across the `Discover`, `Inquiry`, and `Retrieve` journeys. These identifiers describe recovered artifact schemas rather than Research Archive page names. Human-readable routes and behavior descriptions remain in the Manifest so each identifier can still be audited against the server ground truth.

A fixed reference response is a deterministic request and parameter instance
registered for regression testing of the site implementation. These reference
cases are not the denominator of run-level coverage: autonomous runs are scored
from the actual response instances produced by Agent-selected parameters at the
artifact-family level.

The committed catalogs are:

- `resource_manifest.json`: fixed request/response cases and cache identities;
- `behavior_manifest.json`: user-visible behaviors and their resource evidence.

They are repository artifacts only. The public HTTP listener does not serve either catalog, runtime run state, or oracle data.

Regenerate the catalogs after changing any route, payload, script, style, document, image, or cache policy:

```powershell
python -c "from pathlib import Path; from evaluation.cache_recovery.research_archive.manifest import write_public_manifests; write_public_manifests(Path('evaluation/cache_recovery/research_archive'))"
```

The test suite fails if regenerated output differs from the committed files.

## Rebuilding assets

Install the pinned asset-build dependencies, then run the deterministic builder:

```powershell
python -m pip install -r evaluation/cache_recovery/research_archive/requirements-build.txt
python -m evaluation.cache_recovery.research_archive.build_assets
```

Source masters are retained under `static/source/`. The PDF response used in
the final campaign is preserved byte-for-byte as `web-evidence-master.pdf`;
delivery images, the remaining PDF, CSV, and JSON files are rebuilt from the
other masters and ordered source records.

## Validation

```powershell
python -m unittest evaluation.cache_recovery.verification.test_manifest evaluation.cache_recovery.verification.test_server evaluation.cache_recovery.verification.test_assets evaluation.cache_recovery.verification.test_chromium evaluation.cache_recovery.verification.test_process -v
```

The Chromium tests use a new persistent profile, cross-check browser responses with host server logs, and then reopen the same profile to verify physical HTTP `Cache_Data` files and CDP `fromDiskCache` reuse.

## Experimental boundary

These checks validate the evaluation substrate. They do not perform TMI interpretation, autonomous VM execution, Snapshot Engine collection, ChromeCacheView comparison, repeated runs, baseline comparison, or statistical analysis. Controlled cacheability requests in the Chromium preflight are excluded from autonomous-action and coverage metrics.
