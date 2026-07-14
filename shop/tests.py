from decimal import Decimal
import json

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Address, CartItem, Category, Coupon, Order, PhoneVerificationCode, Product, UserProfile
from .payments import handle_payment_notification
from .services import (
    bind_phone_to_user,
    cancel_pending_order,
    create_order_from_cart,
    issue_phone_verification_code,
    mark_order_paid,
    register_user_by_phone,
    verify_phone_code,
)


@override_settings(DEBUG=True, ENABLE_MOCK_PAYMENT=True, SHOPLITE_PAYMENT_SECRET="")
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
        self.address = Address.objects.create(
            user=self.user,
            receiver="测试用户",
            phone="13800138000",
            province="广东省",
            city="广州市",
            district="天河区",
            detail="测试路 1 号",
            is_default=True,
        )

    def test_create_order_from_cart_keeps_order_pending_and_clears_cart(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)

        order = create_order_from_cart(self.user, address_id=self.address.id)

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
        order = create_order_from_cart(self.user, address_id=self.address.id)

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
        order = create_order_from_cart(self.user, address_id=self.address.id)

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
        order = create_order_from_cart(self.user, address_id=self.address.id)

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
        order = create_order_from_cart(self.user, address_id=self.address.id)

        cancelled_order, changed = cancel_pending_order(order)

        self.assertTrue(changed)
        self.assertEqual(cancelled_order.status, Order.STATUS_CANCELLED)


class PhoneVerificationTests(TestCase):
    def test_issue_and_verify_phone_code_once(self):
        verification = issue_phone_verification_code("13800138000")

        verified = verify_phone_code("13800138000", verification._raw_code)

        self.assertEqual(verified.id, verification.id)
        verified.refresh_from_db()
        self.assertIsNotNone(verified.used_at)
        with self.assertRaises(ValidationError):
            verify_phone_code("13800138000", verification.code)

    def test_phone_register_api_creates_session_user(self):
        verification = PhoneVerificationCode.objects.create(
            phone="13800138001",
            code=self._code_digest("13800138001", PhoneVerificationCode.PURPOSE_REGISTER, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_REGISTER,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            "/api/auth/phone/register/",
            data=json.dumps({
                "phone": verification.phone,
                "code": "123456",
                "password": "SecretPass123",
                "password_confirm": "SecretPass123",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserProfile.objects.filter(phone="13800138001").exists())
        user_id = self.client.session.get("_auth_user_id")
        self.assertIsNotNone(user_id)
        self.assertEqual(response.json()["action"], "registered")

    def test_phone_register_api_rejects_existing_phone(self):
        register_user_by_phone("13800138005", password="SecretPass123")
        PhoneVerificationCode.objects.create(
            phone="13800138005",
            code=self._code_digest("13800138005", PhoneVerificationCode.PURPOSE_REGISTER, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_REGISTER,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            "/api/auth/phone/register/",
            data=json.dumps({
                "phone": "13800138005",
                "code": "123456",
                "password": "SecretPass123",
                "password_confirm": "SecretPass123",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(UserProfile.objects.filter(phone="13800138005").count(), 1)

    def test_phone_login_api_uses_existing_phone_account(self):
        user = register_user_by_phone("13800138004", password="SecretPass123")
        PhoneVerificationCode.objects.create(
            phone="13800138004",
            code=self._code_digest("13800138004", PhoneVerificationCode.PURPOSE_LOGIN, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_LOGIN,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            "/api/auth/phone/login/",
            data=json.dumps({"phone": "13800138004", "code": "123456"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "logged_in")
        self.assertEqual(UserProfile.objects.filter(phone="13800138004").count(), 1)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.id))

    def test_phone_login_api_rejects_unregistered_phone(self):
        PhoneVerificationCode.objects.create(
            phone="13800138006",
            code=self._code_digest("13800138006", PhoneVerificationCode.PURPOSE_LOGIN, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_LOGIN,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            "/api/auth/phone/login/",
            data=json.dumps({"phone": "13800138006", "code": "123456"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(UserProfile.objects.filter(phone="13800138006").exists())

    def test_password_login_accepts_registered_phone(self):
        user = register_user_by_phone("13800138007", password="SecretPass123")

        response = self.client.post(
            "/api/auth/password/login/",
            data=json.dumps({"login": "13800138007", "password": "SecretPass123"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.id))

    def test_phone_login_api_rejects_wrong_code(self):
        register_user_by_phone("13800138002", password="SecretPass123")
        PhoneVerificationCode.objects.create(
            phone="13800138002",
            code=self._code_digest("13800138002", PhoneVerificationCode.PURPOSE_LOGIN, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_LOGIN,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            "/api/auth/phone/login/",
            data=json.dumps({"phone": "13800138002", "code": "999999"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(UserProfile.objects.filter(phone="13800138002").exists())

    def test_send_login_code_rejects_unregistered_phone(self):
        response = self.client.post(
            "/api/auth/phone/code/",
            data=json.dumps({"phone": "13800138008", "purpose": "login"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_bind_phone_rejects_phone_used_by_another_account(self):
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        bind_phone_to_user(owner, "13800138003")

        with self.assertRaises(ValidationError):
            bind_phone_to_user(other, "13800138003")

    @staticmethod
    def _code_digest(phone, purpose, code):
        from .services import _phone_code_digest

        return _phone_code_digest(phone, purpose, code)


@override_settings(DEBUG=True, WECHAT_LOGIN_MODE="mock")
class WechatLoginTests(TestCase):
    def test_mock_wechat_login_creates_social_account(self):
        response = self.client.get("/api/auth/wechat/login/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SocialAccount.objects.filter(provider="weixin", uid="mock-wechat-openid").count(), 1)
        self.assertIsNotNone(self.client.session.get("_auth_user_id"))

    def test_mock_wechat_login_reuses_existing_social_account(self):
        self.client.get("/api/auth/wechat/login/")
        first_user_id = self.client.session.get("_auth_user_id")
        self.client.logout()

        self.client.get("/api/auth/wechat/login/")

        self.assertEqual(SocialAccount.objects.filter(provider="weixin", uid="mock-wechat-openid").count(), 1)
        self.assertEqual(self.client.session.get("_auth_user_id"), first_user_id)
