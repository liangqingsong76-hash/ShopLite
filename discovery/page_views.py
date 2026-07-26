"""智能找商品的页面入口。

依赖流向：根路由 -> 本模块 -> ``discovery/search.html``；浏览器随后只调用
``discovery.api_views``。本模块不解析意图、不查询商品，也不保存语音或图片。
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .providers import image_provider_availability


@login_required
def search_page(request):
    """展示适老化的文字、语音和图片找商品页面。"""

    image_availability = image_provider_availability()
    return render(
        request,
        "discovery/search.html",
        {
            # 页面与 API 共用同一 provider 校验；非空但无效的路径也不能误启用。
            "image_search_enabled": image_availability.available,
            "image_provider_status": image_availability,
        },
    )
