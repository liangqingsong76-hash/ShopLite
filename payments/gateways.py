"""支付渠道输入适配和模拟支付回调验证。

上游：storefront 的 mock 支付页面与未来 webhook URL。
下游：验证后的数据仅调用 payments.services.mark_order_paid。
TODO：接入支付宝/微信官方 SDK 与证书校验前，禁止把真实渠道标为可用。
"""

from collections.abc import Mapping
import hmac
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError

from commerce.models import Order

from .services import mark_order_paid


# 回调仅需要少量签名字段；限制正文与字段长度可避免异常输入进入 ORM 或审计数据。
MAX_PAYMENT_PAYLOAD_BYTES = 16 * 1024
MAX_ORDER_NO_LENGTH = 32
MAX_TRADE_NO_LENGTH = 64
MAX_AMOUNT_LENGTH = 32
MAX_TRADE_STATUS_LENGTH = 32
MAX_SIGNATURE_LENGTH = 128


def build_mock_payment_url(request, order):
    """由 storefront 负责实际 URL 反向解析；本函数保留兼容入口。"""

    from django.urls import reverse

    return request.build_absolute_uri(reverse("shop:mock_payment", args=[order.id]))


def parse_payment_payload(request):
    """读取受大小限制的对象 JSON 或无重复字段的表单支付通知载荷。

    回调签名只适用于键值对象；数组、标量、重复表单字段和无效 UTF-8 均作为
    客户端错误拒绝，不能交由下游网关或 ORM 猜测解释。
    """

    body = request.body
    if len(body) > MAX_PAYMENT_PAYLOAD_BYTES:
        raise ValidationError("支付通知正文过大")

    if str(request.content_type or "").lower() == "application/json":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("支付通知 JSON 格式无效") from exc
        if not isinstance(payload, dict):
            raise ValidationError("支付通知必须是 JSON 对象")
        return payload

    form_payload = request.POST
    if any(len(values) != 1 for _, values in form_payload.lists()):
        raise ValidationError("支付通知不能包含重复字段")
    return {key: values[0] for key, values in form_payload.lists()}


def verify_mock_signature(payload):
    """验证仅限开发环境且必须配置密钥的模拟支付签名。"""

    secret = getattr(settings, "SHOPLITE_PAYMENT_SECRET", "")
    if not isinstance(payload, Mapping) or not getattr(settings, "ENABLE_MOCK_PAYMENT", False):
        return False
    if not secret:
        # 即使 DEBUG=True 也不能让未签名 webhook 改变订单状态。
        return False
    signature = _callback_string(payload, "sign", max_length=MAX_SIGNATURE_LENGTH)
    order_no = _callback_string(payload, "out_trade_no", max_length=MAX_ORDER_NO_LENGTH)
    trade_no = _callback_string(payload, "trade_no", max_length=MAX_TRADE_NO_LENGTH)
    total_amount = _callback_string(payload, "total_amount", max_length=MAX_AMOUNT_LENGTH)
    if not all((signature, order_no, trade_no, total_amount)):
        return False
    message = f"{order_no}|{trade_no}|{total_amount}".encode("utf-8")
    expected = hmac.new(str(secret).encode("utf-8"), message, "sha256").hexdigest()
    return hmac.compare_digest(signature, expected)


def handle_payment_notification(payload):
    """校验严格回调字段、模拟签名与金额后调用支付成功编排服务。"""

    payload = _validated_payment_payload(payload)
    if not verify_mock_signature(payload):
        raise ValidationError("支付通知签名校验失败")
    order_no = payload["out_trade_no"]
    trade_status = payload.get("trade_status", "TRADE_SUCCESS")
    if trade_status not in {"TRADE_SUCCESS", "TRADE_FINISHED", "SUCCESS"}:
        return None, False
    order = Order.objects.get(order_no=order_no)
    _validate_amount(order, payload["total_amount"])
    return mark_order_paid(
        order,
        payment_no=payload["trade_no"],
        provider=Order.PAYMENT_MOCK,
        raw_payload={
            "out_trade_no": order_no,
            "trade_no": payload["trade_no"],
            "trade_status": trade_status,
            "total_amount": payload["total_amount"],
        },
    )


def _validate_amount(order, amount):
    """防止支付通知金额与订单应付金额不一致。"""

    if amount in (None, ""):
        raise ValidationError("支付通知缺少金额")
    try:
        paid_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValidationError("支付金额格式错误")
    if not paid_amount.is_finite():
        raise ValidationError("支付金额格式错误")
    if paid_amount != order.pay_amount:
        raise ValidationError("支付金额与订单金额不一致")


def _validated_payment_payload(payload):
    """验证回调对象的字段类型、长度和订单号一致性，并返回安全的字段副本。"""

    if not isinstance(payload, Mapping):
        raise ValidationError("支付通知必须是对象")
    normalized = {
        "out_trade_no": _required_callback_string(payload, "out_trade_no", MAX_ORDER_NO_LENGTH, "订单号"),
        "trade_no": _required_callback_string(payload, "trade_no", MAX_TRADE_NO_LENGTH, "支付流水号"),
        "total_amount": _required_callback_string(payload, "total_amount", MAX_AMOUNT_LENGTH, "支付金额"),
        "sign": _required_callback_string(payload, "sign", MAX_SIGNATURE_LENGTH, "签名"),
    }
    trade_status = payload.get("trade_status", "TRADE_SUCCESS")
    if not isinstance(trade_status, str) or len(trade_status) > MAX_TRADE_STATUS_LENGTH:
        raise ValidationError("支付状态格式无效")
    normalized["trade_status"] = trade_status

    # 保留旧字段 ``order_no`` 时必须与签名使用的 ``out_trade_no`` 一致，避免歧义。
    legacy_order_no = payload.get("order_no")
    if legacy_order_no not in (None, "") and legacy_order_no != normalized["out_trade_no"]:
        raise ValidationError("支付通知订单号不一致")
    return normalized


def _required_callback_string(payload, key, max_length, label):
    """读取一个必填的短字符串回调字段，拒绝嵌套 JSON、数组及超长输入。"""

    value = _callback_string(payload, key, max_length=max_length)
    if not value:
        raise ValidationError(f"支付通知{label}格式无效")
    return value


def _callback_string(payload, key, *, max_length):
    """从映射中安全取得短字符串字段；字段类型或长度无效时返回空字符串。"""

    value = payload.get(key, "")
    if not isinstance(value, str) or not value or len(value) > max_length:
        return ""
    return value
