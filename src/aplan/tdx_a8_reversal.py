from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class A8PromptSnapshot:
    oscillator: float | None
    raw_buy: bool
    filtered_buy: bool
    ema245: float | None
    ma20: float | None
    ma30: float | None
    above_ema245: bool
    trend_aligned: bool
    red_hold_state: bool


def _ema(values: list[float | None], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("EMA 周期必须为正数")
    alpha = 2.0 / (period + 1.0)
    output: list[float | None] = []
    state: float | None = None
    for value in values:
        if value is None or not isfinite(value):
            output.append(None)
            continue
        state = value if state is None else alpha * value + (1.0 - alpha) * state
        output.append(state)
    return output


def _moving_average(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("MA 周期必须为正数")
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            output[index] = running / period
    return output


def a8_oscillator(closes: list[float]) -> list[float | None]:
    """Reproduce the formula's double-EMA normalized close-change oscillator."""
    if any(not isfinite(value) or value <= 0 for value in closes):
        raise ValueError("收盘价必须为正的有限数")
    changes: list[float | None] = [None]
    changes.extend(closes[index] - closes[index - 1] for index in range(1, len(closes)))
    absolute_changes = [abs(value) if value is not None else None for value in changes]
    numerator = _ema(_ema(changes, 6), 6)
    denominator = _ema(_ema(absolute_changes, 6), 6)
    output: list[float | None] = []
    for signed, absolute in zip(numerator, denominator, strict=True):
        if signed is None or absolute is None or absolute == 0:
            output.append(None)
        else:
            output.append(100.0 * signed / absolute)
    return output


def raw_buy_flags(oscillator: list[float | None], *, warmup_bars: int = 60) -> list[bool]:
    """Translate LLV/COUNT/CROSS exactly; COUNT is true when its count is non-zero."""
    output = [False] * len(oscillator)
    for index in range(max(7, warmup_bars - 1), len(oscillator)):
        window7 = oscillator[index - 6 : index + 1]
        window2 = oscillator[index - 1 : index + 1]
        previous_pair = oscillator[index - 2 : index]
        if any(value is None for value in (*window7, *previous_pair)):
            continue
        values7 = [float(value) for value in window7 if value is not None]
        values2 = [float(value) for value in window2 if value is not None]
        previous2 = [float(value) for value in previous_pair if value is not None]
        current_ma2 = sum(values2) / 2.0
        previous_ma2 = sum(previous2) / 2.0
        output[index] = (
            min(values2) == min(values7)
            and any(value < 0 for value in values2)
            and values2[-2] <= previous_ma2
            and values2[-1] > current_ma2
        )
    return output


def filter_signals(flags: list[bool], suppressed_following_bars: int = 5) -> list[bool]:
    """TDX FILTER semantics used here: keep a hit, suppress the following N bars."""
    if suppressed_following_bars < 0:
        raise ValueError("过滤周期不能为负数")
    output = [False] * len(flags)
    blocked_through = -1
    for index, flag in enumerate(flags):
        if flag and index > blocked_through:
            output[index] = True
            blocked_through = index + suppressed_following_bars
    return output


def red_hold_flags(closes: list[float]) -> list[bool]:
    """Reproduce VAR1..VARC as a separate display/holding-state axis."""
    states = [[False] * len(closes) for _ in range(12)]
    for index in range(2, len(closes)):
        states[0][index] = closes[index] > closes[index - 1] and closes[index] > closes[index - 2]
        for level in range(1, 12):
            rising_step = level % 2 == 0
            price_condition = (
                closes[index] >= closes[index - 1] and closes[index] <= closes[index - 2]
                if rising_step
                else closes[index] <= closes[index - 1] and closes[index] >= closes[index - 2]
            )
            states[level][index] = states[level - 1][index - 1] and price_condition
    return [any(level[index] for level in states) for index in range(len(closes))]


def analyze_a8_prompt(closes: list[float], *, warmup_bars: int = 60) -> list[A8PromptSnapshot]:
    oscillator = a8_oscillator(closes)
    raw = raw_buy_flags(oscillator, warmup_bars=warmup_bars)
    filtered = filter_signals(raw, 5)
    ema245 = _ema([float(value) for value in closes], 245)
    ma20 = _moving_average(closes, 20)
    ma30 = _moving_average(closes, 30)
    red_hold = red_hold_flags(closes)
    output: list[A8PromptSnapshot] = []
    for index in range(len(closes)):
        long_ready = index >= 244 and ema245[index] is not None
        middle_ready = ma20[index] is not None and ma30[index] is not None
        above_ema245 = bool(long_ready and closes[index] > float(ema245[index]))
        trend_aligned = bool(
            above_ema245
            and middle_ready
            and float(ma20[index]) > float(ma30[index])
        )
        output.append(
            A8PromptSnapshot(
                oscillator=oscillator[index],
                raw_buy=raw[index],
                filtered_buy=filtered[index],
                ema245=ema245[index],
                ma20=ma20[index],
                ma30=ma30[index],
                above_ema245=above_ema245,
                trend_aligned=trend_aligned,
                red_hold_state=red_hold[index],
            )
        )
    return output
