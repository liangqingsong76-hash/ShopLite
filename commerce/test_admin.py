"""Django Admin 对订单、售后记录的受控操作回归测试。

依赖流向：测试 -> ``shop.admin`` 批量动作 -> commerce 订单/退款模型。
这些测试只验证后台不会绕过交易状态机或留下半完成的退款审核状态。
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from accounts.models import UserProfile
from catalog.models import Product, ProductSource
from shop.admin import (
    OrderAdmin,
    OrderItemAdmin,
    ProductAdmin,
    ProductSourceAdmin,
    RefundRequestAdmin,
    UserProfileAdmin,
)

from .models import Order, OrderItem, RefundRequest


class RefundRequestAdminSafetyTests(TestCase):
    """验证后台退款审核使用受控、原子的订单与售后状态流转。"""

    def setUp(self):
        """创建管理员动作所需的客户、请求工厂和静默后台实例。"""

        self.user = User.objects.create_user(username="admin-refund-buyer", password="StrongPass!2026")
        self.request = RequestFactory().post("/admin/commerce/refundrequest/")
        self.refund_admin = RefundRequestAdmin(RefundRequest, admin.site)
        # 动作消息属于 Django Admin UI；领域状态断言不依赖 messages middleware。
        self.refund_admin.message_user = lambda *args, **kwargs: None

    def _create_pending_refund(self, suffix):
        """创建一笔已进入售后状态、仍待后台审核的退款申请。"""

        order = Order.objects.create(
            user=self.user,
            order_no=f"ADM-REF-{suffix[:20]}",
            total_amount=Decimal("100.00"),
            pay_amount=Decimal("100.00"),
            address_text="后台审核测试地址",
            status=Order.STATUS_REFUND,
        )
        refund = RefundRequest.objects.create(
            order=order,
            user=self.user,
            refund_no=f"ADMIN-REFUND-{suffix}",
            reason="测试退款",
            amount=Decimal("100.00"),
            order_status_before=Order.STATUS_PAID,
        )
        return order, refund

    def test_critical_records_cannot_be_physically_deleted_in_admin(self):
        """订单、订单项、退款和商品来源记录都必须保留审计链路。"""

        model_admins = (
            OrderAdmin(Order, admin.site),
            OrderItemAdmin(OrderItem, admin.site),
            ProductAdmin(Product, admin.site),
            RefundRequestAdmin(RefundRequest, admin.site),
            ProductSourceAdmin(ProductSource, admin.site),
            UserProfileAdmin(UserProfile, admin.site),
        )

        for model_admin in model_admins:
            with self.subTest(model_admin=model_admin.__class__.__name__):
                self.assertFalse(model_admin.has_delete_permission(self.request))

    def test_approve_only_transitions_a_locked_pending_refund(self):
        """同意操作只能把订单仍处于售后的待审核申请推进为已同意。"""

        order, refund = self._create_pending_refund("APPROVE")

        self.refund_admin.approve_refunds(self.request, RefundRequest.objects.filter(pk=refund.pk))

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_REFUND)
        self.assertEqual(refund.status, RefundRequest.STATUS_APPROVED)

    def test_reject_restores_order_and_refund_together(self):
        """拒绝操作必须在同一事务内恢复订单原状态并标记退款已拒绝。"""

        order, refund = self._create_pending_refund("REJECT")

        self.refund_admin.reject_refunds(self.request, RefundRequest.objects.filter(pk=refund.pk))

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(refund.status, RefundRequest.STATUS_REJECTED)

    def test_reject_does_not_overwrite_an_already_approved_refund(self):
        """重复或并发的拒绝动作不能覆盖已同意的历史审核结论。"""

        order, refund = self._create_pending_refund("ALREADY-APPROVED")
        refund.status = RefundRequest.STATUS_APPROVED
        refund.save(update_fields=["status", "updated_at"])

        self.refund_admin.reject_refunds(self.request, RefundRequest.objects.filter(pk=refund.pk))

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_REFUND)
        self.assertEqual(refund.status, RefundRequest.STATUS_APPROVED)

    def test_reject_keeps_an_invalid_historical_transition_pending(self):
        """损坏的原订单状态不能让拒绝动作把售后单推进到不可恢复的状态。"""

        order, refund = self._create_pending_refund("INVALID-STATUS")
        refund.order_status_before = Order.STATUS_CANCELLED
        refund.save(update_fields=["order_status_before", "updated_at"])

        self.refund_admin.reject_refunds(self.request, RefundRequest.objects.filter(pk=refund.pk))

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_REFUND)
        self.assertEqual(refund.status, RefundRequest.STATUS_PENDING)

    def test_reject_rolls_back_when_refund_status_save_fails(self):
        """退款状态写入失败时，不得留下订单已恢复、退款仍待审核的半完成状态。"""

        order, refund = self._create_pending_refund("ROLLBACK")

        with patch.object(RefundRequest, "save", side_effect=RuntimeError("simulated refund save failure")):
            with self.assertRaises(RuntimeError):
                self.refund_admin.reject_refunds(
                    self.request,
                    RefundRequest.objects.filter(pk=refund.pk),
                )

        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_REFUND)
        self.assertEqual(refund.status, RefundRequest.STATUS_PENDING)
