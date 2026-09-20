"""履约核心的领域测试：重叠识别、幂等留痕、节点替换、认证规则与投影还原。"""

import unittest
from datetime import datetime

from fulfillment.core import FulfillmentCore
from fulfillment.model import (
    Certification,
    CertificationRule,
    DomainError,
    Equipment,
    UTC,
    parse_ts,
)

FIXED_NOW = datetime(2027, 2, 1, tzinfo=UTC)


def make_core():
    return FulfillmentCore(
        inventory=[
            Equipment(
                "TBL-001",
                "table",
                "Shenzhen",
                [Certification("ITTF-T2027", parse_ts("2026-06-01T00:00:00Z"), parse_ts("2028-05-31T00:00:00Z"))],
            ),
            Equipment(
                "TBL-002",
                "table",
                "Shenzhen",
                [Certification("ITTF-T2027", parse_ts("2026-06-01T00:00:00Z"), parse_ts("2028-05-31T00:00:00Z"))],
            ),
            Equipment(
                "TBL-003",
                "table",
                "Lyon",
                [Certification("ITTF-T2024", parse_ts("2024-03-01T00:00:00Z"), parse_ts("2027-03-31T00:00:00Z"))],
            ),
        ],
        cert_rules=[
            CertificationRule(
                "CR-TABLE-2024",
                "table",
                "ITTF-T2024",
                parse_ts("2024-01-01T00:00:00Z"),
                effective_to=parse_ts("2026-12-31T23:59:59Z"),
            ),
            CertificationRule(
                "CR-TABLE-2027",
                "table",
                "ITTF-T2027",
                parse_ts("2027-01-01T00:00:00Z"),
                min_valid_days=30,
            ),
        ],
        technicians=[{"ref": "TECH-A"}, {"ref": "TECH-B"}],
        clock=lambda: FIXED_NOW,
    )


def match_payload(match_id, city, start, end, requirements, event="singles", stage="final"):
    return {
        "match_id": match_id,
        "event": event,
        "city": city,
        "venue": f"{city}-Arena",
        "stage": stage,
        "start": start,
        "end": end,
        "responsible": "运营-负责人",
        "requirements": requirements,
    }


def table_req(legs=None, ref=None):
    req = {"kind": "table"}
    if ref:
        req["ref"] = ref
    if legs:
        req["legs"] = legs
    return req


LEG_TO_CHENGDU = {
    "leg_id": "LEG-1",
    "origin": "Shenzhen",
    "destination": "Chengdu",
    "depart_at": "2027-03-08T00:00:00+08:00",
    "arrive_at": "2027-03-09T08:00:00+08:00",
    "carrier": "CTU-Cargo",
}

# 成都混团决赛：要求就位时间 = 开赛 2027-03-10T11:00+08 往前 12 小时。
MATCH_M1 = match_payload(
    "M1",
    "Chengdu",
    "2027-03-10T11:00:00+08:00",
    "2027-03-10T13:00:00+08:00",
    [table_req(legs=[LEG_TO_CHENGDU])],
    event="mixed-team",
)


def intake(core, version_id, matches, issued_at="2027-02-01T09:00:00+08:00"):
    return core.intake_schedule(
        {"version_id": version_id, "issued_at": issued_at, "matches": matches}
    )


def confirm_first(core, proposal, ref, confirmed_by="运营-负责人"):
    option = next(o for o in proposal["options"] if o["resource_ref"] == ref)
    return core.confirm_proposal(
        proposal["proposal_id"], option["option_id"], confirmed_by
    )


class ScheduleIntakeTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()

    def test_naive_time_rejected(self):
        bad = dict(MATCH_M1, start="2027-03-10T11:00:00")
        with self.assertRaises(DomainError) as ctx:
            intake(self.core, "v1", [bad])
        self.assertEqual(ctx.exception.status, 400)

    def test_overlap_within_schedule_detected(self):
        m2 = match_payload(
            "M2",
            "Macao",
            "2027-03-10T10:00:00+08:00",
            "2027-03-10T12:00:00+08:00",
            [table_req(ref="TBL-001")],
            event="doubles",
        )
        m1 = dict(MATCH_M1, requirements=[table_req(ref="TBL-001")])
        result = intake(self.core, "v1", [m1, m2])
        kinds = {(c["type"], c["resource_ref"]) for c in result["conflicts"]}
        self.assertIn(("within_schedule", "TBL-001"), kinds)

    def test_overlap_with_existing_commitment_detected(self):
        # 确认 M1 的球台承诺（TBL-001）。
        result = intake(self.core, "v1", [MATCH_M1])
        confirm_first(self.core, result["proposals"][0], "TBL-001")
        # 新版赛历：澳门双打同一时段也要 TBL-001。
        m2 = match_payload(
            "M2",
            "Macao",
            "2027-03-10T10:00:00+08:00",
            "2027-03-10T12:00:00+08:00",
            [table_req(ref="TBL-001")],
            event="doubles",
        )
        result2 = intake(self.core, "v2", [dict(MATCH_M1), m2])
        kinds = {(c["type"], c["resource_ref"]) for c in result2["conflicts"]}
        self.assertIn(("existing_commitment", "TBL-001"), kinds)

    def test_technician_overlap_detected(self):
        req = [{"kind": "technician", "ref": "TECH-A"}]
        m1 = dict(MATCH_M1, requirements=req)
        m2 = match_payload(
            "M2",
            "Macao",
            "2027-03-10T10:00:00+08:00",
            "2027-03-10T12:00:00+08:00",
            req,
            event="doubles",
        )
        result = intake(self.core, "v1", [m1, m2])
        self.assertIn(
            ("within_schedule", "TECH-A"),
            {(c["type"], c["resource_ref"]) for c in result["conflicts"]},
        )

    def test_same_version_replay_is_idempotent(self):
        first = intake(self.core, "v1", [MATCH_M1])
        second = intake(self.core, "v1", [MATCH_M1])
        self.assertEqual(first, second)
        self.assertEqual(
            len(self.core.ledger.of_type("schedule_version_received")), 1
        )

    def test_same_version_different_content_conflicts(self):
        intake(self.core, "v1", [MATCH_M1])
        changed = dict(MATCH_M1, end="2027-03-10T14:00:00+08:00")
        with self.assertRaises(DomainError) as ctx:
            intake(self.core, "v1", [changed])
        self.assertEqual(ctx.exception.status, 409)


class ProposalTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.result = intake(self.core, "v1", [MATCH_M1])
        self.proposal = self.result["proposals"][0]

    def test_options_carry_buffer_and_rationale(self):
        self.assertTrue(self.proposal["options"])
        for option in self.proposal["options"]:
            self.assertIn("buffer_minutes", option)
            self.assertTrue(option["rationale"])
        best = self.proposal["options"][0]
        # 要求就位 2027-03-09T23:00+08，末段航段 08:00+08 抵达，余量 900 分钟。
        self.assertEqual(best["buffer_minutes"], 900)

    def test_uncertified_candidate_rejected_with_reason(self):
        rejected = {r["ref"] for r in self.proposal["rejected_candidates"]}
        self.assertIn("TBL-003", rejected)  # 只有 ITTF-T2024，不满足 2027 规则
        option_refs = {o["resource_ref"] for o in self.proposal["options"]}
        self.assertNotIn("TBL-003", option_refs)

    def test_cert_rule_effective_at_match_time(self):
        # 2026 年的比赛适用 CR-TABLE-2024，TBL-003 的 ITTF-T2024 认证可用。
        old_match = match_payload(
            "M0",
            "Montpellier",
            "2026-11-10T19:00:00+01:00",
            "2026-11-10T21:00:00+01:00",
            [table_req()],
        )
        result = intake(self.core, "v2026", [old_match])
        refs = {o["resource_ref"] for o in result["proposals"][0]["options"]}
        self.assertIn("TBL-003", refs)

    def test_confirm_creates_commitment_with_confirmer(self):
        commitment = confirm_first(self.core, self.proposal, "TBL-001")
        self.assertEqual(commitment.state, "CONFIRMED")
        self.assertEqual(commitment.confirmed_by, "运营-负责人")
        self.assertEqual(commitment.resource_ref, "TBL-001")

    def test_double_confirm_blocked_after_option_taken(self):
        confirm_first(self.core, self.proposal, "TBL-001")
        m2 = match_payload(
            "M2",
            "Macao",
            "2027-03-10T10:00:00+08:00",
            "2027-03-10T12:00:00+08:00",
            [table_req()],
            event="doubles",
        )
        result2 = intake(self.core, "v2", [dict(MATCH_M1), m2])
        proposal2 = result2["proposals"][0]
        stale = next(
            o for o in proposal2["options"] if o["resource_ref"] == "TBL-001"
        ) if any(o["resource_ref"] == "TBL-001" for o in proposal2["options"]) else None
        # TBL-001 已被 M1 占用，要么不在候选里，要么确认时被拦截。
        if stale is not None:
            with self.assertRaises(DomainError) as ctx:
                self.core.confirm_proposal(
                    proposal2["proposal_id"], stale["option_id"], "运营-负责人"
                )
            self.assertEqual(ctx.exception.status, 409)


class HandoverAndCallbackTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        result = intake(self.core, "v1", [MATCH_M1])
        self.commitment = confirm_first(self.core, result["proposals"][0], "TBL-001")

    def test_offline_scanner_replay_is_idempotent(self):
        payload = {
            "commitment_id": self.commitment.commitment_id,
            "node": "venue:Chengdu",
            "occurred_at": "2027-03-09T20:00:00+08:00",
            "party": "扫描枪-SG07",
            "idempotency_key": "SG07-0042",
        }
        first = self.core.record_handover(payload)
        self.assertFalse(first["replay"])
        replay = self.core.record_handover(dict(payload))
        self.assertTrue(replay["replay"])
        self.assertEqual(first["event"]["event_id"], replay["event"]["event_id"])
        self.assertEqual(len(self.core.ledger.of_type("handover_recorded")), 1)

    def test_conflicting_handover_same_node_rejected(self):
        payload = {
            "commitment_id": self.commitment.commitment_id,
            "node": "venue:Chengdu",
            "occurred_at": "2027-03-09T20:00:00+08:00",
            "party": "扫描枪-SG07",
            "idempotency_key": "SG07-0042",
        }
        self.core.record_handover(payload)
        with self.assertRaises(DomainError) as ctx:
            self.core.record_handover(dict(payload, idempotency_key="SG07-0099"))
        self.assertEqual(ctx.exception.status, 409)

    def test_handover_time_normalized_to_utc(self):
        self.core.record_handover(
            {
                "commitment_id": self.commitment.commitment_id,
                "node": "venue:Chengdu",
                "occurred_at": "2027-03-09T20:00:00+08:00",
                "party": "扫描枪-SG07",
                "idempotency_key": "SG07-0042",
            }
        )
        event = self.core.ledger.of_type("handover_recorded")[0]
        self.assertEqual(event["occurred_at"], "2027-03-09T12:00:00Z")

    def test_duplicate_carrier_callback_deduped(self):
        payload = {
            "callback_id": "cb-1",
            "leg_id": "LEG-1",
            "status": "arrived",
            "occurred_at": "2027-03-09T08:00:00+08:00",
        }
        first = self.core.carrier_callback(payload)
        self.assertFalse(first["replay"])
        # 承运人重试：同一回调号。
        self.assertTrue(self.core.carrier_callback(dict(payload))["replay"])
        # 换个回调号但业务事实相同（跨时区表示同一时刻）。
        same_fact = dict(payload, callback_id="cb-2", occurred_at="2027-03-09T00:00:00Z")
        self.assertTrue(self.core.carrier_callback(same_fact)["replay"])
        self.assertEqual(len(self.core.ledger.of_type("leg_status_reported")), 1)
        self.assertEqual(self.core.leg_facts["LEG-1"]["status"], "arrived")


class ReassignAndReplacementTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        result = intake(self.core, "v1", [MATCH_M1])
        self.commitment = confirm_first(self.core, result["proposals"][0], "TBL-001")

    def test_reassign_allowed_before_dispatch(self):
        outcome = self.core.reassign(
            self.commitment.commitment_id,
            {"new_ref": "TBL-002", "party": "运营-负责人", "reason": "计划调整"},
        )
        self.assertEqual(outcome["commitment"]["resource_ref"], "TBL-002")

    def test_reassign_forbidden_after_dispatch(self):
        self.core.dispatch(
            {
                "commitment_id": self.commitment.commitment_id,
                "leg_id": "LEG-1",
                "occurred_at": "2027-03-08T01:00:00+08:00",
                "party": "仓库",
                "idempotency_key": "disp-1",
            }
        )
        with self.assertRaises(DomainError) as ctx:
            self.core.reassign(
                self.commitment.commitment_id, {"new_ref": "TBL-002"}
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("替换", ctx.exception.message)

    def test_customs_hold_triggers_replacement_with_cert_check(self):
        outcome = self.core.report_incident(
            {
                "type": "customs_hold",
                "commitment_id": self.commitment.commitment_id,
                "node": "leg:LEG-1",
                "occurred_at": "2027-03-08T09:00:00+08:00",
                "reported_by": "承运人",
            }
        )
        proposal = outcome["replacement_proposal"]
        self.assertEqual(proposal["kind"], "replacement")
        self.assertEqual(proposal["node"], "leg:LEG-1")
        refs = {o["resource_ref"] for o in proposal["options"]}
        self.assertIn("TBL-002", refs)
        self.assertNotIn("TBL-001", refs)  # 原设备不再作为候选
        rejected = {r["ref"] for r in proposal["rejected_candidates"]}
        self.assertIn("TBL-003", rejected)  # 不满足比赛当时有效的认证规则
        # 原承诺在确认前保持原状。
        self.assertEqual(self.core.commitments[self.commitment.commitment_id].state, "CONFIRMED")

    def test_confirm_replacement_closes_chain(self):
        outcome = self.core.report_incident(
            {
                "type": "failure",
                "commitment_id": self.commitment.commitment_id,
                "node": "venue:Chengdu",
                "occurred_at": "2027-03-09T21:00:00+08:00",
                "reported_by": "场馆",
            }
        )
        proposal = outcome["replacement_proposal"]
        new_commitment = confirm_first(self.core, proposal, "TBL-002")
        old = self.core.commitments[self.commitment.commitment_id]
        self.assertEqual(old.state, "REPLACED")
        self.assertEqual(old.replaced_by, new_commitment.commitment_id)
        self.assertIn("failure", old.replace_reason)
        self.assertEqual(new_commitment.replaces, old.commitment_id)
        chain = self.core.replacement_chain("M1")
        self.assertEqual(len(chain["chains"]), 1)
        self.assertEqual(
            [c["commitment_id"] for c in chain["chains"][0]],
            [old.commitment_id, new_commitment.commitment_id],
        )


class PreparationViewTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        result = intake(self.core, "v1", [MATCH_M1])
        self.commitment = confirm_first(self.core, result["proposals"][0], "TBL-001")

    def test_preparation_shows_location_next_buffer_confirmer(self):
        self.core.dispatch(
            {
                "commitment_id": self.commitment.commitment_id,
                "leg_id": "LEG-1",
                "occurred_at": "2027-03-08T01:00:00+08:00",
                "party": "仓库",
                "idempotency_key": "disp-1",
            }
        )
        view = self.core.match_preparation("M1")
        self.assertEqual(view["stage"], "final")
        resource = view["resources"][0]
        self.assertEqual(resource["resource_ref"], "TBL-001")
        self.assertIn("运输中", resource["location"]["description"])
        self.assertEqual(resource["next_commitment"]["label"], "场馆交接")
        self.assertEqual(resource["confirmed_by"], "运营-负责人")
        self.assertEqual(resource["risk_buffer_minutes"], 900)
        self.assertFalse(resource["at_risk"])

    def test_preparation_after_handover(self):
        self.core.record_handover(
            {
                "commitment_id": self.commitment.commitment_id,
                "node": "venue:Chengdu",
                "occurred_at": "2027-03-09T20:00:00+08:00",
                "party": "扫描枪-SG07",
                "idempotency_key": "SG07-0042",
            }
        )
        resource = self.core.match_preparation("M1")["resources"][0]
        self.assertIn("已交接", resource["location"]["description"])
        self.assertEqual(resource["next_commitment"]["label"], "比赛开始")
        # 要求就位 23:00+08，实际交接 20:00+08，余量 180 分钟。
        self.assertEqual(resource["risk_buffer_minutes"], 180)


class ScheduleRestoreTest(unittest.TestCase):
    def test_restore_reverses_schedule_change(self):
        core = make_core()
        intake(core, "v1", [MATCH_M1])
        moved = dict(MATCH_M1, start="2027-03-11T11:00:00+08:00", end="2027-03-11T13:00:00+08:00")
        intake(core, "v2", [moved])
        result = core.restore_schedule(
            "v1",
            {"requested_by": "运营-负责人", "issued_at": "2027-02-02T09:00:00+08:00"},
        )
        self.assertEqual(core.active_version_id, "v1-restore")
        history = core.schedule_history("M1")["history"]
        self.assertEqual([h["version_id"] for h in history], ["v1", "v2", "v1-restore"])
        self.assertEqual(history[-1]["restores"], "v1")
        restored = history[-1]["snapshot"]
        self.assertEqual(restored["start"], "2027-03-10T03:00:00Z")
        self.assertEqual(result["supersedes"], "v2")


if __name__ == "__main__":
    unittest.main()
