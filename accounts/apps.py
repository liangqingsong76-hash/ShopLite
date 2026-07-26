"""账户领域的 Django 应用配置。"""

# 依赖流向：accounts -> Django 应用注册机制。
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """注册账户领域模型、表单和认证服务的 Django 应用。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "账户与认证"

    def ready(self):
        """注册资料头像生命周期信号。

        导入发生在 Django 应用注册完成后，避免模型尚未加载时建立信号监听器。
        """

        # 导入副作用仅为注册信号；保留局部导入避免应用初始化循环依赖。
        from . import signals  # noqa: F401
