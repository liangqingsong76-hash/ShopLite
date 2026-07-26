"""Support 应用配置。"""

from django.apps import AppConfig


class SupportConfig(AppConfig):
    """注册未来客服领域；当前不加载模型 SDK 或外部密钥。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "support"
    verbose_name = "客服支持"
