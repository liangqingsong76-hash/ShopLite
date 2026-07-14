from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Address,
    BrowsingHistory,
    CartItem,
    Category,
    Coupon,
    Notification,
    Order,
    PaymentTransaction,
    PhoneVerificationCode,
    Product,
    RefundRequest,
    UserCoupon,
)
from .payments import handle_payment_notification
from .services import (
    _phone_code_digest,
    add_product_to_cart,
    cancel_pending_order,
    claim_coupon,
    complete_refund,
    create_order_from_cart,
    create_refund_request,
    mark_order_paid,
    register_user_with_phone_code,
    register_user_by_phone,
    reset_password_with_phone_code,
    verify_phone_code,
)


@override_settings(DEBUG=True, ENABLE_MOCK_PAYMENT=True, SHOPLITE_PAYMENT_SECRET="")
class CommerceSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="commerce-user", password="StrongPass!2026")
        self.other = User.objects.create_user(username="other-user", password="StrongPass!2026")
        self.category = Category.objects.create(name="测试分类")
        self.product = Product.objects.create(
            name="安全测试商品",
            category=self.category,
            price=Decimal("350.00"),
            stock=6,
        )
        self.address = Address.objects.create(
            user=self.user,
            receiver="张三",
            phone="13800138000",
            province="广东省",
            city="深圳市",
            district="南山区",
            detail="测试路 1 号",
            is_default=True,
        )
        self.other_address = Address.objects.create(
            user=self.other,
            receiver="李四",
            phone="13800138001",
            province="广东省",
            city="深圳市",
            district="福田区",
            detail="测试路 2 号",
            is_default=True,
        )

    def _pending_order(self, *, coupon=None):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        return create_order_from_cart(
            self.user,
            address_id=self.address.id,
            user_coupon_id=coupon.id if coupon else None,
        )

    def test_order_rejects_another_users_address(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        with self.assertRaises(ValidationError):
            create_order_from_cart(self.user, address_id=self.other_address.id)

    def test_cart_rejects_quantity_above_stock(self):
        with self.assertRaises(ValidationError):
            add_product_to_cart(self.user, self.product.id, quantity=7)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())

    def test_coupon_claim_is_idempotent_and_cancel_returns_coupon(self):
        coupon = Coupon.objects.create(
            code="TEST30",
            name="测试优惠券",
            value=Decimal("30.00"),
            minimum_spend=Decimal("100.00"),
            total_quantity=1,
            valid_until=timezone.now() + timezone.timedelta(days=7),
        )
        user_coupon, created = claim_coupon(self.user, coupon.id)
        self.assertTrue(created)
        _, created = claim_coupon(self.user, coupon.id)
        self.assertFalse(created)
        coupon.refresh_from_db()
        self.assertEqual(coupon.claimed_count, 1)

        order = self._pending_order(coupon=user_coupon)
        user_coupon.refresh_from_db()
        self.assertIsNotNone(user_coupon.used_at)
        cancel_pending_order(order)
        user_coupon.refresh_from_db()
        self.assertIsNone(user_coupon.used_at)

    def test_payment_requires_amount_and_rejects_reused_trade_number(self):
        first = self._pending_order()
        with self.assertRaises(ValidationError):
            handle_payment_notification({"out_trade_no": first.order_no, "trade_no": "TX-1"})

        handle_payment_notification({
            "out_trade_no": first.order_no,
            "trade_no": "TX-1",
            "total_amount": str(first.pay_amount),
        })
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        second = create_order_from_cart(self.user, address_id=self.address.id)
        with self.assertRaises(ValidationError):
            handle_payment_notification({
                "out_trade_no": second.order_no,
                "trade_no": "TX-1",
                "total_amount": str(second.pay_amount),
            })

    def test_refund_completion_is_idempotent_and_restores_stock(self):
        order = self._pending_order()
        mark_order_paid(order, payment_no="REFUND-TEST-PAY")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        refund = create_refund_request(self.user, order, reason="商品质量问题")
        refund, changed = complete_refund(refund)
        self.assertTrue(changed)
        _, changed = complete_refund(refund)
        self.assertFalse(changed)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)

    def test_state_changing_endpoints_require_post_and_ownership(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("shop:address_delete", args=[self.address.id])).status_code, 405)
        self.assertEqual(self.client.get(reverse("shop:mock_payment", args=[self._pending_order().id])).status_code, 405)
        response = self.client.post(reverse("shop:address_delete", args=[self.other_address.id]))
        self.assertEqual(response.status_code, 404)

    def test_alipay_placeholder_never_changes_order(self):
        order = self._pending_order()
        response = self.client.post(reverse("shop:alipay_notify"), {"out_trade_no": order.order_no})
        self.assertEqual(response.status_code, 503)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING)


