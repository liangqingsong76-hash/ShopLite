"""Notifications 应用配置。"""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """注册用户通知模型与服务。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "站内通知"
