from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Literal

Level = Literal["automatic", "mandatory_review"]
WindowStatus = Literal["not_evaluable", "within", "out"]


@dataclass(frozen=True)
class MonitorPolicy:
    window: int
    min_selected_per_group: int
    di_min: float
    spd_range: tuple[float, float]
    reference: Literal["pool", "population"]
    population_share: float
    escalate_after: int
    return_after: int

    def __post_init__(self) -> None:
        counts = (
            self.window,
            self.min_selected_per_group,
            self.escalate_after,
            self.return_after,
        )
        if any(type(value) is not int or value <= 0 for value in counts):
            raise ValueError("Monitor counts must be positive integers")
        low, high = self.spd_range
        if (
            self.reference not in ("pool", "population")
            or not 0 < self.population_share < 1
            or not isfinite(self.di_min)
            or self.di_min < 0
            or not -1 <= low <= high <= 1
        ):
            raise ValueError("Invalid monitor reference or thresholds")


@dataclass(frozen=True)
class SelectionCounts:
    selected_protected: int
    selected_reference: int
    candidates_protected: int
    candidates_reference: int

    def __post_init__(self) -> None:
        counts = (
            self.selected_protected,
            self.selected_reference,
            self.candidates_protected,
            self.candidates_reference,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Selection counts must be nonnegative integers")
        if (
            self.selected_protected > self.candidates_protected
            or self.selected_reference > self.candidates_reference
        ):
            raise ValueError("Selections must be drawn from their candidate groups")

    def add(self, other: "SelectionCounts") -> "SelectionCounts":
        return SelectionCounts(
            self.selected_protected + other.selected_protected,
            self.selected_reference + other.selected_reference,
            self.candidates_protected + other.candidates_protected,
            self.candidates_reference + other.candidates_reference,
        )


@dataclass(frozen=True)
class WindowMetrics:
    status: WindowStatus
    reason: str | None = None
    protected_rate: float | None = None
    reference_rate: float | None = None
    di: float | None = None
    spd: float | None = None
    protected_interval: tuple[float, float] | None = None
    reference_interval: tuple[float, float] | None = None


def _wilson(selected: int, candidates: int) -> tuple[float, float]:
    z = 1.959964
    rate = selected / candidates
    divisor = 1 + z**2 / candidates
    center = (rate + z**2 / (2 * candidates)) / divisor
    half = (
        z * sqrt(rate * (1 - rate) / candidates + z**2 / (4 * candidates**2)) / divisor
    )
    return round(center - half, 6), round(center + half, 6)


def evaluate_window(counts: SelectionCounts, policy: MonitorPolicy) -> WindowMetrics:
    selected_p = counts.selected_protected
    selected_r = counts.selected_reference
    if min(selected_p, selected_r) < policy.min_selected_per_group:
        return WindowMetrics("not_evaluable", "insufficient_selections")

    candidates_p = counts.candidates_protected
    candidates_r = counts.candidates_reference
    total = candidates_p + candidates_r
    pool = policy.reference == "pool"
    denominator_p = candidates_p if pool else policy.population_share * total
    denominator_r = candidates_r if pool else (1 - policy.population_share) * total
    rate_p = selected_p / denominator_p
    rate_r = selected_r / denominator_r
    di, spd = rate_p / rate_r, rate_p - rate_r
    low, high = policy.spd_range
    within = di >= policy.di_min and low <= spd <= high
    return WindowMetrics(
        "within" if within else "out",
        protected_rate=round(rate_p, 6),
        reference_rate=round(rate_r, 6),
        di=round(di, 4),
        spd=round(spd, 4),
        protected_interval=_wilson(selected_p, candidates_p) if pool else None,
        reference_interval=_wilson(selected_r, candidates_r) if pool else None,
    )


@dataclass(frozen=True)
class MonitorControl:
    level: Level = "automatic"
    consecutive_out: int = 0
    consecutive_within: int = 0

    def observe(
        self, status: WindowStatus, policy: MonitorPolicy
    ) -> tuple["MonitorControl", str | None]:
        if status == "not_evaluable":
            return self, None
        if status == "out":
            streak = self.consecutive_out + 1
            if self.level == "automatic" and streak >= policy.escalate_after:
                return MonitorControl("mandatory_review"), "out_of_threshold"
            return MonitorControl(self.level, consecutive_out=streak), None
        streak = self.consecutive_within + 1
        if self.level == "mandatory_review" and streak >= policy.return_after:
            return MonitorControl(), "back_within_threshold"
        return MonitorControl(self.level, consecutive_within=streak), None


@dataclass(frozen=True)
class MonitorWindow:
    requests: int = 0
    counts: SelectionCounts = SelectionCounts(0, 0, 0, 0)

    def record(
        self, counts: SelectionCounts, policy: MonitorPolicy
    ) -> tuple["MonitorWindow", WindowMetrics | None]:
        aggregate = self.counts.add(counts)
        size = self.requests + 1
        if size == policy.window:
            return MonitorWindow(), evaluate_window(aggregate, policy)
        return MonitorWindow(size, aggregate), None
