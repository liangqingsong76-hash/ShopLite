# 导入 Django App 配置基类
from django.apps import AppConfig


# 定义商城 app 的配置类，注册应用，在setting里面可以添加名字为shop的应用
class ShopConfig(AppConfig):
    # 设置默认主键类型和 app 名，超大容量自增整数主键
    default_auto_field = "django.db.models.BigAutoField"
    name = "shop"

    def ready(self):
        # 保证点击劫持中间件始终启用；X_FRAME_OPTIONS=SAMEORIGIN 允许 SimpleUI 的同源 iframe。
        from django.conf import settings

        middleware = "django.middleware.clickjacking.XFrameOptionsMiddleware"
        if middleware not in settings.MIDDLEWARE:
            settings.MIDDLEWARE.append(middleware)
