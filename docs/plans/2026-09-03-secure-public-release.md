# Daily Paper Brief Secure Public Release Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task when work resumes in another session.

**Goal:** Publish a clean-history Apache-2.0 repository that can generate and deliver a Chinese-first Daily arXiv Report without redistributing unlicensed skill content or retaining the high-risk paths found in the 2026-09-03 audit.

**Architecture:** Keep deterministic retrieval, validation, rendering, and delivery code separate from model-authored review data. Treat remote papers and review JSON as untrusted, confine every resource path to its dated report directory, validate every redirect before following it, and require a build receipt before delivery. The OpenClaw integration remains a local single-user workflow; public-service operators must isolate the review worker and credential-bearing sender at the OS or container boundary.

**Tech Stack:** Python 3.12+, Requests, lxml, Pillow, Poppler CLI, OpenClaw cron, Feishu REST API, unittest, GitHub Actions.

---

### Task 1: Clean repository and licensing boundary

**Files:**
- Create: `LICENSE`, `NOTICE`, `docs/PROVENANCE.md`
- Modify: `README.md`, `README.zh-CN.md`, `THIRD_PARTY_NOTICES.md`
- Remove before first commit: `skills/academic-writing-refiner/`

**Steps:**
1. Start from a working-tree export, not the LitFacet Git history.
2. Add the canonical Apache License 2.0 text for owner-controlled files.
3. Preserve the bundled `humanizer-zh` MIT license and document its separate scope.
4. Verify that no unlicensed skill text or historical Git object exists.

**Test:** `git rev-list --objects --all` after the first commit must contain no `academic-writing-refiner` path.

### Task 2: Replace the academic-style reference

**Files:**
- Create: `skills/academic-style-baseline/SKILL.md`
- Modify: `cron/arxiv_cron_prompt.template.md`, bundle validation and tests

**Steps:**
1. Write a narrow original skill that evaluates ordinary academic conventions and confounders without rewriting papers or detecting authorship.
2. Route the writing-signals audit to the new skill.
3. Validate frontmatter and ensure the skill has no executable or network behavior.

**Test:** Run the skill creator's `quick_validate.py` and the repository bundle tests.

### Task 3: Network and media hardening

**Files:**
- Modify: `requirements.txt`, `arxiv_monitor_phd.py`, `arxiv_report_pipeline.py`
- Test: `tests/test_arxiv_report_pipeline.py`, `tests/test_arxiv_monitor_priority.py`

**Steps:**
1. Upgrade Pillow to 12.3.0 or newer patched baseline.
2. Restrict raster decoders and enforce dimension, pixel, frame, and byte limits.
3. Follow HTTP redirects manually, validating the destination before each request.
4. Bound API/RSS bodies and use a hardened XML parser.
5. Remove active or externally loading SVG constructs and add defense-in-depth CSP to generated HTML.

**Test:** Add cases for off-domain redirects, unsupported images, decompression limits, SVG CSS URLs, and bounded XML responses.

### Task 4: Filesystem and delivery confinement

**Files:**
- Modify: `arxiv_report_pipeline.py`, `arxiv_send_html_to_feishu.py`
- Test: `tests/test_arxiv_report_pipeline.py`

**Steps:**
1. Derive the report root from the trusted manifest location and reject paths outside it or through symlinks.
2. Use private directories and atomic mode-0600 writes for downloaded and generated artifacts.
3. Emit a build receipt containing the final HTML hash and source hashes.
4. Load Feishu targets from a private ignored file, scope idempotency by report plus target, and upload the exact bytes that were validated.
5. Require the build receipt during scheduled delivery.

**Test:** Prove that traversal, absolute external paths, symlinks, receipt mismatches, cross-target duplicate state, and file-swap attempts fail closed.

### Task 5: Runtime trust boundary and release gates

**Files:**
- Modify: `cron/arxiv-daily-review.job.template.json`, `scripts/install_openclaw_cron.py`, `docs/SECURITY.md`, release scripts
- Create: `.github/workflows/ci.yml`, `scripts/security_audit.py`

**Steps:**
1. Document that an isolated chat session is not an OS sandbox and make local-trust acknowledgement explicit for live installation.
2. Keep the deterministic sender outside review content and use a fixed private destination.
3. Scan the current tree and full Git history for credentials and private destinations.
4. Fail release checks on unsafe dependency versions, missing licenses, unlicensed bundled skills, or generated/private artifacts.
5. Run tests and security gates in CI.

**Test:** The release gate must report `READY_FOR_OWNER_REVIEW`; a seeded secret or unsafe dependency must make it fail.

### Task 6: Clean commit and public push

**Files:** all release files

**Steps:**
1. Run syntax checks, unit tests, skill validation, bundle verification, dependency audit, and Git history scan.
2. Use a repository-local GitHub noreply author identity.
3. Create one root commit and push `main` without force.
4. Compare local and remote commit IDs and verify public repository metadata.

**Test:** `git ls-remote origin refs/heads/main` must equal local `HEAD`, and the working tree must be clean.
