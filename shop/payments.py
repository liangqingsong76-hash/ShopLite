import hmac
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.urls import reverse

from .models import Order
from .services import mark_order_paid

logger = logging.getLogger(__name__)


def build_mock_payment_url(request, order):
    return request.build_absolute_uri(reverse("shop:mock_payment", args=[order.id]))


def parse_payment_payload(request):
    if request.content_type == "application/json":
        try:
            import json

            return json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    payload = QueryDict("", mutable=True)
    payload.update(request.POST)
    return payload


def verify_mock_signature(payload):
    secret = getattr(settings, "SHOPLITE_PAYMENT_SECRET", "")
    signature = payload.get("sign", "")
    if not secret:
        return True

    order_no = payload.get("out_trade_no", "")
    trade_no = payload.get("trade_no", "")
    total_amount = payload.get("total_amount", "")
    message = f"{order_no}|{trade_no}|{total_amount}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), message, "sha256").hexdigest()
    return hmac.compare_digest(signature, expected)


def handle_payment_notification(payload):
    if not verify_mock_signature(payload):
        raise ValidationError("支付通知签名校验失败")

    order_no = payload.get("out_trade_no") or payload.get("order_no")
    trade_status = payload.get("trade_status", "TRADE_SUCCESS")
    if trade_status not in {"TRADE_SUCCESS", "TRADE_FINISHED", "SUCCESS"}:
        return None, False
    if not order_no:
        raise ValidationError("支付通知缺少订单号")

    order = Order.objects.get(order_no=order_no)
    _validate_amount(order, payload.get("total_amount"))
    return mark_order_paid(order, payment_no=payload.get("trade_no", ""))


def _validate_amount(order, amount):
    if amount in (None, ""):
        return
    try:
        paid_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValidationError("支付金额格式错误")
    if paid_amount != order.pay_amount:
        raise ValidationError("支付金额与订单金额不一致")
