"""商城列表分页与顶部购物车摘要的回归测试。

依赖流向：测试请求 -> shop 视图/选择器 -> catalog 与 commerce 测试数据库。
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from catalog.models import Category, Product
from commerce.models import CartItem
from shop.selectors import base_context


class CategoryPaginationTests(TestCase):
    """保证批量导入商品后分类页仍按固定页面大小查询和展示。"""

    @classmethod
    def setUpTestData(cls):
        """创建超过单页上限的已上架商品。"""

        cls.user = get_user_model().objects.create_user(
            username="pagination-user",
            password="StrongPass123!",
        )
        cls.category = Category.objects.create(name="分页测试")
        Product.objects.bulk_create(
            [
                Product(
                    category=cls.category,
                    name=f"分页商品 {index:02d}",
                    brand="测试品牌",
                    price=Decimal("99.00"),
                    stock=10,
                    is_active=True,
                )
                for index in range(30)
            ]
        )

    def setUp(self):
        """为每次请求建立登录会话。"""

        self.client.force_login(self.user)

    def test_category_uses_twenty_four_item_pages(self):
        """第一页只返回 24 件，并保留数据库总数。"""

        response = self.client.get(reverse("shop:category"))

        self.assertEqual(response.status_code, 200)
        products = response.context["products"]
        self.assertEqual(products.paginator.count, 30)
        self.assertEqual(len(products.object_list), 24)
        self.assertTrue(products.has_next())

    def test_second_page_returns_remaining_items_and_preserves_filters(self):
        """翻页返回剩余商品，分页链接继续携带当前筛选参数。"""

        response = self.client.get(
            reverse("shop:category"),
            {"brand": "测试品牌", "page": "2"},
        )

        self.assertEqual(response.status_code, 200)
        products = response.context["products"]
        self.assertEqual(products.number, 2)
        self.assertEqual(len(products.object_list), 6)
        self.assertContains(response, "brand=%E6%B5%8B%E8%AF%95%E5%93%81%E7%89%8C")


class CartPreviewTotalTests(TestCase):
    """保证顶部只预览三项时，合计仍覆盖整个购物车。"""

    @classmethod
    def setUpTestData(cls):
        """创建四个价格不同的购物车条目。"""

        cls.user = get_user_model().objects.create_user(
            username="cart-preview-user",
            password="StrongPass123!",
        )
        category = Category.objects.create(name="购物车摘要")
        products = [
            Product.objects.create(
                category=category,
                name=f"摘要商品 {index}",
                price=Decimal(f"{index}.00"),
                stock=10,
            )
            for index in range(1, 5)
        ]
        CartItem.objects.bulk_create(
            [
                CartItem(user=cls.user, product=product, quantity=1)
                for product in products
            ]
        )

    def test_preview_total_includes_items_not_rendered_in_preview(self):
        """三条预览记录不能把第四件商品排除在购物车合计之外。"""

        request = RequestFactory().get("/")
        request.user = self.user

        context = base_context(request)

        self.assertEqual(len(context["cart_items"]), 3)
        self.assertEqual(context["cart_total"], Decimal("10.00"))
