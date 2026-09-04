#!/usr/bin/env python3
"""Pick a topic profile preset or print guidance for a custom configuration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = ROOT / "config" / "profiles"
LOCAL_CONFIG = ROOT / "arxiv-monitor-config-phd.json"

GUIDE = """\
No local configuration yet, or you want a custom topic setup.

1. List the bundled presets:
     python3 scripts/setup_config.py --list
2. Install one as the local config (ignored by Git):
     python3 scripts/setup_config.py --profile ai-infra-hpc
3. To customize, start from the preset closest to your field and edit
   arxiv-monitor-config-phd.json. The reference layout is
   config/arxiv-monitor-config-phd.example.json.

Key config fields:
  categories        arXiv categories to poll (e.g. cs.LG, physics.comp-ph)
  keywords          must_include pre-filter and exclude list
  topics            scoring topics; each entry has:
      id                unique lowercase id (a-z, 0-9, _)
      label             display name used in reports and notifications
      base / cap        base score and per-topic score ceiling
      score_weight      weight of this topic in the total relevance score
      term_groups       named term lists with per-group cap and weight
      require_all_groups  true: every group must hit; false: any group
      strong_rule       {"title_group": NAME} or {"title_any": true} to gate
                        the topic score on a title hit; omit for no gate
      title_all_groups_bonus  extra score when every group hits in the title
      quota             null: elastic topic; integer: daily cap
  topic_priority    optional explicit ordering of topic ids
  authors / institutions / research_profile  personalization boosts
  scoring_weights   keyword/author/institution/recency/relevance weights
  min_score, max_papers_per_day, request_delay_seconds  selection limits

Legacy configs with the ai_infra_* / hpc_* / ai4sci_* keyword lists keep
working unchanged; the monitor converts them internally.
"""


def load_profiles() -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON in {path}: {exc}")
        topics = payload.get("topics")
        if not isinstance(topics, list) or not topics:
            raise SystemExit(f"{path} must define a non-empty topics array")
        profiles[path.stem] = payload
    return profiles


def list_profiles(profiles: dict[str, dict]) -> None:
    print("Available topic profiles (config/profiles/):\n")
    for name, payload in profiles.items():
        description = str(payload.get("description") or "").strip()
        labels = [str(topic.get("label") or topic.get("id")) for topic in payload["topics"]]
        print(f"  {name}")
        print(f"    {description}")
        print(f"    topics: {' / '.join(labels)}\n")
    print("Install one with: python3 scripts/setup_config.py --profile NAME")


def install_profile(profiles: dict[str, dict], name: str, force: bool) -> int:
    name = name[:-5] if name.endswith(".json") else name
    if name not in profiles:
        print(
            f"unknown profile: {name!r}; available: {', '.join(sorted(profiles))}",
            file=sys.stderr,
        )
        return 2
    if LOCAL_CONFIG.exists() and not force:
        print(
            f"refusing to overwrite existing {LOCAL_CONFIG.name}; "
            "delete it first or pass --force",
            file=sys.stderr,
        )
        return 2
    shutil.copyfile(PROFILES_DIR / f"{name}.json", LOCAL_CONFIG)
    os.chmod(LOCAL_CONFIG, 0o600)
    print(f"created local config from profile {name!r}: {LOCAL_CONFIG}")
    print("edit it to taste; it is ignored by Git and must not be committed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list available topic profiles")
    parser.add_argument("--profile", metavar="NAME", help="install a profile as the local config")
    parser.add_argument("--force", action="store_true", help="allow overwriting an existing local config")
    args = parser.parse_args(argv)

    profiles = load_profiles()
    if args.list:
        list_profiles(profiles)
        return 0
    if args.profile:
        return install_profile(profiles, args.profile, args.force)
    if args.force:
        parser.error("--force only makes sense together with --profile")
    print(GUIDE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
