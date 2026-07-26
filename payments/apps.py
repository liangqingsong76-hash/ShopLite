"""Payments 应用配置。"""

from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """注册支付流水模型和支付服务。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"
    verbose_name = "支付"
