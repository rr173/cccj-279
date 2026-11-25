"""半开区间 [lo, hi) 工具。所有输入区间（位置、工单、停机）均按半开处理。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    """整数时间轴上的半开区间 [lo, hi)，长度为 hi - lo。"""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise TypeError("区间端点必须为整数")
        if self.lo >= self.hi:
            raise ValueError(f"区间必须非空且半开: [{self.lo}, {self.hi})")

    @property
    def length(self) -> int:
        return self.hi - self.lo

    def intersects(self, other: "Interval") -> bool:
        return self.lo < other.hi and other.lo < self.hi

    def intersection(self, other: "Interval") -> "Interval | None":
        lo, hi = max(self.lo, other.lo), min(self.hi, other.hi)
        if lo < hi:
            return Interval(lo, hi)
        return None

    def clip(self, window: "Interval") -> "Interval | None":
        return self.intersection(window)

    def contains_point(self, t: int) -> bool:
        """点 t 是否落在区间内（半开：lo 命中、hi 不命中）。"""
        return self.lo <= t < self.hi

    @staticmethod
    def hull(intervals: list["Interval"]) -> "Interval | None":
        ivs = list(intervals)
        if not ivs:
            return None
        return Interval(min(i.lo for i in ivs), max(i.hi for i in ivs))

    @staticmethod
    def union(intervals: list["Interval"]) -> list["Interval"]:
        """真实时间并集：重叠/相邻的区间合并，每个时刻最多保留一次。"""
        if not intervals:
            return []
        ordered = sorted(intervals, key=lambda i: (i.lo, i.hi))
        merged: list[Interval] = []
        for iv in ordered:
            if merged and iv.lo <= merged[-1].hi:  # 相邻也并：[a,b)∪[b,c)=[a,c)
                if iv.hi > merged[-1].hi:
                    merged[-1] = Interval(merged[-1].lo, iv.hi)
            else:
                merged.append(Interval(iv.lo, iv.hi))
        return merged
