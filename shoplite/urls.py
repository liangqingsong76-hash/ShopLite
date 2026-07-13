from django.conf import settings    # settings：读取 DEBUG、MEDIA_URL
from django.conf.urls.static import static    # static：开发环境暴露媒体文件
from django.contrib import admin    # admin：后台路由
from django.urls import include, path   # include/path：Django URL 工具
import importlib.util    # importlib：判断 debug_toolbar 是否存在

urlpatterns = [
    path("admin/", admin.site.urls),   # /admin/` 到 Django 后台
    path("accounts/", include("allauth.urls")),     # /accounts/` 到 allauth 登录注册退出等账号路由
    path("api/", include("shop.api_urls")),    # /api/` 到项目 API
    path("", include("shop.urls")),   # /` 到商城页面路由
]

# 第 14-15 行在开发环境下让 Django 直接提供 `/media/` 图片，打开商品详情页的路由
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# 如果安装 debug toolbar，就增加调试路由
if settings.DEBUG and importlib.util.find_spec("debug_toolbar"):
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
