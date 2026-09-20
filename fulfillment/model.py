"""资源履约领域模型：场馆时段、比赛阶段、设备序列号、认证有效期、物流航段与责任方。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

RESOURCE_KINDS = ("table", "scoring", "broadcast", "technician")

# 各类资源赛前就位所需的最小缓冲（分钟）：台面认证、转播联调等都在此期间完成。
SETUP_BUFFER_MINUTES = {
    "table": 12 * 60,
    "scoring": 6 * 60,
    "broadcast": 8 * 60,
    "technician": 24 * 60,
}

# 赛后撤收缓冲（分钟），用于圈定资源被承诺占用的完整窗口。
TEARDOWN_BUFFER_MINUTES = {
    "table": 4 * 60,
    "scoring": 2 * 60,
    "broadcast": 4 * 60,
    "technician": 2 * 60,
}

# 承诺生命周期：确认 → 启运 → 交接 → 使用 → 释放；REPLACED 为被替换后的终态。
COMMITMENT_STATES = (
    "CONFIRMED",
    "DISPATCHED",
    "HANDED_OVER",
    "IN_USE",
    "RELEASED",
    "REPLACED",
)

# 交接或启运以后进入锁定状态：账面上不允许直接改派，只能对相关节点做替换。
LOCKED_STATES = ("DISPATCHED", "HANDED_OVER", "IN_USE")

INCIDENT_TYPES = ("delay", "customs_hold", "failure")


class DomainError(Exception):
    """携带 HTTP 状态码的领域错误，由 API 层转成响应。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def parse_ts(value, field_name="时间"):
    """把携带时区偏移的 ISO 8601 时间归一为 UTC 时刻；拒绝不带时区的时间。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise DomainError(400, f"{field_name}不是合法的 ISO 8601 时间：{value!r}")
    else:
        raise DomainError(400, f"{field_name}必须是 ISO 8601 字符串，收到：{value!r}")
    if dt.tzinfo is None:
        raise DomainError(400, f"{field_name}必须携带时区偏移：{value!r}")
    return dt.astimezone(UTC)


def to_iso(dt):
    """统一以 UTC Zulu 形式输出，避免跨时区回读歧义。"""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.end <= self.start:
            raise DomainError(400, "时段结束必须晚于开始")

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end

    def to_dict(self):
        return {"start": to_iso(self.start), "end": to_iso(self.end)}


@dataclass(frozen=True)
class Certification:
    """一条设备认证留痕：标准与有效期。"""

    standard: str
    valid_from: datetime
    valid_until: datetime

    def covers(self, window, min_valid_days=0):
        horizon = window.end + timedelta(days=min_valid_days)
        return self.valid_from <= window.start and self.valid_until >= horizon

    def to_dict(self):
        return {
            "standard": self.standard,
            "valid_from": to_iso(self.valid_from),
            "valid_until": to_iso(self.valid_until),
        }


@dataclass(frozen=True)
class CertificationRule:
    """认证规则按生效区间留痕；判断候选设备时取比赛当时有效的规则。"""

    rule_id: str
    kind: str
    standard: str
    effective_from: datetime
    effective_to: datetime | None = None
    min_valid_days: int = 0

    def in_effect_at(self, moment):
        return self.effective_from <= moment and (
            self.effective_to is None or moment < self.effective_to
        )

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "standard": self.standard,
            "effective_from": to_iso(self.effective_from),
            "effective_to": to_iso(self.effective_to) if self.effective_to else None,
            "min_valid_days": self.min_valid_days,
        }


@dataclass
class Equipment:
    serial: str
    kind: str
    home: str
    certifications: list = field(default_factory=list)

    def to_dict(self):
        return {
            "serial": self.serial,
            "kind": self.kind,
            "home": self.home,
            "certifications": [c.to_dict() for c in self.certifications],
        }


@dataclass(frozen=True)
class Leg:
    """物流航段留痕：起讫、计划时刻与承运人。"""

    leg_id: str
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    carrier: str = ""

    def to_dict(self):
        return {
            "leg_id": self.leg_id,
            "origin": self.origin,
            "destination": self.destination,
            "depart_at": to_iso(self.depart_at),
            "arrive_at": to_iso(self.arrive_at),
            "carrier": self.carrier,
        }


@dataclass(frozen=True)
class Requirement:
    """一场比赛对一类资源的需求；ref 为空表示按类型分配。"""

    kind: str
    ref: str | None = None
    legs: tuple = ()


@dataclass(frozen=True)
class Match:
    match_id: str
    event: str
    city: str
    venue: str
    stage: str
    window: Window
    requirements: tuple = ()
    responsible: str = "赛事运营中心"

    def to_dict(self):
        return {
            "match_id": self.match_id,
            "event": self.event,
            "city": self.city,
            "venue": self.venue,
            "stage": self.stage,
            "start": to_iso(self.window.start),
            "end": to_iso(self.window.end),
            "responsible": self.responsible,
            "requirements": [
                {
                    "kind": r.kind,
                    "ref": r.ref,
                    "legs": [leg.to_dict() for leg in r.legs],
                }
                for r in self.requirements
            ],
        }


@dataclass
class Commitment:
    """一项资源对一场比赛的书面承诺；启运或交接后锁定，只能被替换。"""

    commitment_id: str
    match_id: str
    resource_ref: str
    kind: str
    usage: Window
    legs: list = field(default_factory=list)
    owner: str = "赛事运营中心"
    state: str = "CONFIRMED"
    confirmed_by: str = ""
    confirmed_at: datetime | None = None
    replaces: str | None = None
    replaced_by: str | None = None
    replace_reason: str = ""

    def required_arrival(self):
        return self.usage.start - timedelta(minutes=SETUP_BUFFER_MINUTES[self.kind])

    def occupied_window(self):
        """承诺占用的完整窗口：航段、赛前就位缓冲、比赛与撤收。"""
        start = self.required_arrival()
        if self.legs:
            start = min(start, min(leg.depart_at for leg in self.legs))
        end = self.usage.end + timedelta(minutes=TEARDOWN_BUFFER_MINUTES[self.kind])
        return Window(start, end)

    def to_dict(self):
        return {
            "commitment_id": self.commitment_id,
            "match_id": self.match_id,
            "resource_ref": self.resource_ref,
            "kind": self.kind,
            "state": self.state,
            "owner": self.owner,
            "usage": self.usage.to_dict(),
            "legs": [leg.to_dict() for leg in self.legs],
            "confirmed_by": self.confirmed_by,
            "confirmed_at": to_iso(self.confirmed_at) if self.confirmed_at else None,
            "replaces": self.replaces,
            "replaced_by": self.replaced_by,
            "replace_reason": self.replace_reason,
        }


@dataclass(frozen=True)
class ProposalOption:
    """一个可确认的方案选项：候选资源、时间余量与选择理由。"""

    option_id: str
    resource_ref: str
    buffer_minutes: int
    rationale: str
    legs: tuple = ()

    def to_dict(self):
        return {
            "option_id": self.option_id,
            "resource_ref": self.resource_ref,
            "buffer_minutes": self.buffer_minutes,
            "rationale": self.rationale,
            "legs": [leg.to_dict() for leg in self.legs],
        }


@dataclass
class Proposal:
    """待负责人确认的方案；确认前不落任何承诺。"""

    proposal_id: str
    kind: str  # allocation（赛历分配）或 replacement（节点替换）
    match_id: str
    requirement_kind: str
    window: Window
    options: list = field(default_factory=list)
    rejected_candidates: list = field(default_factory=list)
    status: str = "PENDING"  # PENDING / CONFIRMED / REJECTED
    assignee: str = "赛事运营中心"
    node: str = ""
    incident_id: str | None = None
    target_commitment: str | None = None
    created_at: datetime | None = None

    def to_dict(self):
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "match_id": self.match_id,
            "requirement_kind": self.requirement_kind,
            "window": self.window.to_dict(),
            "status": self.status,
            "assignee": self.assignee,
            "node": self.node,
            "incident_id": self.incident_id,
            "target_commitment": self.target_commitment,
            "created_at": to_iso(self.created_at) if self.created_at else None,
            "options": [o.to_dict() for o in self.options],
            "rejected_candidates": list(self.rejected_candidates),
        }
