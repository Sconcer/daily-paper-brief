#!/usr/bin/env python3
"""Validate and send one built Daily arXiv Report to a fixed Feishu group."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import requests


WORKSPACE = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_REPORT_ROOT = WORKSPACE / "arxiv_reports"
MAX_FEISHU_FILE_BYTES = 19 * 1024 * 1024
UPLOAD_TIMEOUT = (120, 300)
REPORT_NAME_RE = re.compile(r"^daily_arxiv_report_\d{8}\.html$")


class SendError(RuntimeError):
    pass


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SendError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SendError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SendError(f"expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def require_path_within(
    root: Path,
    value: Path,
    label: str,
    *,
    must_exist: bool = True,
) -> Path:
    root_lexical = Path(os.path.abspath(root.expanduser()))
    if root_lexical.is_symlink():
        raise SendError(f"report root must not be a symbolic link: {root_lexical}")
    root_resolved = root_lexical.resolve(strict=True)
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = root_lexical / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate.is_symlink():
        raise SendError(f"{label} must not use a symbolic link: {candidate}")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise SendError(f"{label} does not exist: {candidate}") from exc
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SendError(f"{label} is outside report root: {resolved}") from exc
    current = root_resolved
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise SendError(f"{label} must not use a symbolic link: {current}")
    return resolved


def read_regular_file_once(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SendError(f"cannot securely open file: {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SendError(f"path is not a regular file: {path}")
        if metadata.st_size > maximum:
            raise SendError(f"file exceeds {maximum} bytes: {path}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise SendError(f"file exceeds {maximum} bytes: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_validated_html(
    path: Path,
    *,
    report_root: Path,
    receipt_path: Optional[Path] = None,
) -> Tuple[Dict[str, Any], bytes]:
    report_root = report_root.expanduser().resolve(strict=True)
    path = require_path_within(report_root, path, "HTML report")
    if not REPORT_NAME_RE.fullmatch(path.name):
        raise SendError(f"unexpected report filename: {path.name}")
    content = read_regular_file_once(path, MAX_FEISHU_FILE_BYTES - 1)
    size = len(content)
    if size <= 512:
        raise SendError(f"HTML report is unexpectedly small ({size} bytes): {path}")
    if b"<html" not in content[:2048].lower() and b"<!doctype html" not in content[:2048].lower():
        raise SendError(f"file does not look like HTML: {path}")
    metadata = {
        "path": str(path),
        "name": path.name,
        "bytes": size,
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    expected_receipt = path.with_suffix(".receipt.json")
    receipt_path = receipt_path or expected_receipt
    receipt_path = require_path_within(report_root, receipt_path, "build receipt")
    if receipt_path != expected_receipt:
        raise SendError(f"receipt path must be {expected_receipt}")
    receipt = load_json(receipt_path)
    expected = {
        "report_file": metadata["name"],
        "report_sha256": metadata["sha256"],
        "report_bytes": metadata["bytes"],
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    source_hashes_valid = all(
        isinstance(receipt.get(key), str)
        and re.fullmatch(r"[0-9a-f]{64}", receipt[key])
        for key in ("manifest_sha256", "reviews_sha256")
    )
    if receipt.get("schema_version") != 1 or mismatches or not source_hashes_valid:
        detail = ", ".join(mismatches) or "schema_version"
        if not source_hashes_valid:
            detail = "source hashes"
        raise SendError(f"build receipt does not match report: {detail}")
    metadata["receipt_path"] = str(receipt_path)
    return metadata, content


def feishu_origin(channel_config: Dict[str, Any]) -> str:
    domain = str(channel_config.get("domain") or "feishu").lower()
    return "https://open.larksuite.com" if "lark" in domain else "https://open.feishu.cn"


def require_credentials(config_path: Path) -> Dict[str, str]:
    config = load_json(config_path)
    channel = ((config.get("channels") or {}).get("feishu") or {})
    app_id = channel.get("appId")
    app_secret = channel.get("appSecret")
    if not isinstance(app_id, str) or not app_id.strip():
        raise SendError("channels.feishu.appId is missing")
    if not isinstance(app_secret, str) or not app_secret.strip():
        raise SendError("channels.feishu.appSecret is missing")
    return {"app_id": app_id, "app_secret": app_secret, "origin": feishu_origin(channel)}


def feishu_session() -> requests.Session:
    """Use a direct session so uploads do not traverse an ambient proxy."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "DailyPaperBriefFeishuSender/1.0"})
    return session


def delivery_target_hash(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]


def delivery_state_key(report_sha256: str, chat_id: str) -> str:
    return f"{report_sha256}:{delivery_target_hash(chat_id)}"


def previous_delivery(
    sent: Dict[str, Any], report_sha256: str, chat_id: str
) -> Optional[Dict[str, Any]]:
    target_hash = delivery_target_hash(chat_id)
    current = sent.get(delivery_state_key(report_sha256, chat_id))
    if isinstance(current, dict):
        return current
    legacy = sent.get(report_sha256)
    if isinstance(legacy, dict) and legacy.get("chat_id_hash") == target_hash:
        return legacy
    return None


def validate_chat_id(value: Any, source: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"oc_[A-Za-z0-9]+", value):
        raise SendError(f"invalid Feishu group chat_id in {source}")
    return value


