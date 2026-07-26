"""快速找商品 API 的独立 URL 表。

集成流向：项目根路由 ``/api/discovery/`` -> 本模块 -> ``api_views``。
"""

from django.urls import path

from . import api_views


app_name = "discovery"

urlpatterns = [
    path("text/", api_views.text_search, name="text-search"),
    path("voice/", api_views.voice_search, name="voice-search"),
    path("image/", api_views.image_search, name="image-search"),
]
