"""商品目录领域的 Django 应用配置。"""

# 依赖流向：catalog -> Django 应用注册机制。
from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """注册商品目录、来源追溯和商品查询能力的 Django 应用。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "catalog"
    verbose_name = "商品目录"
