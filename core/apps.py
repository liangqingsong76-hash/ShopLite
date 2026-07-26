"""Core 应用配置：注册不包含业务模型的基础能力。"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """声明 ShopLite 的共享基础应用。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "ShopLite 基础能力"
