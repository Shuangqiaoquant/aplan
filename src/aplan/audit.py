from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_files(project: Path) -> list[Path]:
    return sorted((project / "runs" / "daily").glob("20??????/*.json"))


def write_chained_audit(
    project: Path,
    trade_date: str,
    audit: dict[str, Any],
) -> Path:
    previous_files = audit_files(project)
    previous = previous_files[-1] if previous_files else None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = project / "runs" / "daily" / trade_date
    directory.mkdir(parents=True, exist_ok=True)
    audit["audit_chain"] = {
        "version": 1,
        "previous_path": (
            str(previous.relative_to(project)) if previous else None
        ),
        "previous_sha256": sha256_file(previous) if previous else None,
    }
    path = directory / f"{timestamp}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def verify_audit_chain(project: Path) -> dict[str, Any]:
    files = audit_files(project)
    errors: list[str] = []
    checked = 0
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        chain = document.get("audit_chain")
        if not chain:
            continue
        checked += 1
        previous_path = chain.get("previous_path")
        previous_sha256 = chain.get("previous_sha256")
        if previous_path is None:
            if previous_sha256 is not None:
                errors.append(f"{path}: previous_path 为空但存在哈希")
            continue
        previous = project / previous_path
        if not previous.exists():
            errors.append(f"{path}: 前序审计不存在 {previous_path}")
            continue
        actual = sha256_file(previous)
        if actual != previous_sha256:
            errors.append(f"{path}: 前序审计哈希不匹配 {previous_path}")
    return {
        "passed": not errors,
        "total_files": len(files),
        "chained_files": checked,
        "errors": errors,
        "latest": str(files[-1].relative_to(project)) if files else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="查询和验证 APlan 审计记录")
    parser.add_argument("command", choices=["verify", "latest"])
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.command == "verify":
        result = verify_audit_chain(project)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise SystemExit(1)
    else:
        files = audit_files(project)
        if not files:
            raise SystemExit("没有审计记录")
        print(json.dumps(json.loads(files[-1].read_text(encoding="utf-8")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

