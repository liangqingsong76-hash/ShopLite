"""交易库存预占、支付确认和退款安全边界的领域回归测试。

依赖流向：测试只调用 ``commerce`` / ``payments`` 的公开服务，再断言订单、商品和库存
持久化结果；不通过 storefront 页面或 Admin 绕过服务层。
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from catalog.models import Category, Product
from payments.services import mark_order_paid

from .models import Address, CartItem, Order, OrderItem, RefundRequest
from .services import cancel_pending_order, complete_refund, create_order_from_cart, create_refund_request
from .tasks import cancel_expired_pending_orders


class InventoryReservationTests(TestCase):
    """验证待付款订单预占库存、释放库存和旧订单兼容扣减规则。"""

    def setUp(self):
        """创建一个买家、地址及两件可售商品。"""

        self.user = User.objects.create_user(username="inventory-buyer", password="StrongPass!2026")
        self.category = Category.objects.create(name="库存测试分类")
        self.product = Product.objects.create(
            category=self.category,
            name="库存测试商品",
            price=Decimal("100.00"),
            stock=5,
            sales=1,
        )
        self.second_product = Product.objects.create(
            category=self.category,
            name="第二件库存测试商品",
            price=Decimal("50.00"),
            stock=5,
        )
        self.address = Address.objects.create(
            user=self.user,
            receiver="测试买家",
            phone="13800138000",
            province="广东省",
            city="深圳市",
            district="南山区",
            detail="测试路 1 号",
            is_default=True,
        )

    def _create_pending_order(self, *, quantity=2):
        """向购物车放入指定数量商品并通过结算服务创建待付款订单。"""

        CartItem.objects.create(user=self.user, product=self.product, quantity=quantity)
        return create_order_from_cart(self.user, address_id=self.address.id)

    def test_pending_order_reserves_stock_and_cancel_releases_it(self):
        """待付款订单应立即预占可售库存，取消后只归还一次。"""

        order = self._create_pending_order(quantity=2)
        self.product.refresh_from_db()
        self.assertTrue(order.stock_reserved)
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 1)

        cancelled, changed = cancel_pending_order(order)
        self.assertTrue(changed)
        self.assertEqual(cancelled.status, Order.STATUS_CANCELLED)
        self.assertFalse(cancelled.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

        _, changed = cancel_pending_order(order)
        self.assertFalse(changed)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_expired_pending_order_releases_reserved_stock(self):
        """Celery 超时取消任务必须调用同一取消服务并归还预占库存。"""

        order = self._create_pending_order(quantity=1)
        Order.objects.filter(id=order.id).update(created_at=timezone.now() - timezone.timedelta(minutes=31))

        self.assertEqual(cancel_expired_pending_orders(minutes=30), 1)
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertFalse(order.stock_reserved)
        self.assertEqual(self.product.stock, 5)

    def test_payment_finalizes_reserved_stock_without_second_deduction(self):
        """已预占订单支付成功只能增加销量，不能再次减少可售库存。"""

        order = self._create_pending_order(quantity=2)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

        paid_order, changed = mark_order_paid(order, payment_no="RESERVED-PAYMENT-1")
        self.assertTrue(changed)
        self.assertEqual(paid_order.status, Order.STATUS_PAID)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 3)

    def test_legacy_unreserved_pending_order_deducts_stock_on_payment_once(self):
        """迁移前的待付款订单保持支付时扣库存一次的兼容行为。"""

        order = Order.objects.create(
            user=self.user,
            order_no="LEGACY-UNRESERVED-1",
            total_amount=Decimal("200.00"),
            pay_amount=Decimal("200.00"),
            address_text="历史订单地址",
            status=Order.STATUS_PENDING,
            stock_reserved=False,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            price=self.product.price,
            quantity=2,
            subtotal=Decimal("200.00"),
        )

        paid_order, changed = mark_order_paid(order, payment_no="LEGACY-PAYMENT-1")
        self.assertTrue(changed)
        self.assertTrue(paid_order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 3)

        _, changed = mark_order_paid(order, payment_no="LEGACY-PAYMENT-1")
        self.assertFalse(changed)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 3)

    def test_variant_quantities_are_aggregated_before_reserving_stock(self):
        """同一商品的多个规格合计超过库存时，不得分别通过校验而超卖。"""

        CartItem.objects.create(user=self.user, product=self.product, quantity=3, color="黑色")
        CartItem.objects.create(user=self.user, product=self.product, quantity=3, color="白色")

        with self.assertRaises(ValidationError):
            create_order_from_cart(self.user, address_id=self.address.id)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(CartItem.objects.filter(user=self.user).count(), 2)

    def test_refund_stays_approved_until_real_provider_confirmation_exists(self):
        """没有真实支付渠道退款回调时，不能标记退款完成或回补库存。"""

        order = self._create_pending_order(quantity=1)
        mark_order_paid(order, payment_no="REFUND-PAYMENT-1")
        refund = create_refund_request(self.user, order, reason="商品质量问题")
        RefundRequest.objects.filter(id=refund.id).update(status=RefundRequest.STATUS_APPROVED)

        with self.assertRaises(ValidationError):
            complete_refund(refund)

        refund.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(refund.status, RefundRequest.STATUS_APPROVED)
        self.assertIsNone(refund.completed_at)
        self.assertEqual(self.product.stock, 4)
        self.assertEqual(self.product.sales, 2)
