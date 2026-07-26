"""Commerce 应用配置。"""

from django.apps import AppConfig


class CommerceConfig(AppConfig):
    """注册客户交易领域的模型和服务。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "commerce"
    verbose_name = "交易与售后"