class PhoneAccountSecurityTests(TestCase):
    def setUp(self):
        cache.clear()

    def _verification(self, phone, purpose, raw="123456"):
        return PhoneVerificationCode.objects.create(
            phone=phone,
            code=_phone_code_digest(phone, purpose, raw),
            purpose=purpose,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

    def test_five_wrong_codes_lock_verification(self):
        verification = self._verification("13800138100", PhoneVerificationCode.PURPOSE_LOGIN)
        for _ in range(5):
            with self.assertRaises(ValidationError):
                verify_phone_code("13800138100", "999999")
        verification.refresh_from_db()
        self.assertEqual(verification.attempt_count, 5)
        self.assertIsNotNone(verification.used_at)

    def test_weak_password_does_not_consume_registration_code(self):
        verification = self._verification("13800138101", PhoneVerificationCode.PURPOSE_REGISTER)
        with self.assertRaises(ValidationError):
            register_user_with_phone_code("13800138101", "123456", "123")
        verification.refresh_from_db()
        self.assertIsNone(verification.used_at)

    def test_phone_password_reset_changes_password_once(self):
        user = register_user_by_phone("13800138102", password="OldStrongPass!2026")
        verification = self._verification("13800138102", PhoneVerificationCode.PURPOSE_RESET)
        reset_password_with_phone_code("13800138102", "123456", "NewStrongPass!2026")
        user.refresh_from_db()
        self.assertTrue(user.check_password("NewStrongPass!2026"))
        with self.assertRaises(ValidationError):
            reset_password_with_phone_code("13800138102", "123456", "AnotherPass!2026")
        verification.refresh_from_db()
        self.assertIsNotNone(verification.used_at)

    @override_settings(DEBUG=False, WECHAT_LOGIN_MODE="mock")
    def test_mock_wechat_is_disabled_without_debug(self):
        self.assertEqual(self.client.get(reverse("api:wechat_login")).status_code, 503)
        self.assertEqual(self.client.get(reverse("api:alipay_login")).status_code, 503)


class PageAndProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="page-user", password="StrongPass!2026", email="old@example.test")
        self.category = Category.objects.create(name="页面分类")
        self.product = Product.objects.create(name="页面测试商品", category=self.category, price=Decimal("99.00"), stock=5)
        self.address = Address.objects.create(
            user=self.user,
            receiver="页面用户",
            phone="13800138200",
            province="浙江省",
            city="杭州市",
            detail="西湖测试路 8 号",
            is_default=True,
        )
        self.client.force_login(self.user)

    def test_personal_pages_render(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        urls = (
            reverse("shop:home"),
            reverse("shop:profile"),
            reverse("shop:settings"),
            reverse("shop:coupons"),
            reverse("shop:notifications"),
            reverse("shop:history"),
            reverse("shop:refunds"),
            reverse("shop:bills"),
            reverse("shop:address_list"),
            reverse("shop:checkout_page"),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_profile_settings_persist(self):
        response = self.client.post(reverse("shop:settings"), {
            "action": "profile",
            "nickname": "新昵称",
            "email": "new@example.test",
            "bio": "新的个人简介",
            "order_notifications": "on",
        })
        self.assertRedirects(response, reverse("shop:settings"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "新昵称")
        self.assertEqual(self.user.email, "new@example.test")
        self.assertEqual(self.user.profile.bio, "新的个人简介")

    def test_product_view_creates_single_history_record(self):
        url = reverse("shop:product_detail", args=[self.product.id])
        self.client.get(url)
        self.client.get(url)
        self.assertEqual(BrowsingHistory.objects.filter(user=self.user, product=self.product).count(), 1)

    def test_health_check_reaches_database(self):
        self.client.logout()
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
