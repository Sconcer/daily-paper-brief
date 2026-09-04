# Daily Paper Brief

> Evidence-first literature monitoring and review reports for AI infrastructure, HPC systems, and AI4Sci.

[![CI](https://github.com/Sconcer/daily-paper-brief/actions/workflows/ci.yml/badge.svg)](https://github.com/Sconcer/daily-paper-brief/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[简体中文](README.zh-CN.md) · English · [Project page](https://sconcer.github.io/daily-paper-brief/)

Daily Paper Brief turns a daily arXiv feed into a traceable research report: it selects papers, preserves source evidence, coordinates structured review, validates every required field, builds one offline HTML report, and sends that report once.

Daily Paper Brief is independent. It is not affiliated with, endorsed by, or operated by arXiv or Cornell University.

> **Release status: source-ready for owner review; local gates and public CI pass.** Owner-controlled files use Apache-2.0; `humanizer-zh` remains under MIT. Runtime paper content and generated reports are not covered by the code license. See [the risk assessment](docs/OPEN_SOURCE_RISK_ASSESSMENT.md).

## What you get

| Input | Output |
|---|---|
| Current arXiv metadata and papers | Ranked AI Infra → HPC Systems → AI4Sci selection |
| Paper PDF/HTML and public metadata | Source manifest, extracted text, main figure or explicit fallback |
| Structured model review | Chinese-first summaries, group profile, three review perspectives, claim–evidence checks |
| Fixed writing-signal rubric | Located evidence, counter-evidence, confounders, and a non-accusatory label |
| Validated review JSON | One self-contained HTML report, build receipt, editable draw.io source, and directly generated SVG |

Daily Paper Brief does **not** perform formal peer review, prove citation correctness, infer misconduct, or calculate an “AI-written percentage.”

## Five-minute orientation

### 1. Verify the bundle offline

Use a compatible environment:

```bash
python3 scripts/verify_bundle.py
python3 scripts/security_audit.py
python3 -m unittest discover -s tests -v
```

The default suite does not contact arXiv or Feishu.

### 2. Create a local configuration

```bash
cp config/arxiv-monitor-config-phd.example.json arxiv-monitor-config-phd.json
```

Review the categories, keywords, authors, institutions, scoring weights, and maximum daily paper count. The local file is ignored by Git.

### 3. Build the verified environment

```bash
./scripts/bootstrap.sh
```

The bootstrap script creates `.venv`, installs hash-locked binary Python packages, checks Poppler, creates the local config when absent, and runs the offline security gates and tests.

## Daily data flow

```text
Monitor
  arxiv_monitor_phd.py
      ↓ frozen selection + dedup state

Prepare
  arxiv_report_pipeline.py prepare
      ↓ PDFs, text, figures, provenance, writing descriptors

Review
  OpenClaw agent + bounded review batches
      ↓ structured review JSON

Gate
  arxiv_report_pipeline.py merge-batches / build
      ↓ validated offline HTML + build receipt + vertical draw.io/SVG maps

Deliver
  arxiv_send_html_to_feishu.py --dry-run
  arxiv_send_html_to_feishu.py
      ↓ receipt-checked, destination-scoped Feishu attachment
```

A successful model turn is not a successful pipeline. Completion requires either a verified no-new-paper state or complete review JSON, successful HTML build, successful dry run, and a delivered/existing message ID.

## Repository map

| Path | Role |
|---|---|
| `src/arxiv_monitor_phd.py` | API/RSS retrieval, scoring, topic ordering, daily deduplication |
| `src/arxiv_report_pipeline.py` | Asset retrieval, sanitization, schema validation, diagrams, HTML |
| `src/ai_writing_metrics.py` | Reproducible descriptive metrics; never an authorship classifier |
| `src/arxiv_send_html_to_feishu.py` | Receipt validation and destination-scoped Feishu file delivery |
| `config/` | Example monitor configuration; the real local config stays untracked at the repository root |
| `cron/` | Portable prompt, policy, and OpenClaw job templates |
| `skills/` | One Apache-2.0 academic baseline and one separately licensed MIT pattern catalog |
| `tests/` | Offline units plus an opt-in network probe |
| `docs/` | Security, provenance, validation, naming, and release-risk records |

## Run deterministic stages manually

Run the monitor once:

```bash
.venv/bin/python src/arxiv_monitor_phd.py
```

Prepare source assets:

```bash
.venv/bin/python src/arxiv_report_pipeline.py prepare \
  --input ./papers_to_expand.json \
  --date YYYY-MM-DD
```

The rendered cron runbook contains the exact review-batch, merge, build, dry-run, and send commands. Runtime and downloaded files are ignored by Git.

Test arXiv connectivity only when needed:

```bash
.venv/bin/python tests/network_smoke_arxiv.py
```

## OpenClaw cron

Set local runtime values. Use a real contact in the user agent before sustained API usage:

```bash
export FEISHU_CHAT_ID='YOUR_FEISHU_CHAT_ID'
export ARXIV_REVIEW_MODEL='openai/gpt-5.6-sol'
export DAILY_PAPER_BRIEF_USER_AGENT='DailyPaperBrief/1.0 (independent research tool; contact: you@example.org)'
```

Render private runtime files without changing OpenClaw:

```bash
python3 scripts/install_openclaw_cron.py --render-only
```

Inspect `runtime/`, then explicitly create a new job:

```bash
python3 scripts/install_openclaw_cron.py --apply --acknowledge-local-agent-trust
```

The acknowledgement is required because an isolated OpenClaw chat session is not an OS sandbox. The installer also limits the job's tool surface, refuses both the Daily Paper Brief name and legacy arXiv-review signatures, and never edits or removes an existing job.

Defaults: 10:00 `Asia/Shanghai`, no stagger, isolated session, GPT-5.6, medium reasoning, 90-minute maximum turn, Feishu announcement delivery.

## Data and credentials

Never commit:

- `~/.openclaw/openclaw.json`, OAuth databases, tokens, or service environment;
- real Feishu destinations or delivery history;
- `arxiv_pushed_ids.json`, `papers_to_expand.json`, `sent.json`, or locks;
- downloaded PDFs, extracted full text, figures, review batches, HTML reports, or sessions.

The sender reads Feishu credentials from the private OpenClaw configuration only after the HTML and its build receipt agree. It uploads the exact bytes that were validated, reads the destination from a mode-0600 runtime file, and scopes duplicate state to both content and destination. `--dry-run` does not read credentials or contact Feishu.

## Source rights and attribution

Metadata and paper content are different rights layers. Individual PDFs, figures, code, and datasets may use different licenses. Do not publish generated reports with embedded figures/full text until each use has an adequate license or other legal basis.

Requested source acknowledgement:

> Thank you to arXiv for use of its open access interoperability.

Review the current [arXiv API access guidance](https://info.arxiv.org/help/api/index.html), [API terms](https://info.arxiv.org/help/api/tou.html), [API manual](https://info.arxiv.org/help/api/user-manual.html), and [brand guidance](https://info.arxiv.org/brand/logos.html) before public deployment.

## Important limitations

- `AI-writing-signals-v1.0` reports observable writing-assistance signals, not authorship or misconduct.
- Automatic descriptors are not calibrated for individual-paper classification.
- Author/lab profiles are public-source summaries and must not guess advisor or corresponding-author relationships.
- Poppler still parses untrusted PDFs. Time, memory, file-size, and log limits reduce impact but are not a syscall sandbox.
- OpenClaw review jobs retain `exec` to run deterministic pipeline stages. Use this workflow only in a trusted single-user environment unless the reviewer and credential-bearing sender are isolated at the OS/container boundary.
- Python packages are hash locked; Poppler and the host Python interpreter remain external supply-chain inputs.
- A symlinked OpenClaw state root can block native `exec`; configure `OPENCLAW_STATE_DIR` to the real directory instead of disabling safety checks.

## Documentation

- [Agent skill](SKILL.md) — the repository itself is a loadable skill (clone or symlink into a skills directory)
- [Open-source risk assessment](docs/OPEN_SOURCE_RISK_ASSESSMENT.md)
- [Name decision](docs/NAME_DECISION.md)
- [Security model](docs/SECURITY.md)
- [Source manifest](docs/SOURCE_MANIFEST.md)
- [Validation record](docs/VALIDATION.md)
- [Provenance](docs/PROVENANCE.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

Owner-controlled files are licensed under [Apache-2.0](LICENSE). The bundled
`humanizer-zh` snapshot remains under its preserved MIT license. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for scope and attribution.
