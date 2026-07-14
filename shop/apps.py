# 导入 Django App 配置基类
from django.apps import AppConfig


# 定义商城 app 的配置类，注册应用，在setting里面可以添加名字为shop的应用
class ShopConfig(AppConfig):
    # 设置默认主键类型和 app 名，超大容量自增整数主键
    default_auto_field = "django.db.models.BigAutoField"
    name = "shop"

    def ready(self):
        # SimpleUI 会为其 iframe 布局移除点击劫持中间件；商城后台不依赖 iframe，恢复安全默认值。
        from django.conf import settings

        middleware = "django.middleware.clickjacking.XFrameOptionsMiddleware"
        if middleware not in settings.MIDDLEWARE:
            settings.MIDDLEWARE.append(middleware)
