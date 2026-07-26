"""旧支付导入路径的兼容导出层。

依赖流向：旧店铺视图/测试 -> 本模块 -> payments.gateways。真实渠道能力必须
继续在 payments 领域实现，不能回写到 storefront。
"""

from payments.gateways import (
    build_mock_payment_url,
    handle_payment_notification,
    parse_payment_payload,
    verify_mock_signature,
)


__all__ = (
    "build_mock_payment_url",
    "handle_payment_notification",
    "parse_payment_payload",
    "verify_mock_signature",
)
