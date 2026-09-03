# Open-source risk assessment

**Source-release status: READY_FOR_OWNER_REVIEW**

Assessment date: 2026-09-03. This is an engineering assessment, not legal
advice or trademark clearance.

## Decision

The source tree may be published after the final local and GitHub CI gates pass.
It must not be presented as a hardened multi-tenant service. Runtime reports,
papers, extracted text, figures, credentials, destinations, and personal
configuration remain outside the open-source release.

Owner-controlled files use Apache-2.0. The only bundled third-party skill,
`humanizer-zh`, retains its MIT license. The unlicensed
`academic-writing-refiner` package and its Git history are excluded; a new
Apache-2.0 `academic-style-baseline` provides the narrow counter-check required
by the report policy.

## Risk register

| ID | Risk | Severity | Source-release status | Operational requirement |
|---|---|---:|---|---|
| R1 | Paper/figure redistribution rights | High | Runtime artifacts ignored | Keep reports private unless each embedded use has a legal basis |
| R2 | Prompt injection reaches a model with `exec` | High | Clearly scoped, privileged tools reduced | Use only in a trusted single-user environment or isolate reviewer and sender by OS/container identity |
| R3 | Native PDF parser compromise or denial of service | High | Limits added; draw.io removed from runtime | Patch Poppler and run parsing in a stronger sandbox for adversarial/public workloads |
| R4 | Model review contains unsupported claims | High | Strict shape/evidence validation | Human-check consequential claims; schema validity is not factual validity |
| R5 | AI-writing signal misuse | High | Mandatory caveats and counter-evidence retained | Never treat the score as authorship probability or misconduct evidence |
| R6 | Local-file exfiltration through manifest paths | High | Mitigated | Preserve root confinement, symlink rejection, receipts, and tests |
| R7 | SSRF through redirects or asset URLs | High | Mitigated | Preserve pre-request redirect validation and fixed arXiv host/port policy |
| R8 | Vulnerable Pillow image parsing | High | Mitigated at 12.3.0 | Keep dependency audit and decoder allowlist current |
| R9 | Credential or destination leakage | High | Mitigated in candidate tree and private runtime split | Scan the complete Git history before every release |
| R10 | Author/lab profile privacy and incorrect relationship inference | Medium | Policy prohibits guessing | Cite public evidence and provide a correction/removal channel for hosted reports |
| R11 | Python/GitHub Actions supply chain | Medium | Versions, hashes, and action SHAs locked | Review automated dependency updates and regenerate locks from trusted indexes |
| R12 | Disk, memory, or output exhaustion | Medium | Per-resource and native-process limits added | Add deployment-level quotas for long-running or multi-user services |
| R13 | arXiv name or endorsement confusion | Medium | Source-neutral project brand and disclaimer | Use “Daily arXiv Report” only as a descriptive document title |
| R14 | Personalized research interests disclosed | Medium | Real config ignored | Keep categories, watched authors, destinations, and state out of Git |

## Rights boundary

Apache-2.0 covers only owner-controlled source and documentation. It does not
relicense:

- papers, abstracts beyond applicable metadata terms, figures, or source files;
- linked repositories, datasets, or model weights;
- generated reports that reproduce third-party content;
- the separately licensed `humanizer-zh` snapshot.

The safest public profile is source code, empty/example schemas, tests, and
documentation. Direct users to the paper's abstract page rather than publicly
mirroring content without permission.

## Security boundary

The code now validates redirect destinations before connection, caps decoded
downloads, restricts image formats, confines manifest paths, generates SVG
without running draw.io, emits a build receipt, uploads the exact validated
bytes, scopes duplicate state by destination, limits native parser resources,
and uses a reduced OpenClaw tool list.

These controls do not turn an LLM with `exec` into a sandbox. For adversarial
inputs, use separate workers and credentials:

```text
retriever (network, no secrets)
  → reviewer (sanitized read-only inputs, no delivery credential)
  → deterministic builder (no network)
  → sender (fixed destination, narrow Feishu credential)
```

## Release checklist

- [x] Apache-2.0 selected for owner-controlled files.
- [x] MIT notice retained for `humanizer-zh`.
- [x] Unlicensed skill excluded from the clean history.
- [x] Real configuration, destinations, state, papers, and reports ignored.
- [x] Dependency versions and distribution hashes locked.
- [x] GitHub Actions pinned to full commit SHAs.
- [x] Offline security and release gates included.
- [x] arXiv acknowledgement and independent-project disclaimer present.
- [x] Security reporting channel and supported branch documented.
- [x] Final reachable Git history scan completed after the root commit.
- [ ] Public GitHub CI completed on the pushed commit.

Run:

```bash
python3 scripts/verify_bundle.py
python3 scripts/security_audit.py
python3 scripts/check_open_source_readiness.py
python3 scripts/audit_dependencies.py
python3 -m unittest discover -s tests -v
```
