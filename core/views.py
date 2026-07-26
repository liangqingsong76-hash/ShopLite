"""不归属于具体业务领域的 HTTP 视图。

上游：项目根路由调用健康检查。
下游：仅检查默认数据库连接并返回 JSON，不处理用户业务。
"""

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse


def health_check(request):
    """返回服务、默认数据库及生产 Redis 缓存的可用状态。

    输入：任意 HTTP 请求。
    输出：成功时返回 ``{"status": "ok"}``；数据库不可用时返回 503。
    调用链：Nginx/Docker 健康检查 → 本函数 → Django 数据库/Redis 连接。
    """

    try:
        connections["default"].ensure_connection()
        # 生产 Compose 强制使用 Redis；只读探测能让 Docker 在 Redis 失联时停止把服务视为健康。
        if settings.USE_REDIS_CACHE:
            cache.get("shoplite:health-check")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
