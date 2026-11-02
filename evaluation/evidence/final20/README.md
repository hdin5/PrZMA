# Cache Recovery final-20 evidence

This directory is the portable evidence package for the fixed 20-run live
campaign and its paired replay.

## Layout

- `selection.json` identifies the 20 included source run IDs.
- `live_runs/<run_id>/` contains each live run's raw actions, snapshot ZIP,
  ChromeCacheView export, extracted cache objects, comparison, manifests,
  metrics, TMI records, and provenance.
- `replay_runs/run_<replay_id>/` contains the corresponding model-free action
  replay records and Research Archive request evidence.
- `summary/` contains aggregate JSON/CSV metrics, run tables, and paired-replay
  summaries.
- `SHA256SUMS` inventories every packaged evidence file with its SHA-256 digest.

## Verify the package

From the repository root:

```powershell
.\przma_cache_evaluation.ps1 -Mode Verify -Evidence .\evaluation\evidence\final20
```

Verification checks the selected population, required raw evidence, portable
metadata, snapshot ZIP membership, package exclusions, published replay
coverage, and all SHA-256 hashes.

## Published results

- 20 live runs and 20 paired replay runs.
- 299 of 300 verified behavior outcomes.
- 579 of 580 generated target families.
- All 579 generated families recovered.
- 100% paired replay family reproduction.

Run-level summaries and aggregate metrics are available in the JSON and CSV
files under `summary/`. The raw run evidence remains available for independent
analysis.
