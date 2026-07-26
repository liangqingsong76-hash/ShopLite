"""支付网关输入与模拟回调验签的回归测试。

依赖流向：Django RequestFactory -> ``payments.gateways``。这些测试覆盖网关边界，
确保未经签名或格式不明确的回调无法进入订单状态服务。
"""

# Django 测试依赖：构造最小 HTTP 请求并临时设置模拟支付配置。
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, override_settings

# 支付网关依赖：只测试输入解析和验签，不创建订单或调用支付成功服务。
from .gateways import (
    MAX_PAYMENT_PAYLOAD_BYTES,
    handle_payment_notification,
    parse_payment_payload,
    verify_mock_signature,
)


class PaymentCallbackInputTests(SimpleTestCase):
    """验证支付回调必须是小型对象并携带有效签名。"""

    def setUp(self):
        """为每个网关输入测试创建独立请求工厂。"""

        self.factory = RequestFactory()

    def test_json_array_is_rejected_before_order_lookup(self):
        """JSON 数组不是签名字段映射，必须在解析阶段返回校验错误。"""

        request = self.factory.post("/payment/notify/", data="[]", content_type="application/json")

        with self.assertRaisesMessage(ValidationError, "支付通知必须是 JSON 对象"):
            parse_payment_payload(request)

    def test_oversized_payload_is_rejected_before_json_decoding(self):
        """超出回调边界的正文不能占用 JSON 解析或数据库查询资源。"""

        oversized_body = b"x" * (MAX_PAYMENT_PAYLOAD_BYTES + 1)
        request = self.factory.post("/payment/notify/", data=oversized_body, content_type="application/json")

        with self.assertRaisesMessage(ValidationError, "支付通知正文过大"):
            parse_payment_payload(request)

    @override_settings(DEBUG=True, ENABLE_MOCK_PAYMENT=True, SHOPLITE_PAYMENT_SECRET="")
    def test_blank_mock_secret_never_accepts_a_webhook(self):
        """DEBUG 环境缺少密钥时也不得把任意回调视为有效签名。"""

        payload = {
            "out_trade_no": "SL2026071800000001",
            "trade_no": "MOCK-TRANSACTION-1",
            "total_amount": "1.00",
            "sign": "anything",
        }

        self.assertFalse(verify_mock_signature(payload))

    @override_settings(DEBUG=True, ENABLE_MOCK_PAYMENT=True, SHOPLITE_PAYMENT_SECRET="test-secret")
    def test_nested_callback_field_is_rejected_without_orm_access(self):
        """嵌套 JSON 值不能被隐式转字符串后参与验签或订单号查询。"""

        payload = {
            "out_trade_no": {"id": "SL2026071800000001"},
            "trade_no": "MOCK-TRANSACTION-2",
            "total_amount": "1.00",
            "sign": "anything",
        }

        with self.assertRaisesMessage(ValidationError, "支付通知订单号格式无效"):
            handle_payment_notification(payload)
