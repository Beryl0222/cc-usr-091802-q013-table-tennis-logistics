"""资源履约核心：赛历接收、重叠承诺识别、方案确认、节点替换与准备页投影。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .ledger import Ledger
from .model import (
    COMMITMENT_STATES,
    INCIDENT_TYPES,
    LOCKED_STATES,
    RESOURCE_KINDS,
    SETUP_BUFFER_MINUTES,
    Certification,
    CertificationRule,
    Commitment,
    DomainError,
    Equipment,
    Leg,
    Match,
    Proposal,
    ProposalOption,
    Requirement,
    UTC,
    Window,
    new_id,
    parse_ts,
    to_iso,
)


def _require(payload, key, label):
    value = payload.get(key)
    if value is None or value == "":
        raise DomainError(400, f"缺少必填字段：{label}（{key}）")
    return value


class FulfillmentCore:
    """履约服务的领域核心，不依赖 HTTP 层，便于直接测试。"""

    def __init__(self, inventory=(), cert_rules=(), technicians=(), clock=None):
        self._clock = clock or (lambda: datetime.now(UTC))
        self.ledger = Ledger(clock=self._clock)
        self.equipment = {e.serial: e for e in inventory}
        self.cert_rules = sorted(cert_rules, key=lambda r: r.effective_from)
        self.technicians = {t["ref"]: dict(t) for t in technicians}
        self.versions = {}
        self.active_version_id = None
        self.commitments = {}
        self.proposals = {}
        self.incidents = {}
        self.handovers = {}  # (commitment_id, node) -> 交接事实
        self.leg_facts = {}  # leg_id -> 最新航段事实

    # ------------------------------------------------------------------
    # 基础数据装载
    # ------------------------------------------------------------------
    @classmethod
    def load_fixture(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        inventory = [
            Equipment(
                serial=item["serial"],
                kind=item["kind"],
                home=item.get("home", ""),
                certifications=[
                    Certification(
                        standard=c["standard"],
                        valid_from=parse_ts(c["valid_from"], "认证生效时间"),
                        valid_until=parse_ts(c["valid_until"], "认证失效时间"),
                    )
                    for c in item.get("certifications", [])
                ],
            )
            for item in data.get("inventory", [])
        ]
        rules = [
            CertificationRule(
                rule_id=r["rule_id"],
                kind=r["kind"],
                standard=r["standard"],
                effective_from=parse_ts(r["effective_from"], "规则生效时间"),
                effective_to=(
                    parse_ts(r["effective_to"], "规则失效时间")
                    if r.get("effective_to")
                    else None
                ),
                min_valid_days=r.get("min_valid_days", 0),
            )
            for r in data.get("certification_rules", [])
        ]
        return cls(
            inventory=inventory,
            cert_rules=rules,
            technicians=data.get("technicians", []),
        )

    # ------------------------------------------------------------------
    # 认证规则
    # ------------------------------------------------------------------
    def rule_in_effect(self, kind, moment):
        """取某一时刻对某类资源有效的认证规则（最新生效者）。"""
        effective = [
            r for r in self.cert_rules if r.kind == kind and r.in_effect_at(moment)
        ]
        return effective[-1] if effective else None

    def check_certification(self, ref, kind, window):
        """核对候选设备是否满足比赛当时有效的认证规则。"""
        rule = self.rule_in_effect(kind, window.start)
        if rule is None:
            return True, "该时段无生效的认证规则，默认放行", None
        equipment = self.equipment.get(ref)
        if equipment is None:
            return False, f"设备 {ref} 不在器材档案中", rule
        for cert in equipment.certifications:
            if cert.standard == rule.standard and cert.covers(
                window, rule.min_valid_days
            ):
                return True, (
                    f"认证 {cert.standard} 有效期至 {to_iso(cert.valid_until)}，"
                    f"覆盖比赛窗口并满足规则 {rule.rule_id}"
                ), rule
        return (
            False,
            f"设备 {ref} 缺少满足规则 {rule.rule_id}（{rule.standard}）的有效认证",
            rule,
        )

    # ------------------------------------------------------------------
    # 赛历接收：归一化、重叠识别、生成待确认方案
    # ------------------------------------------------------------------
    def intake_schedule(self, payload):
        version_id = _require(payload, "version_id", "赛历版本号")
        issued_at = parse_ts(
            _require(payload, "issued_at", "签发时间"), "赛历签发时间"
        )
        matches = [self._parse_match(m) for m in payload.get("matches", [])]
        fingerprint = json.dumps(
            [m.to_dict() for m in matches], sort_keys=True, ensure_ascii=False
        )
        if version_id in self.versions:
            existing = self.versions[version_id]
            if existing["fingerprint"] == fingerprint:
                return existing["intake_result"]  # 同一版本重复推送，幂等返回
            raise DomainError(409, f"赛历版本 {version_id} 已存在且内容不一致")

        supersedes = self.active_version_id
        diff = self._diff_versions(
            self.versions[supersedes]["matches"] if supersedes else {}, matches
        )
        conflicts = self._detect_conflicts(matches)

        proposals = []
        for match in matches:
            for req in match.requirements:
                if self._covered_by_active_commitment(match, req):
                    continue
                proposals.append(self._build_allocation_proposal(match, req))

        self.ledger.append(
            "schedule_version_received",
            {
                "version_id": version_id,
                "supersedes": supersedes,
                "match_count": len(matches),
                "conflict_count": len(conflicts),
            },
            occurred_at=issued_at,
        )
        result = {
            "version_id": version_id,
            "issued_at": to_iso(issued_at),
            "supersedes": supersedes,
            "diff": diff,
            "conflicts": conflicts,
            "proposals": [p.to_dict() for p in proposals],
        }
        self.versions[version_id] = {
            "version_id": version_id,
            "issued_at": issued_at,
            "matches": {m.match_id: m for m in matches},
            "fingerprint": fingerprint,
            "restores": payload.get("restores"),
            "intake_result": result,
        }
        self.active_version_id = version_id
        return result

    def _parse_match(self, raw):
        window = Window(
            parse_ts(_require(raw, "start", "比赛开始时间"), "比赛开始时间"),
            parse_ts(_require(raw, "end", "比赛结束时间"), "比赛结束时间"),
        )
        requirements = []
        for req in raw.get("requirements", []):
            kind = _require(req, "kind", "资源类型")
            if kind not in RESOURCE_KINDS:
                raise DomainError(400, f"未知资源类型：{kind}")
            legs = tuple(
                Leg(
                    leg_id=_require(leg, "leg_id", "航段号"),
                    origin=_require(leg, "origin", "航段起点"),
                    destination=_require(leg, "destination", "航段终点"),
                    depart_at=parse_ts(
                        _require(leg, "depart_at", "航段出发时间"), "航段出发时间"
                    ),
                    arrive_at=parse_ts(
                        _require(leg, "arrive_at", "航段到达时间"), "航段到达时间"
                    ),
                    carrier=leg.get("carrier", ""),
                )
                for leg in req.get("legs", [])
            )
            requirements.append(Requirement(kind=kind, ref=req.get("ref"), legs=legs))
        return Match(
            match_id=_require(raw, "match_id", "比赛编号"),
            event=_require(raw, "event", "赛事项目"),
            city=_require(raw, "city", "城市"),
            venue=_require(raw, "venue", "场馆"),
            stage=_require(raw, "stage", "比赛阶段"),
            window=window,
            requirements=tuple(requirements),
            responsible=raw.get("responsible", "赛事运营中心"),
        )

    def _diff_versions(self, old_matches, new_matches):
        diff = {"added": [], "removed": [], "modified": []}
        new_by_id = {m.match_id: m for m in new_matches}
        for match_id, old in old_matches.items():
            if match_id not in new_by_id:
                diff["removed"].append(match_id)
        for match_id, new in new_by_id.items():
            old = old_matches.get(match_id)
            if old is None:
                diff["added"].append(match_id)
                continue
            changes = {}
            for field_name in ("event", "city", "venue", "stage"):
                if getattr(old, field_name) != getattr(new, field_name):
                    changes[field_name] = {
                        "before": getattr(old, field_name),
                        "after": getattr(new, field_name),
                    }
            if old.window != new.window:
                changes["window"] = {
                    "before": old.window.to_dict(),
                    "after": new.window.to_dict(),
                }
            if changes:
                diff["modified"].append({"match_id": match_id, "changes": changes})
        return diff

    def _needed_window(self, match, req):
        """一项需求需要占用资源的完整窗口（含航段与就位缓冲）。"""
        start = match.window.start - timedelta(
            minutes=SETUP_BUFFER_MINUTES[req.kind]
        )
        if req.legs:
            start = min(start, min(leg.depart_at for leg in req.legs))
        return Window(start, match.window.end)

    def _active_commitments(self):
        return [c for c in self.commitments.values() if c.state != "REPLACED"]

    def _detect_conflicts(self, matches):
        """识别设备及技术人员的重叠承诺：赛历内部互撞 + 与账上已有承诺相撞。"""
        conflicts = []
        claims = []  # (ref, match_id, window, source)
        for match in matches:
            for req in match.requirements:
                if req.ref:
                    claims.append(
                        (req.ref, match.match_id, self._needed_window(match, req))
                    )
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                ref_a, match_a, win_a = claims[i]
                ref_b, match_b, win_b = claims[j]
                if ref_a == ref_b and win_a.overlaps(win_b):
                    conflicts.append(
                        {
                            "type": "within_schedule",
                            "resource_ref": ref_a,
                            "matches": [match_a, match_b],
                            "detail": (
                                f"新版赛历中 {ref_a} 被 {match_a} 与 {match_b} "
                                "同时承诺，占用窗口重叠"
                            ),
                        }
                    )
        for ref, match_id, window in claims:
            for commitment in self._active_commitments():
                if (
                    commitment.resource_ref == ref
                    and commitment.match_id != match_id
                    and commitment.occupied_window().overlaps(window)
                ):
                    conflicts.append(
                        {
                            "type": "existing_commitment",
                            "resource_ref": ref,
                            "matches": [commitment.match_id, match_id],
                            "existing_commitment": commitment.commitment_id,
                            "detail": (
                                f"{ref} 已承诺给 {commitment.match_id}"
                                f"（{commitment.commitment_id}），"
                                f"与 {match_id} 的占用窗口重叠"
                            ),
                        }
                    )
        return conflicts

    def _covered_by_active_commitment(self, match, req):
        for commitment in self._active_commitments():
            if commitment.match_id != match.match_id or commitment.kind != req.kind:
                continue
            if req.ref and commitment.resource_ref != req.ref:
                continue
            if commitment.usage == match.window:
                return True
        return False

    def _candidate_refs(self, req):
        refs = []
        if req.ref:
            refs.append(req.ref)
        if req.kind == "technician":
            pool = sorted(self.technicians)
        else:
            pool = sorted(
                serial
                for serial, item in self.equipment.items()
                if item.kind == req.kind
            )
        for ref in pool:
            if ref not in refs:
                refs.append(ref)
        return refs

    def _evaluate_candidate(self, ref, kind, window, legs, ignore_commitment=None):
        """返回 (ok, reason, buffer_minutes)；ok 为 False 时 reason 即拒绝理由。"""
        if kind != "technician" and ref not in self.equipment:
            return False, f"设备 {ref} 不在器材档案中", 0
        if kind == "technician" and ref not in self.technicians:
            return False, f"技术人员 {ref} 不在名册中", 0
        ok, reason, _rule = self.check_certification(ref, kind, window)
        if not ok:
            return False, reason, 0
        for commitment in self._active_commitments():
            if commitment.commitment_id == ignore_commitment:
                continue
            if commitment.resource_ref == ref and commitment.occupied_window().overlaps(
                window
            ):
                return (
                    False,
                    f"与已有承诺 {commitment.commitment_id}"
                    f"（{commitment.match_id}）占用窗口重叠",
                    0,
                )
        required_arrival = window.start
        estimated_arrival = (
            max(leg.arrive_at for leg in legs) if legs else required_arrival
        )
        buffer_minutes = int(
            (required_arrival - estimated_arrival).total_seconds() // 60
        )
        if buffer_minutes < 0:
            return (
                False,
                f"时间余量不足：预计 {to_iso(estimated_arrival)} 就位，"
                f"晚于要求就位时间 {to_iso(required_arrival)}",
                buffer_minutes,
            )
        return True, "", buffer_minutes

    def _build_allocation_proposal(self, match, req):
        window = Window(
            match.window.start - timedelta(minutes=SETUP_BUFFER_MINUTES[req.kind]),
            match.window.end,
        )
        options = []
        rejected = []
        for ref in self._candidate_refs(req):
            ok, reason, buffer = self._evaluate_candidate(ref, req.kind, window, req.legs)
            if not ok:
                rejected.append({"ref": ref, "reason": reason})
                continue
            rationale = self._rationale(ref, req.kind, window, buffer, req.legs)
            options.append(
                ProposalOption(
                    option_id=new_id("opt"),
                    resource_ref=ref,
                    buffer_minutes=buffer,
                    rationale=rationale,
                    legs=req.legs,
                )
            )
        options.sort(key=lambda o: o.buffer_minutes, reverse=True)
        proposal = Proposal(
            proposal_id=new_id("prop"),
            kind="allocation",
            match_id=match.match_id,
            requirement_kind=req.kind,
            window=window,
            options=options,
            rejected_candidates=rejected,
            assignee=match.responsible,
            created_at=self._clock(),
        )
        self.proposals[proposal.proposal_id] = proposal
        self.ledger.append(
            "proposal_created",
            {
                "proposal_id": proposal.proposal_id,
                "kind": proposal.kind,
                "match_id": match.match_id,
                "requirement_kind": req.kind,
                "option_count": len(options),
            },
        )
        return proposal

    def _rationale(self, ref, kind, window, buffer, legs):
        parts = [f"选用 {ref}"]
        ok, cert_reason, _ = (
            self.check_certification(ref, kind, window)
            if kind != "technician"
            else (True, "", None)
        )
        if cert_reason:
            parts.append(cert_reason)
        if legs:
            parts.append(
                f"末段航段预计 {to_iso(max(leg.arrive_at for leg in legs))} 抵达"
            )
        parts.append(
            f"距要求就位时间 {to_iso(window.start)} 余量 {buffer} 分钟，无重叠承诺"
        )
        return "；".join(parts) + "。"

    # ------------------------------------------------------------------
    # 方案确认 / 驳回
    # ------------------------------------------------------------------
    def confirm_proposal(self, proposal_id, option_id, confirmed_by):
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise DomainError(404, f"方案 {proposal_id} 不存在")
        if proposal.status != "PENDING":
            raise DomainError(409, f"方案 {proposal_id} 已处理（{proposal.status}）")
        option = next((o for o in proposal.options if o.option_id == option_id), None)
        if option is None:
            raise DomainError(404, f"方案 {proposal_id} 中不存在选项 {option_id}")
        if not confirmed_by:
            raise DomainError(400, "缺少确认人（confirmed_by）")
        # 确认时复核：方案生成后账上可能已落新承诺，失效选项不能确认。
        ok, reason, _buffer = self._evaluate_candidate(
            option.resource_ref,
            proposal.requirement_kind,
            proposal.window,
            list(option.legs),
            ignore_commitment=proposal.target_commitment,
        )
        if not ok:
            raise DomainError(409, f"选项已失效，不能确认：{reason}")

        commitment = Commitment(
            commitment_id=new_id("cmt"),
            match_id=proposal.match_id,
            resource_ref=option.resource_ref,
            kind=proposal.requirement_kind,
            usage=self._usage_window(proposal),
            legs=list(option.legs),
            owner=proposal.assignee,
            confirmed_by=confirmed_by,
            confirmed_at=self._clock(),
        )
        if proposal.kind == "replacement":
            target = self.commitments.get(proposal.target_commitment)
            if target is None:
                raise DomainError(404, "被替换的承诺不存在")
            commitment.replaces = target.commitment_id
            target.state = "REPLACED"
            target.replaced_by = commitment.commitment_id
            incident = self.incidents.get(proposal.incident_id or "")
            target.replace_reason = (
                f"{incident['type']}@{proposal.node}" if incident else proposal.node
            )
            self.ledger.append(
                "commitment_replaced",
                {
                    "old_commitment": target.commitment_id,
                    "new_commitment": commitment.commitment_id,
                    "node": proposal.node,
                    "incident_id": proposal.incident_id,
                    "confirmed_by": confirmed_by,
                },
            )
        self.commitments[commitment.commitment_id] = commitment
        proposal.status = "CONFIRMED"
        self.ledger.append(
            "commitment_created",
            {
                "commitment_id": commitment.commitment_id,
                "proposal_id": proposal_id,
                "match_id": commitment.match_id,
                "resource_ref": commitment.resource_ref,
                "kind": commitment.kind,
                "confirmed_by": confirmed_by,
            },
        )
        return commitment

    def _usage_window(self, proposal):
        start = proposal.window.start + timedelta(
            minutes=SETUP_BUFFER_MINUTES[proposal.requirement_kind]
        )
        return Window(start, proposal.window.end)

    def reject_proposal(self, proposal_id, reason, rejected_by):
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise DomainError(404, f"方案 {proposal_id} 不存在")
        if proposal.status != "PENDING":
            raise DomainError(409, f"方案 {proposal_id} 已处理（{proposal.status}）")
        proposal.status = "REJECTED"
        self.ledger.append(
            "proposal_rejected",
            {
                "proposal_id": proposal_id,
                "reason": reason,
                "rejected_by": rejected_by,
            },
        )
        return proposal

    # ------------------------------------------------------------------
    # 履约事件：启运、交接、承运人回调
    # ------------------------------------------------------------------
    def _get_commitment(self, commitment_id):
        commitment = self.commitments.get(commitment_id)
        if commitment is None:
            raise DomainError(404, f"承诺 {commitment_id} 不存在")
        return commitment

    def dispatch(self, payload):
        commitment = self._get_commitment(_require(payload, "commitment_id", "承诺号"))
        if commitment.state not in ("CONFIRMED", "DISPATCHED"):
            raise DomainError(409, f"承诺当前状态 {commitment.state} 不允许登记启运")
        leg_id = _require(payload, "leg_id", "航段号")
        occurred_at = parse_ts(_require(payload, "occurred_at", "启运时间"), "启运时间")
        event, replay = self.ledger.append(
            "commitment_dispatched",
            {
                "commitment_id": commitment.commitment_id,
                "leg_id": leg_id,
                "party": payload.get("party", ""),
            },
            occurred_at=occurred_at,
            idempotency_key=payload.get("idempotency_key"),
        )
        if not replay:
            commitment.state = "DISPATCHED"
            self.leg_facts[leg_id] = {
                "status": "departed",
                "at": occurred_at,
                "leg_id": leg_id,
            }
        return {"event": event, "replay": replay, "state": commitment.state}

    def record_handover(self, payload):
        """登记交接事实；扫描枪离线补录凭幂等键去重，同一节点只认一条事实。"""
        commitment = self._get_commitment(_require(payload, "commitment_id", "承诺号"))
        node = _require(payload, "node", "交接节点")
        occurred_at = parse_ts(_require(payload, "occurred_at", "交接时间"), "交接时间")
        idempotency_key = _require(payload, "idempotency_key", "幂等键")
        fact_key = (commitment.commitment_id, node)
        existing = self.handovers.get(fact_key)
        if existing is not None and existing["idempotency_key"] != idempotency_key:
            raise DomainError(
                409,
                f"节点 {node} 已有交接事实（{existing['event_id']}），"
                "不同来源的记录不能改写唯一事实",
            )
        event, replay = self.ledger.append(
            "handover_recorded",
            {
                "commitment_id": commitment.commitment_id,
                "node": node,
                "party": _require(payload, "party", "责任方"),
            },
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
        )
        if not replay:
            self.handovers[fact_key] = {
                "event_id": event["event_id"],
                "idempotency_key": idempotency_key,
                "occurred_at": occurred_at,
                "party": payload["party"],
            }
            if commitment.state in ("CONFIRMED", "DISPATCHED"):
                commitment.state = "HANDED_OVER"
        return {"event": event, "replay": replay, "state": commitment.state}

    def carrier_callback(self, payload):
        """承运人回调：按回调号与业务指纹双重去重，时刻统一归一为 UTC。"""
        callback_id = _require(payload, "callback_id", "回调号")
        leg_id = _require(payload, "leg_id", "航段号")
        status = _require(payload, "status", "航段状态")
        occurred_at = parse_ts(_require(payload, "occurred_at", "状态时间"), "状态时间")
        dedup_key = f"leg:{leg_id}:{status}:{to_iso(occurred_at)}"
        event, replay = self.ledger.append(
            "leg_status_reported",
            {"leg_id": leg_id, "status": status, "callback_id": callback_id},
            occurred_at=occurred_at,
            idempotency_key=f"callback:{callback_id}",
            dedup_key=dedup_key,
        )
        if not replay:
            current = self.leg_facts.get(leg_id)
            if current is None or occurred_at >= current["at"]:
                self.leg_facts[leg_id] = {
                    "status": status,
                    "at": occurred_at,
                    "leg_id": leg_id,
                }
        return {"event": event, "replay": replay}

    # ------------------------------------------------------------------
    # 改派限制与节点替换
    # ------------------------------------------------------------------
    def reassign(self, commitment_id, payload):
        """账面改派：仅允许未启运的承诺；交接或启运以后必须走节点替换。"""
        commitment = self._get_commitment(commitment_id)
        if commitment.state in LOCKED_STATES:
            raise DomainError(
                409,
                f"承诺 {commitment_id} 已{commitment.state}，交接或启运以后"
                "不能在账面上直接改派，请对相关节点发起替换",
            )
        if commitment.state in ("RELEASED", "REPLACED"):
            raise DomainError(409, f"承诺 {commitment_id} 已终结，不能改派")
        new_ref = _require(payload, "new_ref", "新资源")
        ok, reason, _buffer = self._evaluate_candidate(
            new_ref,
            commitment.kind,
            Window(commitment.required_arrival(), commitment.usage.end),
            commitment.legs,
            ignore_commitment=commitment.commitment_id,
        )
        if not ok:
            raise DomainError(422, f"候选资源不满足条件：{reason}")
        old_ref = commitment.resource_ref
        commitment.resource_ref = new_ref
        event, _ = self.ledger.append(
            "commitment_reassigned",
            {
                "commitment_id": commitment_id,
                "old_ref": old_ref,
                "new_ref": new_ref,
                "party": payload.get("party", ""),
                "reason": payload.get("reason", ""),
            },
        )
        return {"event": event, "commitment": commitment.to_dict()}

    def report_incident(self, payload):
        """延期、清关受阻、故障：只触发相关节点的替换方案，不改写原承诺。"""
        incident_type = _require(payload, "type", "异常类型")
        if incident_type not in INCIDENT_TYPES:
            raise DomainError(400, f"未知异常类型：{incident_type}")
        commitment = self._get_commitment(_require(payload, "commitment_id", "承诺号"))
        node = _require(payload, "node", "受影响节点")
        occurred_at = parse_ts(
            _require(payload, "occurred_at", "异常发生时间"), "异常发生时间"
        )
        incident_id = new_id("inc")
        self.incidents[incident_id] = {
            "incident_id": incident_id,
            "type": incident_type,
            "commitment_id": commitment.commitment_id,
            "node": node,
            "occurred_at": occurred_at,
            "detail": payload.get("detail", ""),
            "reported_by": payload.get("reported_by", ""),
        }
        self.ledger.append(
            "incident_reported",
            dict(self.incidents[incident_id], occurred_at=to_iso(occurred_at)),
            occurred_at=occurred_at,
        )
        proposal = self._build_replacement_proposal(
            commitment, node, incident_id, incident_type
        )
        return {
            "incident": self.incidents[incident_id],
            "replacement_proposal": proposal.to_dict(),
        }

    def _build_replacement_proposal(self, commitment, node, incident_id, incident_type):
        window = Window(commitment.required_arrival(), commitment.usage.end)
        options = []
        rejected = []
        req = Requirement(kind=commitment.kind)
        for ref in self._candidate_refs(req):
            if ref == commitment.resource_ref:
                continue
            ok, reason, buffer = self._evaluate_candidate(
                ref,
                commitment.kind,
                window,
                commitment.legs,
                ignore_commitment=commitment.commitment_id,
            )
            if not ok:
                rejected.append({"ref": ref, "reason": reason})
                continue
            rationale = (
                f"替换 {commitment.resource_ref}（{incident_type}@{node}）："
                f"候选 {ref} 满足比赛当时有效的认证规则，"
                f"距要求就位时间余量 {buffer} 分钟，无重叠承诺。"
            )
            options.append(
                ProposalOption(
                    option_id=new_id("opt"),
                    resource_ref=ref,
                    buffer_minutes=buffer,
                    rationale=rationale,
                    legs=tuple(commitment.legs),
                )
            )
        options.sort(key=lambda o: o.buffer_minutes, reverse=True)
        proposal = Proposal(
            proposal_id=new_id("prop"),
            kind="replacement",
            match_id=commitment.match_id,
            requirement_kind=commitment.kind,
            window=window,
            options=options,
            rejected_candidates=rejected,
            assignee=commitment.owner,
            node=node,
            incident_id=incident_id,
            target_commitment=commitment.commitment_id,
            created_at=self._clock(),
        )
        self.proposals[proposal.proposal_id] = proposal
        self.ledger.append(
            "proposal_created",
            {
                "proposal_id": proposal.proposal_id,
                "kind": "replacement",
                "match_id": commitment.match_id,
                "node": node,
                "incident_id": incident_id,
                "option_count": len(options),
            },
        )
        return proposal

    # ------------------------------------------------------------------
    # 准备页投影与历史追溯
    # ------------------------------------------------------------------
    def _find_match(self, match_id):
        version = self.versions.get(self.active_version_id or "")
        if version and match_id in version["matches"]:
            return version["matches"][match_id]
        for candidate in self.versions.values():
            if match_id in candidate["matches"]:
                return candidate["matches"][match_id]
        raise DomainError(404, f"比赛 {match_id} 不在任何赛历版本中")

    def _location_of(self, commitment):
        handover_nodes = [
            (node, fact)
            for (cid, node), fact in self.handovers.items()
            if cid == commitment.commitment_id
        ]
        if handover_nodes:
            node, fact = max(handover_nodes, key=lambda item: item[1]["occurred_at"])
            return {"description": f"已交接：{node}", "as_of": to_iso(fact["occurred_at"])}
        leg_facts = [
            self.leg_facts[leg.leg_id] for leg in commitment.legs if leg.leg_id in self.leg_facts
        ]
        if leg_facts:
            latest = max(leg_facts, key=lambda f: f["at"])
            leg = next(l for l in commitment.legs if l.leg_id == latest["leg_id"])
            if latest["status"] == "arrived":
                desc = f"已到达：{leg.destination}"
            else:
                desc = f"运输中：{leg.origin} → {leg.destination}（{latest['status']}）"
            return {"description": desc, "as_of": to_iso(latest["at"])}
        if commitment.legs:
            return {
                "description": f"待启运：{commitment.legs[0].origin}",
                "as_of": None,
            }
        return {"description": "场馆待命", "as_of": None}

    def _next_commitment_of(self, commitment):
        if commitment.state == "CONFIRMED":
            if commitment.legs:
                first = min(commitment.legs, key=lambda l: l.depart_at)
                return {
                    "label": f"启运 {first.origin} → {first.destination}",
                    "at": to_iso(first.depart_at),
                }
            return {"label": "场馆交接", "at": to_iso(commitment.required_arrival())}
        if commitment.state == "DISPATCHED":
            return {"label": "场馆交接", "at": to_iso(commitment.required_arrival())}
        if commitment.state == "HANDED_OVER":
            return {"label": "比赛开始", "at": to_iso(commitment.usage.start)}
        if commitment.state == "IN_USE":
            return {"label": "撤收释放", "at": to_iso(commitment.usage.end)}
        return None

    def _risk_buffer_of(self, commitment):
        required = commitment.required_arrival()
        handover_facts = [
            fact
            for (cid, _node), fact in self.handovers.items()
            if cid == commitment.commitment_id
        ]
        if handover_facts:
            actual = max(f["occurred_at"] for f in handover_facts)
            estimated = actual
        else:
            arrived = [
                self.leg_facts[leg.leg_id]
                for leg in commitment.legs
                if self.leg_facts.get(leg.leg_id, {}).get("status") == "arrived"
            ]
            if arrived:
                estimated = max(f["at"] for f in arrived)
            elif commitment.legs:
                estimated = max(leg.arrive_at for leg in commitment.legs)
            else:
                estimated = required
        minutes = int((required - estimated).total_seconds() // 60)
        return minutes, minutes < 0

    def match_preparation(self, match_id):
        """比赛准备页：每项资源的位置、下一承诺、风险缓冲与确认人。"""
        match = self._find_match(match_id)
        resources = []
        for commitment in sorted(
            (
                c
                for c in self.commitments.values()
                if c.match_id == match_id and c.state != "REPLACED"
            ),
            key=lambda c: c.commitment_id,
        ):
            buffer_minutes, at_risk = self._risk_buffer_of(commitment)
            resources.append(
                {
                    "commitment_id": commitment.commitment_id,
                    "resource_ref": commitment.resource_ref,
                    "kind": commitment.kind,
                    "state": commitment.state,
                    "owner": commitment.owner,
                    "location": self._location_of(commitment),
                    "next_commitment": self._next_commitment_of(commitment),
                    "risk_buffer_minutes": buffer_minutes,
                    "at_risk": at_risk,
                    "confirmed_by": commitment.confirmed_by,
                    "confirmed_at": (
                        to_iso(commitment.confirmed_at)
                        if commitment.confirmed_at
                        else None
                    ),
                }
            )
        pending = [
            p.to_dict()
            for p in self.proposals.values()
            if p.match_id == match_id and p.status == "PENDING"
        ]
        return {
            "match_id": match_id,
            "event": match.event,
            "city": match.city,
            "venue": match.venue,
            "stage": match.stage,
            "window": match.window.to_dict(),
            "resources": resources,
            "pending_proposals": pending,
            "generated_at": to_iso(self._clock()),
        }

    def replacement_chain(self, match_id):
        """一场比赛完整的替换链：从最初承诺到当前生效承诺。"""
        related = [c for c in self.commitments.values() if c.match_id == match_id]
        if not related:
            raise DomainError(404, f"比赛 {match_id} 没有任何承诺记录")
        chains = []
        roots = [c for c in related if c.replaces is None]
        for root in sorted(roots, key=lambda c: c.confirmed_at or self._clock()):
            chain = []
            node = root
            while node is not None:
                chain.append(
                    {
                        "commitment_id": node.commitment_id,
                        "resource_ref": node.resource_ref,
                        "kind": node.kind,
                        "state": node.state,
                        "replace_reason": node.replace_reason,
                        "confirmed_by": node.confirmed_by,
                        "confirmed_at": (
                            to_iso(node.confirmed_at) if node.confirmed_at else None
                        ),
                    }
                )
                node = self.commitments.get(node.replaced_by or "")
            chains.append(chain)
        return {"match_id": match_id, "chains": chains}

    def schedule_history(self, match_id):
        """一场比赛在各赛历版本中的改动轨迹，供反向还原核对。"""
        history = []
        for version in sorted(
            self.versions.values(), key=lambda v: v["issued_at"]
        ):
            match = version["matches"].get(match_id)
            history.append(
                {
                    "version_id": version["version_id"],
                    "issued_at": to_iso(version["issued_at"]),
                    "restores": version["restores"],
                    "present": match is not None,
                    "snapshot": match.to_dict() if match else None,
                }
            )
        if not any(h["present"] for h in history):
            raise DomainError(404, f"比赛 {match_id} 不在任何赛历版本中")
        return {"match_id": match_id, "history": history}

    def restore_schedule(self, version_id, payload):
        """反向还原：以历史版本内容签发新版本，改动同样走冲突识别与确认。"""
        source = self.versions.get(version_id)
        if source is None:
            raise DomainError(404, f"赛历版本 {version_id} 不存在")
        new_version_id = payload.get("new_version_id") or f"{version_id}-restore"
        issued_at = parse_ts(
            payload.get("issued_at", to_iso(self._clock())), "签发时间"
        )
        result = self.intake_schedule(
            {
                "version_id": new_version_id,
                "issued_at": to_iso(issued_at),
                "matches": [m.to_dict() for m in source["matches"].values()],
                "restores": version_id,
            }
        )
        self.ledger.append(
            "schedule_restored",
            {
                "restored_version": version_id,
                "new_version": new_version_id,
                "requested_by": payload.get("requested_by", ""),
            },
            occurred_at=issued_at,
        )
        return result
