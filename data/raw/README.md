# `data/raw` folder

This folder is intended for **raw data** related to the company: dumps, exports, sample files, event samples, or untransformed datasets.

- **Main purpose**: serve as a landing zone or reference for original data before pipelines process it.
- **Recommendation**: document each dataset’s origin, format, expected size, privacy/PII considerations, and how it is versioned (ideally avoiding sensitive data in the repository).

## In this folder

- `incidents-trackflow.csv`: the helpdesk export that `scripts/seed_incidents.py` seeds from.
- `prefect-results/` (git-ignored, created by the pipeline): Prefect's persisted results of the two transform tasks, the intermediate files behind the 1-hour transform cache. They hold counts and excluded event ids, never payloads or user ids, and can be deleted at any time.

> _Spanish version: [README.es.md](./README.es.md)._
