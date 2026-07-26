"""快速找商品应用的 Django 配置。"""

from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    """注册无数据表、无启动副作用的商品发现领域应用。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "discovery"
    verbose_name = "智能找商品"
