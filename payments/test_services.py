"""支付流水幂等、迟到回调和可用支付渠道的事务回归测试。

依赖流向：测试通过 commerce 正常结算服务构造待付款订单，再调用 payments 服务；不通过
视图或后台绕过领域状态机。并发用例使用独立数据库连接，验证同一渠道流水绝不会改绑。
"""

import threading
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings

from catalog.models import Category, Product
from commerce.models import Address, CartItem, Order
from commerce.services import cancel_pending_order, create_order_from_cart
from notifications.models import Notification

from .models import PaymentTransaction
from .services import mark_order_paid


class PaymentStateMachineTests(TestCase):
    """验证支付服务不会突破取消、库存或已付款订单的状态边界。"""

    def setUp(self):
        """创建可由正常购物车结算流程使用的测试买家、地址和商品。"""

        self.user = User.objects.create_user(username="payment-state-buyer", password="StrongPass!2026")
        self.category = Category.objects.create(name="支付状态测试分类")
        self.product = Product.objects.create(
            category=self.category,
            name="支付状态测试商品",
            price=Decimal("100.00"),
            stock=20,
        )
        self.second_product = Product.objects.create(
            category=self.category,
            name="第二件支付状态测试商品",
            price=Decimal("80.00"),
            stock=20,
        )
        self.address = Address.objects.create(
            user=self.user,
            receiver="支付测试买家",
            phone="13800138000",
            province="广东省",
            city="深圳市",
            district="南山区",
            detail="测试路 1 号",
            is_default=True,
        )

    def _create_pending_order(self, product, *, quantity=1):
        """以真实结算路径创建一张已预占库存的待付款订单。"""

        CartItem.objects.create(user=self.user, product=product, quantity=quantity)
        return create_order_from_cart(self.user, address_id=self.address.id)

    def test_cancelled_order_late_payment_is_reconciliation_audit_only(self):
        """取消后迟到成功回调只能留待人工对账，不能复活订单或影响库存销量。"""

        order = self._create_pending_order(self.product, quantity=2)
        cancel_pending_order(order)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 20)
        self.assertEqual(self.product.sales, 0)

        sensitive_payload = {
            "sign": "must-not-appear-in-log",
            "payer_openid": "private-payer-identifier",
        }
        with patch("payments.services.logger.critical") as critical_log:
            with self.captureOnCommitCallbacks(execute=True):
                reconciled_order, changed = mark_order_paid(
                    order,
                    payment_no="LATE-PAYMENT-001",
                    raw_payload=sensitive_payload,
                )

            self.assertFalse(changed)
            self.assertEqual(reconciled_order.status, Order.STATUS_CANCELLED)
            self.assertTrue(reconciled_order.payment_reconciliation_required)
            self.product.refresh_from_db()
            self.assertEqual(self.product.stock, 20)
            self.assertEqual(self.product.sales, 0)
            payment = PaymentTransaction.objects.get(transaction_no="LATE-PAYMENT-001")
            self.assertEqual(payment.order_id, order.id)
            self.assertEqual(payment.status, PaymentTransaction.STATUS_RECONCILIATION)
            notification = Notification.objects.get(
                user=self.user,
                title="订单已取消但收到付款",
            )
            self.assertIn(order.order_no, notification.content)

            with self.captureOnCommitCallbacks(execute=True):
                _, changed = mark_order_paid(
                    order,
                    payment_no="LATE-PAYMENT-001",
                    raw_payload=sensitive_payload,
                )
            self.assertFalse(changed)
            self.assertEqual(PaymentTransaction.objects.filter(transaction_no="LATE-PAYMENT-001").count(), 1)
            self.assertEqual(
                Notification.objects.filter(
                    user=self.user,
                    title="订单已取消但收到付款",
                ).count(),
                1,
            )
            critical_log.assert_called_once()
            log_call = critical_log.call_args
            self.assertEqual(log_call.args, ("late_successful_payment_requires_reconciliation",))
            self.assertEqual(
                log_call.kwargs["extra"],
                {
                    "event": "late_successful_payment_requires_reconciliation",
                    "order_id": order.id,
                    "payment_transaction_id": payment.id,
                    "payment_provider": Order.PAYMENT_MOCK,
                },
            )
            self.assertNotIn("must-not-appear-in-log", repr(log_call))
            self.assertNotIn("private-payer-identifier", repr(log_call))

    def test_paid_order_rejects_a_different_transaction_number(self):
        """重复回调只能携带原交易号，不能静默接受另一笔渠道扣款。"""

        order = self._create_pending_order(self.product)
        mark_order_paid(order, payment_no="PRIMARY-PAYMENT-001")

        with self.assertRaisesMessage(ValidationError, "支付流水号与已记录流水号不一致"):
            mark_order_paid(order, payment_no="CONFLICTING-PAYMENT-002")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(order.payment_no, "PRIMARY-PAYMENT-001")
        self.assertEqual(PaymentTransaction.objects.filter(order=order).count(), 1)

    def test_existing_transaction_number_cannot_be_rebound_to_another_order(self):
        """顺序竞争也必须保留赢家流水并回滚另一订单的支付状态。"""

        first_order = self._create_pending_order(self.product)
        second_order = self._create_pending_order(self.second_product)
        mark_order_paid(first_order, payment_no="SHARED-PAYMENT-001")

        with self.assertRaisesMessage(ValidationError, "支付流水号已被其他订单使用"):
            mark_order_paid(second_order, payment_no="SHARED-PAYMENT-001")

        first_order.refresh_from_db()
        second_order.refresh_from_db()
        payment = PaymentTransaction.objects.get(transaction_no="SHARED-PAYMENT-001")
        self.assertEqual(first_order.status, Order.STATUS_PAID)
        self.assertEqual(second_order.status, Order.STATUS_PENDING)
        self.assertEqual(payment.order_id, first_order.id)

    @override_settings(DEBUG=False, ENABLE_MOCK_PAYMENT=False)
    def test_checkout_rejects_when_no_payment_channel_is_available(self):
        """没有真实渠道且模拟支付关闭时不得创建会长期占用库存的待付款订单。"""

        CartItem.objects.create(user=self.user, product=self.product, quantity=2)

        with self.assertRaisesMessage(ValidationError, "当前没有可用支付渠道"):
            create_order_from_cart(self.user, address_id=self.address.id)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 20)
        self.assertTrue(CartItem.objects.filter(user=self.user, product=self.product).exists())
        self.assertFalse(Order.objects.filter(user=self.user).exists())


