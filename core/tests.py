"""基础运行能力的回归测试。

依赖流向：测试客户端 -> ``core.views.health_check`` -> 数据库与缓存探针；本文件不写入
领域业务数据，用于确保部署编排只在所依赖的基础服务可用时报告健康。
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse


class HealthCheckTests(TestCase):
    """验证健康检查同时覆盖数据库和生产 Redis 缓存依赖。"""

    def test_health_check_returns_ok_when_dependencies_are_available(self):
        """默认测试数据库与本地缓存可用时应返回成功。"""

        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @override_settings(USE_REDIS_CACHE=True)
    @patch("core.views.cache.get", side_effect=ConnectionError("redis unavailable"))
    def test_health_check_returns_503_when_required_redis_is_unavailable(self, _cache_get):
        """Compose 强制使用 Redis 时，缓存不可用不能继续报告服务健康。"""

        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
