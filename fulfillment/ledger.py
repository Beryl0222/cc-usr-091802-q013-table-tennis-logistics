"""追加式台账：所有事实以事件留痕，键控幂等、回调去重，保证唯一事实。"""

from datetime import datetime

from .model import UTC, new_id, to_iso


class Ledger:
    """只追加、不改写的事件台账。

    - idempotency_key：扫描枪离线补录等场景的去重键，重放返回原事件；
    - dedup_key：承运人重复回调等业务指纹，重复回调返回首次落账的事实。
    """

    def __init__(self, clock=None):
        self.events = []
        self._by_idem = {}
        self._by_dedup = {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(
        self,
        event_type,
        payload,
        occurred_at=None,
        idempotency_key=None,
        dedup_key=None,
    ):
        """追加事件；命中幂等键或去重指纹时返回原事件并标记 replay。"""
        if idempotency_key is not None and idempotency_key in self._by_idem:
            return self._by_idem[idempotency_key], True
        if dedup_key is not None and dedup_key in self._by_dedup:
            return self._by_dedup[dedup_key], True
        event = {
            "event_id": new_id("evt"),
            "seq": len(self.events) + 1,
            "type": event_type,
            "occurred_at": to_iso(occurred_at) if occurred_at else None,
            "recorded_at": to_iso(self._clock()),
            "payload": payload,
            "idempotency_key": idempotency_key,
            "dedup_key": dedup_key,
        }
        self.events.append(event)
        if idempotency_key is not None:
            self._by_idem[idempotency_key] = event
        if dedup_key is not None:
            self._by_dedup[dedup_key] = event
        return event, False

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]