def resolve_chat_id(chat_id: Optional[str], chat_id_file: Optional[str]) -> str:
    if chat_id is not None:
        return validate_chat_id(chat_id, "--chat-id")
    if chat_id_file is None:
        raise SendError("either --chat-id or --chat-id-file is required")
    unresolved_target = Path(chat_id_file).expanduser()
    if unresolved_target.is_symlink():
        raise SendError(f"target file must not be a symbolic link: {unresolved_target}")
    target_path = unresolved_target.resolve(strict=True)
    target = load_json(target_path)
    return validate_chat_id(target.get("chat_id"), str(target_path))


def api_json(response: requests.Response, operation: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SendError(f"{operation} returned non-JSON HTTP {response.status_code}") from exc
    if response.status_code >= 400 or payload.get("code", 0) != 0:
        code = payload.get("code", response.status_code)
        message = payload.get("msg") or payload.get("message") or "unknown error"
        raise SendError(f"{operation} failed: code={code}, message={message}")
    return payload


def send_html(
    metadata: Dict[str, Any],
    content: bytes,
    chat_id: str,
    config_path: Path,
    state_path: Path,
    force: bool = False,
) -> Dict[str, Any]:
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        lock_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise SendError(f"cannot securely open delivery lock: {lock_path}: {exc}") from exc
    with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state: Dict[str, Any] = (
            load_json(state_path)
            if state_path.exists()
            else {"schema_version": 2, "sent": {}}
        )
        sent = state.setdefault("sent", {})
        if not isinstance(sent, dict):
            raise SendError(f"invalid sent map in {state_path}")
        previous = previous_delivery(sent, metadata["sha256"], chat_id)
        if previous and not force:
            return {
                "ok": True,
                "skipped": True,
                "reason": "identical report already sent to this target",
                "message_id": previous.get("message_id"),
                **metadata,
            }

        credentials = require_credentials(config_path)
        session = feishu_session()
        try:
            token_response = session.post(
                f"{credentials['origin']}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": credentials["app_id"], "app_secret": credentials["app_secret"]},
                timeout=(10, 30),
            )
            token_payload = api_json(token_response, "tenant token request")
            token = token_payload.get("tenant_access_token")
            if not token:
                raise SendError("tenant token response contained no tenant_access_token")
            authorization = {"Authorization": f"Bearer {token}"}

            upload_response = session.post(
                f"{credentials['origin']}/open-apis/im/v1/files",
                headers=authorization,
                data={"file_type": "stream", "file_name": metadata["name"]},
                files={"file": (metadata["name"], content, "text/html; charset=utf-8")},
                timeout=UPLOAD_TIMEOUT,
            )
            upload_payload = api_json(upload_response, "HTML file upload")
            file_key = ((upload_payload.get("data") or {}).get("file_key"))
            if not file_key:
                raise SendError("file upload response contained no file_key")

            message_response = session.post(
                f"{credentials['origin']}/open-apis/im/v1/messages",
                headers={**authorization, "Content-Type": "application/json"},
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
                },
                timeout=(10, 60),
            )
            message_payload = api_json(message_response, "HTML file message")
            message_id = ((message_payload.get("data") or {}).get("message_id"))
            if not message_id:
                raise SendError("file message response contained no message_id")
        finally:
            session.close()

        target_hash = delivery_target_hash(chat_id)
        state["schema_version"] = 2
        sent[delivery_state_key(metadata["sha256"], chat_id)] = {
            "message_id": message_id,
            "file_name": metadata["name"],
            "bytes": metadata["bytes"],
            "sent_at": datetime.now().astimezone().isoformat(),
            "chat_id_hash": target_hash,
        }
        atomic_write_json(state_path, state)
        return {"ok": True, "skipped": False, "message_id": message_id, **metadata}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--file", required=True, help="path to the standalone HTML report")
    target = result.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", help="Feishu group chat_id")
    target.add_argument("--chat-id-file", help="private JSON file containing a chat_id field")
    result.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    result.add_argument("--receipt", help="build receipt; defaults beside the report")
    result.add_argument("--config", default=str(DEFAULT_CONFIG))
    result.add_argument("--state", help="idempotency state file; defaults to <report-root>/sent.json")
    result.add_argument("--force", action="store_true", help="send again to the same target")
    result.add_argument("--dry-run", action="store_true", help="validate only; do not read credentials or contact Feishu")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        os.umask(0o077)
        report_root = Path(args.report_root).expanduser().resolve(strict=True)
        report_path = Path(args.file).expanduser()
        receipt_path = Path(args.receipt).expanduser() if args.receipt else None
        metadata, content = load_validated_html(
            report_path,
            report_root=report_root,
            receipt_path=receipt_path,
        )
        chat_id = resolve_chat_id(args.chat_id, args.chat_id_file)
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, **metadata}, ensure_ascii=False, indent=2))
            return 0
        state_path = (
            Path(args.state).expanduser()
            if args.state
            else report_root / "sent.json"
        )
        state_path = require_path_within(
            report_root,
            state_path,
            "delivery state",
            must_exist=False,
        )
        result = send_html(
            metadata,
            content,
            chat_id,
            Path(args.config).expanduser().resolve(strict=True),
            state_path,
            force=args.force,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SendError, requests.RequestException, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
