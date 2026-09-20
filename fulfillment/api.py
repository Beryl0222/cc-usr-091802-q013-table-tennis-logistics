"""HTTP API 层：把领域核心暴露为 REST 端点。"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .model import DomainError, to_iso


def _version_summary(core):
    return {
        "active_version": core.active_version_id,
        "versions": [
            {
                "version_id": v["version_id"],
                "issued_at": to_iso(v["issued_at"]),
                "restores": v["restores"],
                "match_count": len(v["matches"]),
            }
            for v in core.versions.values()
        ],
    }


def _get_version(core, version_id):
    version = core.versions.get(version_id)
    if version is None:
        raise DomainError(404, f"赛历版本 {version_id} 不存在")
    return version["intake_result"]


def _get_proposal(core, proposal_id):
    proposal = core.proposals.get(proposal_id)
    if proposal is None:
        raise DomainError(404, f"方案 {proposal_id} 不存在")
    return proposal.to_dict()


def _get_commitment(core, commitment_id):
    commitment = core.commitments.get(commitment_id)
    if commitment is None:
        raise DomainError(404, f"承诺 {commitment_id} 不存在")
    return commitment.to_dict()


def make_routes(core, health_payload):
    """声明式路由表：(方法, 路径模式, 处理函数)。"""
    return [
        ("GET", r"/health", lambda m, b: (200, health_payload())),
        ("POST", r"/schedule-versions", lambda m, b: (201, core.intake_schedule(b))),
        ("GET", r"/schedule-versions", lambda m, b: (200, _version_summary(core))),
        (
            "GET",
            r"/schedule-versions/(?P<version_id>[^/]+)",
            lambda m, b: (200, _get_version(core, m["version_id"])),
        ),
        (
            "POST",
            r"/schedule-versions/(?P<version_id>[^/]+)/restore",
            lambda m, b: (201, core.restore_schedule(m["version_id"], b)),
        ),
        (
            "GET",
            r"/proposals/(?P<proposal_id>[^/]+)",
            lambda m, b: (200, _get_proposal(core, m["proposal_id"])),
        ),
        (
            "POST",
            r"/proposals/(?P<proposal_id>[^/]+)/confirm",
            lambda m, b: (
                201,
                core.confirm_proposal(
                    m["proposal_id"],
                    b.get("option_id", ""),
                    b.get("confirmed_by", ""),
                ).to_dict(),
            ),
        ),
        (
            "POST",
            r"/proposals/(?P<proposal_id>[^/]+)/reject",
            lambda m, b: (
                200,
                core.reject_proposal(
                    m["proposal_id"],
                    b.get("reason", ""),
                    b.get("rejected_by", ""),
                ).to_dict(),
            ),
        ),
        (
            "POST",
            r"/commitments/(?P<commitment_id>[^/]+)/dispatch",
            lambda m, b: (
                200,
                core.dispatch(dict(b, commitment_id=m["commitment_id"])),
            ),
        ),
        (
            "POST",
            r"/commitments/(?P<commitment_id>[^/]+)/reassign",
            lambda m, b: (200, core.reassign(m["commitment_id"], b)),
        ),
        (
            "GET",
            r"/commitments/(?P<commitment_id>[^/]+)",
            lambda m, b: (200, _get_commitment(core, m["commitment_id"])),
        ),
        ("POST", r"/handovers", lambda m, b: (201, core.record_handover(b))),
        (
            "POST",
            r"/carrier-callbacks",
            lambda m, b: (200, core.carrier_callback(b)),
        ),
        ("POST", r"/incidents", lambda m, b: (201, core.report_incident(b))),
        (
            "GET",
            r"/matches/(?P<match_id>[^/]+)/preparation",
            lambda m, b: (200, core.match_preparation(m["match_id"])),
        ),
        (
            "GET",
            r"/matches/(?P<match_id>[^/]+)/replacement-chain",
            lambda m, b: (200, core.replacement_chain(m["match_id"])),
        ),
        (
            "GET",
            r"/matches/(?P<match_id>[^/]+)/schedule-history",
            lambda m, b: (200, core.schedule_history(m["match_id"])),
        ),
        (
            "GET",
            r"/ledger/events",
            lambda m, b: (200, {"events": core.ledger.events}),
        ),
    ]


def make_handler(core, health_payload):
    routes = [
        (method, re.compile(f"^{pattern}$"), handler)
        for method, pattern, handler in make_routes(core, health_payload)
    ]

    class ApiHandler(BaseHTTPRequestHandler):
        """履约服务 REST 入口；领域错误映射为对应状态码。"""

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._respond(400, {"error": "请求体不是合法的 JSON"})
                    return
            matched_path = False
            for route_method, pattern, handler in routes:
                match = pattern.match(path)
                if not match:
                    continue
                matched_path = True
                if route_method != method:
                    continue
                try:
                    status, payload = handler(match.groupdict(), body)
                except DomainError as err:
                    self._respond(err.status, {"error": err.message})
                    return
                except Exception as err:  # noqa: BLE001 - 兜底，避免连接悬挂
                    self._respond(500, {"error": f"服务内部错误：{err}"})
                    return
                self._respond(status, payload)
                return
            self._respond(405 if matched_path else 404, {"error": "资源不存在"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _respond(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ApiHandler
