"""国际乒赛资源履约：跨城市赛事的认证设备、物流航段与交接承诺协调。"""

from .core import FulfillmentCore
from .model import DomainError

__all__ = ["FulfillmentCore", "DomainError"]
