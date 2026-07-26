"""店面 JSON 写入 API 的输入边界回归测试。

依赖流向：测试客户端 -> ``shop.api_views`` -> 领域服务。测试确保无效 ID、
非对象 JSON 和超大载荷在视图层返回 400，而不是触发 ORM 转换异常。
"""

# Python 标准库依赖：构造请求 JSON。
import json

# Django 测试与认证依赖：通过真实路由调用受登录保护的写入 API。
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

# 兼容模型门面依赖：为 API 输入测试建立最小商品数据。
from .models import Category, Product


class StorefrontApiInputSafetyTests(TestCase):
    """验证写入型 API 在进入领域服务前严格校验请求对象和外部 ID。"""

    def setUp(self):
        """创建已登录用户与一个可售商品，避免授权或空数据干扰输入断言。"""

        self.user = User.objects.create_user(username="api-input-user", password="StrongPass!2026")
        category = Category.objects.create(name="API 输入测试分类")
        self.product = Product.objects.create(name="API 输入测试商品", category=category, price="19.90", stock=5)
        self.client.force_login(self.user)

    def test_invalid_ids_return_400_instead_of_orm_value_errors(self):
        """非法、嵌套和超范围 ID 均应返回客户端错误，不应产生 500。"""

        cases = (
            ("api:cart_add", {"product_id": "not-a-number"}),
            ("api:cart_update", {"item_id": "99999999999999999999", "quantity": 1}),
            ("api:cart_delete", {"item_id": 0}),
            ("api:favorite_toggle", {"product_id": [self.product.id]}),
            ("api:order_create", {"address_id": {"id": 1}}),
        )

        for route_name, payload in cases:
            with self.subTest(route=route_name):
                response = self.client.post(
                    reverse(route_name),
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)

    def test_non_object_or_oversized_json_returns_400_without_writing_cart(self):
        """数组与超大正文不能被当成购物车输入，且不能产生部分写入。"""

        array_response = self.client.post(
            reverse("api:cart_add"),
            data="[]",
            content_type="application/json",
        )
        oversized_response = self.client.post(
            reverse("api:cart_add"),
            data=b"x" * (16 * 1024 + 1),
            content_type="application/json",
        )

        self.assertEqual(array_response.status_code, 400)
        self.assertEqual(oversized_response.status_code, 400)
        self.assertFalse(self.user.cartitem_set.exists())

    def test_non_string_or_too_long_variant_returns_400(self):
        """规格字段不能以嵌套 JSON 或超出模型字段长度的文本写入数据库。"""

        nested_response = self.client.post(
            reverse("api:cart_add"),
            data=json.dumps({"product_id": self.product.id, "color": ["黑色"]}),
            content_type="application/json",
        )
        long_response = self.client.post(
            reverse("api:cart_add"),
            data=json.dumps({"product_id": self.product.id, "color": "黑" * 101}),
            content_type="application/json",
        )

        self.assertEqual(nested_response.status_code, 400)
        self.assertEqual(long_response.status_code, 400)
        self.assertFalse(self.user.cartitem_set.exists())
