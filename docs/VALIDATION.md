# Validation record

Validation date: 2026-09-03.

## Release construction

- The repository was cloned from an empty public remote and populated from a
  working-tree export; no private Git history was imported.
- The unlicensed `academic-writing-refiner` tree was excluded before the first
  commit.
- The Apache-2.0 `LICENSE` is byte-for-byte identical to GitHub's canonical
  Apache-2.0 license body.
- Git author configuration uses the repository owner's GitHub noreply address.
- Concrete machine paths, Feishu destinations, credentials, runtime state,
  papers, figures, and generated reports are excluded.

## Environment and dependencies

- Python: 3.12.13 in a fresh repository-local virtual environment.
- Requests: 2.34.2.
- lxml: 6.1.1.
- Pillow: 12.3.0.
- Seven resolved Python packages are pinned with 401 PyPI distribution hashes
  in `requirements.lock`.
- `pip check`: passed.
- OSV query for all seven locked versions: no known vulnerabilities returned on
  2026-09-03.
- Poppler smoke environment: 26.04.0. draw.io is not executed by the builder.

## Automated checks

- `scripts/verify_bundle.py`: passed for all public candidate and required files.
- `scripts/security_audit.py`: passed with no findings; one documented warning
  remains because the trusted local OpenClaw job needs `exec`.
- A temporary fake OpenAI-style token caused the security gate to fail with
  exit code 1 and the expected file-level finding; the fixture was then removed.
- `scripts/check_open_source_readiness.py`: `READY_FOR_OWNER_REVIEW`, no blockers.
- Python compilation and `bash -n scripts/bootstrap.sh`: passed.
- Offline unit suite: passed, including:
  - topic priority and deduplication;
  - bounded XML responses and category validation;
  - pre-request redirect rejection;
  - raster decoder, pixel/frame, SVG, and entity restrictions;
  - report-root and symlink confinement;
  - vertical draw.io plus directly generated SVG;
  - strict review schema and Chinese-primary output;
  - CSP/no-referrer HTML and build receipt;
  - one-read sender dry-run, private target resolution, and destination-scoped
    idempotency;
  - private runtime rendering, reduced cron tools, and explicit trust acknowledgement.
- Skill Creator validation: `academic-style-baseline` valid.
- ClawDefender prompt checks: cron prompt, review policy, and the new baseline
  returned clean. This is supplementary; the repository does not treat the
  pattern scanner as a security boundary.

## Network probes

The opt-in probe used a contact-bearing project user agent and the required
delay. On 2026-09-03:

- arXiv API: HTTP 200, 3,360 decoded bytes;
- arXiv RSS: HTTP 200, 48,499 decoded bytes.

No Feishu message was sent and no live OpenClaw cron job was created or edited
while validating this public repository.

## Post-commit and remote checks

Before publication, the final reachable Git history must pass the offline
credential scan, local `HEAD` must equal `origin/main`, and the GitHub Actions
CI run for that commit must succeed. The final handoff records those results.
