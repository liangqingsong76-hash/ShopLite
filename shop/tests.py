from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import CartItem, Category, Order, Product
from .payments import handle_payment_notification
from .services import cancel_pending_order, create_order_from_cart, mark_order_paid


class CheckoutFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="buyer", password="secret")
        self.category = Category.objects.create(name="数码")
        self.product = Product.objects.create(
            name="测试商品",
            category=self.category,
            price=Decimal("199.00"),
            stock=5,
            sales=2,
        )

    def test_create_order_from_cart_keeps_order_pending_and_clears_cart(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)

        order = create_order_from_cart(self.user)

        self.assertEqual(order.status, Order.STATUS_PENDING)
        self.assertEqual(order.total_amount, Decimal("398.00"))
        self.assertEqual(order.discount_amount, Decimal("30.00"))
        self.assertEqual(order.pay_amount, Decimal("368.00"))
        self.assertEqual(order.items.count(), 1)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(self.product.sales, 2)

    def test_mark_order_paid_deducts_stock_once(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        order = create_order_from_cart(self.user)

        paid_order, changed = mark_order_paid(order)
        self.assertTrue(changed)
        self.assertEqual(paid_order.status, Order.STATUS_PAID)
        self.assertIsNotNone(paid_order.paid_at)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 4)

        _, changed = mark_order_paid(order)
        self.assertFalse(changed)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(self.product.sales, 4)

    def test_payment_notification_validates_amount(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        order = create_order_from_cart(self.user)

        with self.assertRaises(ValidationError):
            handle_payment_notification(
                {
                    "out_trade_no": order.order_no,
                    "trade_no": "TRADE-1",
                    "trade_status": "TRADE_SUCCESS",
                    "total_amount": "1.00",
                }
            )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING)

    def test_payment_notification_marks_order_paid(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        order = create_order_from_cart(self.user)

        paid_order, changed = handle_payment_notification(
            {
                "out_trade_no": order.order_no,
                "trade_no": "TRADE-2",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": str(order.pay_amount),
            }
        )

        self.assertTrue(changed)
        self.assertEqual(paid_order.status, Order.STATUS_PAID)

    def test_cancel_pending_order_uses_cancelled_status(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        order = create_order_from_cart(self.user)

        cancelled_order, changed = cancel_pending_order(order)

        self.assertTrue(changed)
        self.assertEqual(cancelled_order.status, Order.STATUS_CANCELLED)
