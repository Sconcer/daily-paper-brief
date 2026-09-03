# Source manifest

Release preparation date: 2026-09-03.

The repository was created as a clean working-tree export. It does not inherit
the private pipeline's Git objects, author email, runtime state, or unlicensed
skill snapshot.

## Owner-controlled source lineage

| Repository file | Private source role | Public-release treatment |
|---|---|---|
| `arxiv_monitor_phd.py` | Daily arXiv API/RSS monitor | Source-neutral branding, bounded XML, fixed endpoints, private writes |
| `arxiv-monitor-config-phd.example.json` | Personal monitor configuration | Example values only; real configuration is ignored |
| `arxiv_report_pipeline.py` | Asset preparation, review validation, diagrams, HTML | Redirect prevalidation, media limits, path confinement, resource limits, CSP, receipts |
| `ai_writing_metrics.py` | Deterministic writing descriptors | Source snapshot retained under repository Apache-2.0 scope |
| `arxiv_send_html_to_feishu.py` | Feishu HTML attachment sender | Fixed private target file, receipt check, one-read upload, target-scoped idempotency |
| `cron/` | Active OpenClaw prompt, policy, and job definition | Machine paths and destinations replaced by private runtime placeholders |
| `scripts/install_openclaw_cron.py` | Cron setup | Render-only default, private mode-0600 files, reduced tools, explicit trust acknowledgement |
| `tests/` | Private regression tests | Expanded with security-boundary and release-gate coverage |

Current file hashes are intentionally not hand-maintained in this document.
Git provides the immutable release tree hash, while CI and
`scripts/verify_bundle.py` validate the required files.

## Skills

- `skills/academic-style-baseline/` was created for this repository and is
  covered by Apache-2.0.
- `skills/humanizer-zh/` is a separately licensed snapshot. Its MIT license and
  attribution-bearing README are preserved.
- The private, unlicensed `academic-writing-refiner` package is excluded in its
  entirety and never enters this repository's Git history.

## Generated lock data

`requirements.lock` records the exact resolved Python dependency versions and
all distribution hashes published for those releases by PyPI on 2026-09-03.
It was generated mechanically from `requirements.txt` and verified with pip's
`--require-hashes --only-binary=:all:` dry run.

## Excluded by design

- `openclaw.json`, credential databases, API/OAuth tokens, and service state;
- concrete Feishu chat IDs and delivery history;
- personal categories, watched authors, institutions, and research interests;
- downloaded PDFs, extracted text, figures, review batches, HTML, and receipts;
- `arxiv_pushed_ids.json`, `papers_to_expand.json`, `sent.json`, and locks;
- session logs and the private repository's Git history;
- published-literature and CCF pipelines, which are different scheduled jobs.
