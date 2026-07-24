from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REQUIRED_HORIZONS = (1, 5, 10, 20, 40, 60)
REQUIRED_AGENTS = (
    "market_theme",
    "announcement_risk",
    "intraday_confirmation",
    "portfolio_heat",
)


def protocol_paths(project: Path) -> tuple[Path, Path]:
    config = project / "config" / "validation_protocol.toml"
    lock = project / "config" / "validation_protocol.lock.json"
    return config, lock


def protocol_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(document.get("schema_version") or 0) != 1:
        errors.append("schema_version 必须为 1")
    if document.get("status") != "frozen":
        errors.append("协议状态必须为 frozen")
    if tuple(document.get("horizons", {}).get("evaluation_days") or ()) != REQUIRED_HORIZONS:
        errors.append(f"观察周期必须固定为 {list(REQUIRED_HORIZONS)}")
    if document.get("baseline", {}).get("name") != "纯量价横截面基线":
        errors.append("基线必须为纯量价横截面基线")
    if tuple(document.get("baseline", {}).get("predictive_inputs") or ()) != ("adjusted_ohlcv",):
        errors.append("纯量价基线的预测输入只能是 adjusted_ohlcv")
    agents = tuple(document.get("agent_ablation", {}).get("agents") or ())
    if agents != REQUIRED_AGENTS:
        errors.append(f"Agent 消融顺序必须固定为 {list(REQUIRED_AGENTS)}")
    if document.get("horizons", {}).get("all_horizons_required") is not False:
        errors.append("协议必须明确不要求所有周期同时有效")
    if document.get("time_design", {}).get("final_holdout_open_once") is not True:
        errors.append("最终留出集必须只打开一次")
    if document.get("agent_ablation", {}).get("automatic_weight_change") is not False:
        errors.append("Agent 不得自动修改权重")
    return errors


def load_protocol(project: Path, *, require_lock: bool = True) -> dict[str, Any]:
    config_path, lock_path = protocol_paths(project)
    if not config_path.exists():
        raise ValueError(f"未找到验证协议：{config_path}")
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    errors = _validate(document)
    if errors:
        raise ValueError("验证协议无效：" + "；".join(errors))
    digest = protocol_sha256(config_path)
    lock: dict[str, Any] | None = None
    if require_lock:
        if not lock_path.exists():
            raise ValueError(f"未找到验证协议锁：{lock_path}")
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if lock.get("protocol_sha256") != digest:
            raise ValueError("验证协议已变化但锁未更新；必须新建版本并记录变更原因")
        if lock.get("protocol_id") != document.get("protocol_id"):
            raise ValueError("验证协议与锁的 protocol_id 不一致")
    return {
        "document": document,
        "protocol_sha256": digest,
        "config_path": str(config_path),
        "lock_path": str(lock_path),
        "lock": lock,
    }


def freeze_protocol(project: Path, *, change_reason: str) -> dict[str, Any]:
    if not change_reason.strip():
        raise ValueError("冻结协议必须提供 change_reason")
    config_path, lock_path = protocol_paths(project)
    loaded = load_protocol(project, require_lock=False)
    document = loaded["document"]
    lock = {
        "schema_version": 1,
        "protocol_id": document["protocol_id"],
        "version": document["version"],
        "frozen_on": document["frozen_on"],
        "protocol_sha256": loaded["protocol_sha256"],
        "change_reason": change_reason.strip(),
        "change_control": "任何口径变化必须更新版本、原因和锁；历史报告继续引用原哈希",
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lock


def verify_protocol(project: Path) -> dict[str, Any]:
    loaded = load_protocol(project)
    document = loaded["document"]
    return {
        "status": "verified",
        "verified_at": datetime.now(UTC).isoformat(),
        "protocol_id": document["protocol_id"],
        "version": document["version"],
        "protocol_sha256": loaded["protocol_sha256"],
        "horizons": document["horizons"]["evaluation_days"],
        "baseline": document["baseline"]["name"],
        "agents": document["agent_ablation"]["agents"],
        "research_only": document["research_only"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 冻结验证协议")
    parser.add_argument("command", choices=["verify", "freeze"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    result = (
        verify_protocol(project)
        if args.command == "verify"
        else freeze_protocol(project, change_reason=args.reason)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
