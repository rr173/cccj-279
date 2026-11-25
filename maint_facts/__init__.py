"""检修事实拆分系统。

模块
----
- intervals    : 半开区间 [lo, hi)
- events       : 带稳定 ID 与递增修订号的事件存储及修订协议
- engine       : 按工单 × 实际位置物化的事实引擎、增量修订、版本发布
- availability : 产线可用性账（只读分段状态查询，不改动检修账）
"""

from .intervals import Interval
from .events import (
    Event,
    EventStore,
    RevisionConflict,
    RevisionRejected,
    ACCEPT,
    NOOP,
    REJECT,
    LOC,
    WO,
    DOWN,
    SPARE,
)
from .engine import (
    MaintenanceFactEngine,
    Proposal,
    Agg,
    FactKey,
    LedgerEntry,
    UNKNOWN,
    PublishFailed,
)
from .availability import (
    DEVICE,
    SERVICE,
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    AccessStats,
    AvailabilityIndex,
    AvailabilityReport,
    AvailabilitySegment,
    Dependency,
    DependencyCycleError,
    DeviceEvidence,
    DownRef,
    EdgeRef,
    MonitorRecord,
    SegmentBasis,
    ServiceVerdict,
    recompute_from_basis,
)

__all__ = [
    "Interval",
    "Event",
    "EventStore",
    "RevisionConflict",
    "RevisionRejected",
    "ACCEPT",
    "NOOP",
    "REJECT",
    "LOC",
    "WO",
    "DOWN",
    "SPARE",
    "MaintenanceFactEngine",
    "Proposal",
    "Agg",
    "FactKey",
    "LedgerEntry",
    "UNKNOWN",
    "PublishFailed",
    "DEVICE",
    "SERVICE",
    "STATE_AVAILABLE",
    "STATE_UNAVAILABLE",
    "STATE_UNKNOWN",
    "AccessStats",
    "AvailabilityIndex",
    "AvailabilityReport",
    "AvailabilitySegment",
    "Dependency",
    "DependencyCycleError",
    "DeviceEvidence",
    "DownRef",
    "EdgeRef",
    "MonitorRecord",
    "SegmentBasis",
    "ServiceVerdict",
    "recompute_from_basis",
]
