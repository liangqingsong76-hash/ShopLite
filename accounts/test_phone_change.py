"""手机号首次绑定与双验证码换绑的安全回归测试。

依赖流向：测试客户端 -> 兼容 API 路由 -> ``accounts.api_views`` ->
``accounts.services``。测试覆盖验证码用途隔离、旧号归属、新号占用、原子消费和
认证成功后的同站重定向，防止页面改版重新引入单验证码覆盖手机号的漏洞。
"""

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PhoneVerificationCode, UserProfile
from .services import _phone_code_digest, change_phone_with_codes, register_user_by_phone


class PhoneChangeSecurityTests(TestCase):
    """验证已有手机号只能通过当前号码、新号码两条专用验证码完成换绑。"""

    def setUp(self):
        """清空短信限流缓存，并创建一个已绑定手机号的登录账号。"""

        cache.clear()
        self.current_phone = "13800139101"
        self.new_phone = "13800139102"
        self.user = register_user_by_phone(self.current_phone, password="StrongPass!2026")

    def tearDown(self):
        """清理本用例产生的短信限流键。"""

        cache.clear()

    def _code(self, phone, purpose, raw_code):
        """创建一条指定用途的有效验证码摘要。"""

        return PhoneVerificationCode.objects.create(
            phone=phone,
            code=_phone_code_digest(phone, purpose, raw_code),
            purpose=purpose,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

    def _post_json(self, name, payload):
        """向命名 API 路由提交 JSON。"""

        return self.client.post(
            reverse(name),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_phone_management_code_purposes_require_login(self):
        """绑定、旧号验证和新号验证都不能由匿名请求触发短信发送。"""

        for purpose in ("bind", "change_old", "change_new"):
            response = self._post_json(
                "api:phone_code_send",
                {"phone": self.new_phone, "purpose": purpose},
            )
            self.assertEqual(response.status_code, 401)

    @patch("accounts.api_views.issue_phone_verification_code")
    def test_old_phone_code_is_forced_to_authenticated_users_current_phone(self, issue_code):
        """客户端伪造的旧号码会被忽略，验证码只能发往资料中的当前号码。"""

        attacker_selected_phone = "13800139109"
        self.client.force_login(self.user)

        response = self._post_json(
            "api:phone_code_send",
            {"phone": attacker_selected_phone, "purpose": "change_old"},
        )

        self.assertEqual(response.status_code, 200)
        issue_code.assert_called_once_with(
            self.current_phone,
            purpose=PhoneVerificationCode.PURPOSE_CHANGE_OLD,
        )

    @patch("accounts.api_views.issue_phone_verification_code")
    def test_change_new_code_is_not_issued_for_an_occupied_phone(self, issue_code):
        """目标号码已属于其他账号时，不发送验证码且外部仍返回防枚举通用响应。"""

        occupied_phone = "13800139103"
        register_user_by_phone(occupied_phone, password="StrongPass!2026")
        self.client.force_login(self.user)

        response = self._post_json(
            "api:phone_code_send",
            {"phone": occupied_phone, "purpose": "change_new"},
        )

        self.assertEqual(response.status_code, 200)
        issue_code.assert_not_called()

    def test_existing_phone_rejects_legacy_single_bind_code(self):
        """已有手机号不能继续使用旧的 ``bind`` 验证码直接覆盖号码。"""

        bind_code = self._code(self.new_phone, PhoneVerificationCode.PURPOSE_BIND, "123456")
        self.client.force_login(self.user)

        response = self._post_json(
            "api:phone_bind",
            {"phone": self.new_phone, "code": "123456"},
        )

        self.assertEqual(response.status_code, 400)
        self.user.profile.refresh_from_db()
        bind_code.refresh_from_db()
        self.assertEqual(self.user.profile.phone, self.current_phone)
        self.assertIsNone(bind_code.used_at)

    def test_successful_change_consumes_both_codes_and_updates_phone_atomically(self):
        """两条专用验证码均正确时才替换号码，并同时一次性消费验证码。"""

        current_code = self._code(
            self.current_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_OLD,
            "123456",
        )
        new_code = self._code(
            self.new_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_NEW,
            "654321",
        )

        profile = change_phone_with_codes(self.user, "123456", self.new_phone, "654321")

        profile.refresh_from_db()
        current_code.refresh_from_db()
        new_code.refresh_from_db()
        self.assertEqual(profile.phone, self.new_phone)
        self.assertIsNotNone(profile.phone_verified_at)
        self.assertIsNotNone(current_code.used_at)
        self.assertIsNotNone(new_code.used_at)

    def test_wrong_new_code_preserves_old_code_and_records_failed_attempt(self):
        """新号校验失败不能消费正确旧号验证码，失败次数仍必须持久化。"""

        current_code = self._code(
            self.current_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_OLD,
            "123456",
        )
        new_code = self._code(
            self.new_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_NEW,
            "654321",
        )

        with self.assertRaises(ValidationError):
            change_phone_with_codes(self.user, "123456", self.new_phone, "000000")

        self.user.profile.refresh_from_db()
        current_code.refresh_from_db()
        new_code.refresh_from_db()
        self.assertEqual(self.user.profile.phone, self.current_phone)
        self.assertIsNone(current_code.used_at)
        self.assertIsNone(new_code.used_at)
        self.assertEqual(new_code.attempt_count, 1)

    def test_bind_or_login_codes_cannot_be_reused_for_phone_change(self):
        """相同明文但用途不同的验证码摘要不能跨流程复用。"""

        bind_code = self._code(self.new_phone, PhoneVerificationCode.PURPOSE_BIND, "654321")
        self._code(
            self.current_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_OLD,
            "123456",
        )

        with self.assertRaises(ValidationError):
            change_phone_with_codes(self.user, "123456", self.new_phone, "654321")

        self.user.profile.refresh_from_db()
        bind_code.refresh_from_db()
        self.assertEqual(self.user.profile.phone, self.current_phone)
        self.assertIsNone(bind_code.used_at)

    def test_first_phone_binding_still_uses_single_bind_code(self):
        """未绑定账号继续使用原有 ``bind`` 用途完成首次绑定。"""

        unbound_user = User.objects.create_user(username="unbound", password="StrongPass!2026")
        UserProfile.objects.get_or_create(user=unbound_user)
        code = self._code(self.new_phone, PhoneVerificationCode.PURPOSE_BIND, "123456")
        self.client.force_login(unbound_user)

        response = self._post_json(
            "api:phone_bind",
            {"phone": self.new_phone, "code": "123456"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "bound")
        unbound_user.profile.refresh_from_db()
        code.refresh_from_db()
        self.assertEqual(unbound_user.profile.phone, self.new_phone)
        self.assertIsNotNone(code.used_at)

    def test_change_api_requires_both_current_and_new_codes(self):
        """换绑 API 缺少任意一条验证码时都不能改变手机号。"""

        self._code(
            self.current_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_OLD,
            "123456",
        )
        self._code(
            self.new_phone,
            PhoneVerificationCode.PURPOSE_CHANGE_NEW,
            "654321",
        )
        self.client.force_login(self.user)

        response = self._post_json(
            "api:phone_bind",
            {"current_code": "123456", "new_phone": self.new_phone, "new_code": ""},
        )

        self.assertEqual(response.status_code, 400)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.phone, self.current_phone)


class PhoneAuthenticationRedirectTests(TestCase):
    """验证手机号认证返回的跳转地址只能停留在当前站点。"""

    def setUp(self):
        """清理限流缓存。"""

        cache.clear()

    def tearDown(self):
        """清理限流缓存。"""

        cache.clear()

    def _login(self, phone, next_url):
        """创建手机号账号和登录验证码，并提交带 ``next`` 的登录请求。"""

        register_user_by_phone(phone, password="StrongPass!2026")
        raw_code = "123456"
        PhoneVerificationCode.objects.create(
            phone=phone,
            code=_phone_code_digest(phone, PhoneVerificationCode.PURPOSE_LOGIN, raw_code),
            purpose=PhoneVerificationCode.PURPOSE_LOGIN,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )
        return self.client.post(
            reverse("api:phone_login"),
            data=json.dumps({"phone": phone, "code": raw_code, "next": next_url}),
            content_type="application/json",
        )

    def test_external_next_is_replaced_with_home(self):
        """外站 ``next`` 不能成为认证成功后的开放重定向目标。"""

        response = self._login("13800139111", "https://evil.example/phishing")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect_url"], "/")

    def test_same_site_relative_next_is_preserved(self):
        """站内相对路径仍可用于把用户送回认证前页面。"""

        response = self._login("13800139112", "/orders/?status=pending")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect_url"], "/orders/?status=pending")


class SettingsPhoneFlowPageTests(TestCase):
    """验证设置页按账户状态展示单码首次绑定或双码换绑流程。"""

    def test_bound_user_sees_two_step_change_flow_and_live_status(self):
        """已有手机号的用户应看到当前号码和新号码两步验证控件。"""

        user = register_user_by_phone("13800139121", password="StrongPass!2026")
        self.client.force_login(user)

        response = self.client.get(reverse("shop:settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="currentPhoneCode"')
        self.assertContains(response, 'id="newPhoneCode"')
        self.assertContains(response, 'purpose: "change_old"')
        self.assertContains(response, 'purpose: "change_new"')
        self.assertContains(response, 'aria-live="polite"')

    def test_unbound_user_sees_first_bind_flow(self):
        """无手机号账号仍应看到单验证码首次绑定控件。"""

        user = User.objects.create_user(username="unbound-page", password="StrongPass!2026")
        UserProfile.objects.get_or_create(user=user)
        self.client.force_login(user)

        response = self.client.get(reverse("shop:settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="bindPhone"')
        self.assertContains(response, 'id="bindCode"')
        self.assertContains(response, 'purpose: "bind"')
        self.assertNotContains(response, 'id="currentPhoneCode"')
