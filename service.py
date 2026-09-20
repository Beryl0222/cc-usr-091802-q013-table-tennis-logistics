"""国际乒赛资源履约的服务入口。"""

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from fulfillment import FulfillmentCore
from fulfillment.api import make_handler

SERVICE_ID = "table-tennis-logistics"
SERVICE_NAME = "国际乒赛资源履约"

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample.json"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_core(fixture_path=FIXTURE_PATH):
    """从公开样例装载器材档案、认证规则与技术人员名册。"""
    path = Path(fixture_path)
    if path.exists():
        return FulfillmentCore.load_fixture(path)
    return FulfillmentCore()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    core = build_core()
    handler = make_handler(core, health_payload)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
