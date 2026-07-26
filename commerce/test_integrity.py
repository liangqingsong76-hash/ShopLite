"""数据完整性与订单取消锁顺序的回归测试。

依赖流向：测试构造 catalog/commerce/payments 领域记录，调用 commerce 取消服务，
并直接校验数据库删除保护、后台权限以及资源回补结果。
"""

from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.models import Category, Product
from payments.models import PaymentTransaction
from shop.admin import CategoryAdmin, CouponAdmin

from .models import Coupon, Order, OrderItem, UserCoupon
from .services import cancel_pending_order


User = get_user_model()


class ProtectedBusinessHistoryTests(TestCase):
    """验证商品、领券和支付历史不会被上游记录删除所级联清空。"""

    def setUp(self):
        """创建外键删除保护所需的最小业务记录。"""

        self.user = User.objects.create_user(username="integrity-user", password="StrongPass!2026")
        self.category = Category.objects.create(name="完整性分类")
        self.product = Product.objects.create(
            category=self.category,
            name="完整性商品",
            price=Decimal("19.90"),
            stock=5,
        )
        self.coupon = Coupon.objects.create(
            code="INTEGRITY-COUPON",
            name="完整性优惠券",
            value=Decimal("5.00"),
            valid_until=timezone.now() + timezone.timedelta(days=7),
        )
        self.user_coupon = UserCoupon.objects.create(user=self.user, coupon=self.coupon)
        self.order = Order.objects.create(
            user=self.user,
            order_no="INTEGRITY-ORDER",
            total_amount=Decimal("19.90"),
            pay_amount=Decimal("19.90"),
        )
        self.payment = PaymentTransaction.objects.create(
            order=self.order,
            provider=Order.PAYMENT_MOCK,
            transaction_no="INTEGRITY-PAYMENT",
            amount=Decimal("19.90"),
        )

    def test_category_with_product_is_protected(self):
        """商品仍归属分类时，删除分类必须抛出 ``ProtectedError``。"""

        with self.assertRaises(ProtectedError):
            self.category.delete()
        self.assertTrue(Category.objects.filter(id=self.category.id).exists())
        self.assertTrue(Product.objects.filter(id=self.product.id).exists())

    def test_coupon_with_claim_record_is_protected(self):
        """已有用户领取记录时，删除优惠券规则必须保留历史审计。"""

        with self.assertRaises(ProtectedError):
            self.coupon.delete()
        self.assertTrue(Coupon.objects.filter(id=self.coupon.id).exists())
        self.assertTrue(UserCoupon.objects.filter(id=self.user_coupon.id).exists())

    def test_order_with_payment_transaction_is_protected(self):
        """已有支付流水时，删除订单必须保留订单和资金审计。"""

        with self.assertRaises(ProtectedError):
            self.order.delete()
        self.assertTrue(Order.objects.filter(id=self.order.id).exists())
        self.assertTrue(PaymentTransaction.objects.filter(id=self.payment.id).exists())


class AdminDeletePermissionTests(TestCase):
    """验证运营后台引导使用停用开关，而不是物理删除主数据。"""

    def test_category_admin_disables_delete(self):
        """分类后台对列表页和对象详情页都不授予删除权限。"""

        model_admin = CategoryAdmin(Category, admin.site)
        self.assertFalse(model_admin.has_delete_permission(request=None))
        self.assertFalse(model_admin.has_delete_permission(request=None, obj=Category(name="待停用分类")))

    def test_coupon_admin_disables_delete(self):
        """优惠券后台对列表页和对象详情页都不授予删除权限。"""

        model_admin = CouponAdmin(Coupon, admin.site)
        self.assertFalse(model_admin.has_delete_permission(request=None))
        self.assertFalse(model_admin.has_delete_permission(request=None, obj=Coupon(name="待停用优惠券")))


class CancelPendingOrderIntegrityTests(TestCase):
    """验证取消订单只回补一次库存、释放用户券且不通过 JOIN 提前锁券。"""

    def setUp(self):
        """构造一个已经预占库存并核销优惠券的待付款订单。"""

        self.user = User.objects.create_user(username="cancel-integrity-user", password="StrongPass!2026")
        self.category = Category.objects.create(name="取消完整性分类")
        # 原库存 10，订单预占 3 后数据库中的当前可售库存为 7。
        self.product = Product.objects.create(
            category=self.category,
            name="取消完整性商品",
            price=Decimal("30.00"),
            stock=7,
        )
        self.coupon_rule = Coupon.objects.create(
            code="CANCEL-INTEGRITY",
            name="取消完整性优惠券",
            value=Decimal("10.00"),
            valid_until=timezone.now() + timezone.timedelta(days=7),
        )
        self.user_coupon = UserCoupon.objects.create(
            user=self.user,
            coupon=self.coupon_rule,
            used_at=timezone.now(),
        )
        self.order = Order.objects.create(
            user=self.user,
            order_no="CANCEL-INTEGRITY-ORDER",
            total_amount=Decimal("90.00"),
            discount_amount=Decimal("10.00"),
            pay_amount=Decimal("80.00"),
            coupon=self.user_coupon,
            stock_reserved=True,
            status=Order.STATUS_PENDING,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            price=self.product.price,
            quantity=3,
            subtotal=Decimal("90.00"),
        )

    def test_cancel_restores_inventory_and_coupon_exactly_once(self):
        """首次取消回补库存并释放券，重复取消不产生第二次回补。"""

        cancelled_order, changed = cancel_pending_order(self.order)
        self.assertTrue(changed)
        self.assertEqual(cancelled_order.status, Order.STATUS_CANCELLED)
        self.assertFalse(cancelled_order.stock_reserved)

        self.product.refresh_from_db()
        self.user_coupon.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertIsNone(self.user_coupon.used_at)

        repeated_order, changed_again = cancel_pending_order(cancelled_order)
        self.assertFalse(changed_again)
        self.assertEqual(repeated_order.status, Order.STATUS_CANCELLED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_order_lock_query_does_not_join_coupon_table(self):
        """订单行锁必须由单表查询取得，避免在商品之前隐式锁用户券。"""

        with CaptureQueriesContext(connection) as captured:
            cancel_pending_order(self.order)

        order_selects = [
            query["sql"]
            for query in captured.captured_queries
            if "select" in query["sql"].lower() and "commerce_order" in query["sql"].lower()
        ]
        self.assertTrue(order_selects, "未捕获到订单锁查询")
        self.assertNotIn("JOIN", order_selects[0].upper())
