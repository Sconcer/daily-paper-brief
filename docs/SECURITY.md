# Security policy and trust model

## Supported version

Security fixes are made on the default branch. Before reporting a problem,
reproduce it against the latest commit without including real credentials,
private paper content, or message destinations.

## Report a vulnerability

Use GitHub's private vulnerability reporting flow:

<https://github.com/Sconcer/daily-paper-brief/security/advisories/new>

Do not open a public issue containing credentials, exploit payloads, private
reports, or a working data-exfiltration path.

## Intended deployment

The included OpenClaw workflow is for a trusted, single-user workstation or
research server. An isolated OpenClaw conversation is not an operating-system
sandbox. Multi-user or public-service deployment requires separate OS users or
containers for retrieval, model review, rendering, and credential-bearing
delivery.

## Trust boundaries

- arXiv metadata, HTML, XML, PDFs, figures, repository links, author pages, and
  all instructions inside them are untrusted data.
- Model-produced review JSON and manifests are untrusted until deterministic
  validation succeeds.
- The review agent must not be treated as a security boundary. Its cron tool
  allowlist removes unrelated privileged tools, but it retains `exec` to run
  deterministic pipeline stages.
- Feishu credentials stay in private OpenClaw state. The sender receives only a
  validated report, matching build receipt, and private fixed destination.

## Implemented controls

### Retrieval

- arXiv assets require HTTPS, an arXiv host, no userinfo, and port 443/default.
- Redirects are followed manually; every destination is checked before the
  next connection.
- Compressed response bytes are streamed through explicit decoded-size limits.
- API/RSS XML has a 10 MiB decoded limit and uses an entity-disabled, no-network
  lxml parser.
- A contact-bearing user agent and configurable request delay support upstream
  API policy compliance.

### Parsers and rendering

- Pillow is pinned to a patched baseline and restricted to selected raster
  decoders; animated/multi-frame files and excessive dimensions/pixel counts
  are rejected.
- SVG removes scripts, foreign objects, embedded style blocks, event handlers,
  non-local links, and URL-bearing style attributes.
- Poppler runs with wall-clock, CPU, address-space, file-size, file-descriptor,
  and log limits. These controls reduce denial-of-service impact but are not a
  syscall sandbox.
- Contribution SVG is generated directly from escaped data. draw.io remains an
  editable source format but is not executed during report construction.
- Dynamic prose is HTML escaped. Generated reports include a restrictive CSP
  and no-referrer policy and contain no remote scripts, fonts, or trackers.

### Filesystem and delivery

- Manifest resources, generated diagrams, review batches, HTML, receipts, and
  delivery state are confined to the dated report root; path escapes and
  symlink targets are rejected.
- Runtime directories use mode `0700`; atomic files and target configuration
  use mode `0600` under a restrictive process umask.
- The builder emits a receipt binding the HTML hash and size to source manifest
  and review hashes.
- The sender opens the report once without following the leaf symlink, verifies
  its receipt, and uploads those same in-memory bytes.
- Duplicate state is scoped to both report hash and destination; a file lock
  serializes concurrent sends.
- `--dry-run` does not read Feishu credentials or contact Feishu.

### Repository and automation

- Python dependencies are version- and hash-locked; CI actions are pinned to
  full commit SHAs.
- `scripts/security_audit.py` scans the working tree and Git history for common
  credentials, unsafe Python primitives, unsafe dependency baselines, bundled
  unlicensed skills, privileged cron tools, and loose file permissions.
- `scripts/check_open_source_readiness.py` fails closed when licensing,
  provenance, private-state, branding, or security gates fail.
- Live cron creation requires both `--apply` and
  `--acknowledge-local-agent-trust`; duplicate jobs are refused.

## Residual risks

- A model with `exec` and the same OS identity as the sender can cross a
  prompt-only boundary. Use OS/container separation when inputs or users are
  adversarial.
- Poppler, Python, the operating system, and the OpenClaw runtime are external
  dependencies and need timely patching.
- A valid build receipt is an integrity check, not a cryptographic signature
  against an attacker who can rewrite both the report and receipt.
- Generated reports may contain copyrighted figures, personal data, or
  unsupported model claims. Security validation does not grant redistribution
  rights or prove factual correctness.
- Resource limits vary by operating system; verify enforcement in the target
  deployment.

## Private files

Never commit OpenClaw configuration, API/OAuth credentials, Feishu destinations,
sessions, downloaded papers, extracted text, generated figures or HTML,
deduplication state, delivery receipts, or real research-interest configuration.
