from __future__ import annotations

import argparse

from .strategy_registry import StrategyRegistry


def build_registry() -> StrategyRegistry:
    """集中注册策略；当前有意保持为空。"""
    return StrategyRegistry()


def main() -> None:
    parser = argparse.ArgumentParser(description="查看 APlan 策略插件")
    parser.add_argument("command", choices=["list"])
    args = parser.parse_args()
    registry = build_registry()
    if args.command == "list":
        metadata = registry.list_metadata()
        if not metadata:
            print("当前没有已注册策略。研究策略须通过验证后再显式注册。")
            return
        for item in metadata:
            print(
                f"{item.strategy_id}@{item.version} "
                f"status={item.status.value} simulation={item.approved_for_simulation} "
                f"live={item.approved_for_live}"
            )


if __name__ == "__main__":
    main()

