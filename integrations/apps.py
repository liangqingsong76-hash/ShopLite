"""Integrations 应用配置。"""

from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    """注册不直接承担外部爬虫或商家平台业务的集成边界。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"
    verbose_name = "外部集成"