class PaymentTransactionConcurrencyTests(TransactionTestCase):
    """验证两个独立数据库事务不能把同一渠道流水绑定到两张订单。"""

    reset_sequences = True

    def setUp(self):
        """为两条并发回调分别构造买家、商品和真实预占订单。"""

        category = Category.objects.create(name="支付并发测试分类")
        self.first_user = User.objects.create_user(username="payment-race-one", password="StrongPass!2026")
        self.second_user = User.objects.create_user(username="payment-race-two", password="StrongPass!2026")
        self.first_product = Product.objects.create(
            category=category,
            name="支付并发商品一",
            price=Decimal("50.00"),
            stock=10,
        )
        self.second_product = Product.objects.create(
            category=category,
            name="支付并发商品二",
            price=Decimal("60.00"),
            stock=10,
        )
        self.first_order = self._create_pending_order(self.first_user, self.first_product, "并发买家一")
        self.second_order = self._create_pending_order(self.second_user, self.second_product, "并发买家二")

    @staticmethod
    def _create_pending_order(user, product, receiver):
        """创建一个独立用户的待付款订单，避免并发测试共享购物车或商品行锁。"""

        address = Address.objects.create(
            user=user,
            receiver=receiver,
            phone="13800138000",
            province="广东省",
            city="深圳市",
            district="南山区",
            detail="并发测试路 1 号",
            is_default=True,
        )
        CartItem.objects.create(user=user, product=product, quantity=1)
        return create_order_from_cart(user, address_id=address.id)

    def test_simultaneous_shared_transaction_number_has_one_winner_only(self):
        """两个订单并发使用同一 trade_no 时，一个成功、另一个业务失败且流水不改绑。"""

        barrier = threading.Barrier(2)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker(order_id):
            """在独立线程/数据库连接内模拟一条支付成功回调。"""

            close_old_connections()
            try:
                order = Order.objects.get(id=order_id)
                barrier.wait(timeout=10)
                _, changed = mark_order_paid(order, payment_no="RACE-SHARED-TRANSACTION")
                outcome = ("success", order_id, changed)
            except Exception as exc:  # 测试需要收集两个线程的领域结果。
                outcome = ("error", order_id, exc)
            finally:
                close_old_connections()
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(self.first_order.id,)),
            threading.Thread(target=worker, args=(self.second_order.id,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "支付并发事务未在预期时间内结束")

        self.assertEqual(len(outcomes), 2)
        successes = [outcome for outcome in outcomes if outcome[0] == "success" and outcome[2]]
        errors = [outcome for outcome in outcomes if outcome[0] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][2], ValidationError)

        payment = PaymentTransaction.objects.get(transaction_no="RACE-SHARED-TRANSACTION")
        self.first_order.refresh_from_db()
        self.second_order.refresh_from_db()
        order_statuses = {self.first_order.id: self.first_order.status, self.second_order.id: self.second_order.status}
        self.assertEqual(payment.order_id, successes[0][1])
        self.assertEqual(order_statuses[payment.order_id], Order.STATUS_PAID)
        losing_order_id = self.second_order.id if payment.order_id == self.first_order.id else self.first_order.id
        self.assertEqual(order_statuses[losing_order_id], Order.STATUS_PENDING)
