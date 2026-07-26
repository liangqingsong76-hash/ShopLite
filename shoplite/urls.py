"""ShopLite 根路由。

流向：浏览器/Nginx -> 本模块 -> 账户、智能检索、商城与后台应用。
"""

import importlib.util

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path

from accounts.views import (
    phone_password_reset_legacy_redirect,
    phone_password_reset_page,
    phone_signup_redirect,
    private_avatar,
)
from core.views import health_check
from discovery.page_views import search_page


urlpatterns = [
    path("health/", health_check, name="health"),
    path("media/avatars/<path:avatar_path>", private_avatar, name="private_avatar"),
    path("admin/", admin.site.urls),
    # 必须排在 allauth 之前，强制注册与密码重置进入手机号短信流程。
    path("accounts/signup/", phone_signup_redirect, name="phone_signup"),
    path("accounts/password/reset/", phone_password_reset_page, name="phone_password_reset"),
    re_path(
        r"^accounts/password/reset/(?P<legacy_reset_path>.+)/$",
        phone_password_reset_legacy_redirect,
        name="phone_password_reset_legacy",
    ),
    path("accounts/", include("allauth.urls")),
    path("discover/", search_page, name="discovery_search"),
    path("api/discovery/", include("discovery.urls")),
    path("api/", include("shop.api_urls")),
    path("", include("shop.urls")),
]

if settings.DEBUG:
    # 头像路由已在上方拦截；这里只为本地开发暴露商品等非头像媒体。
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG and importlib.util.find_spec("debug_toolbar"):
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
