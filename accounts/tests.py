"""账户路由的安全回归测试。

依赖流向：Django 测试客户端 -> 项目根路由 -> ``accounts.views``；本测试确保
allauth 保留登录能力时，旧邮件密码重置 URL 仍不能绕过手机号短信验证。
"""

# Python 测试依赖：构造 API JSON、临时媒体目录和替换短信发送副作用。
import json
import tempfile
from unittest.mock import patch

# allauth 测试依赖：直接构造已验证/未验证邮箱，覆盖登录安全边界。
from allauth.account.models import EmailAddress

# Django 测试与路由反解依赖：以真实 URL 分派验证短信重置与认证安全边界。
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

# 本领域表单、模型与服务依赖：构造一次性验证码、头像资料和手机号账户。
from .forms import ProfileSettingsForm
from .models import PhoneVerificationCode, UserProfile
from .services import _phone_code_digest, register_user_by_phone


class PhonePasswordResetRoutingTests(TestCase):
    """验证密码重置只能进入手机短信验证码页面和 API。"""

    def test_reset_root_renders_phone_page_and_rejects_email_post(self):
        """旧 allauth 根路径应展示短信页面，POST 邮箱不能触发邮件重置。"""

        reset_url = reverse("account_reset_password")

        response = self.client.get(reset_url)
        email_post = self.client.post(reset_url, {"email": "user@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/api/auth/phone/password-reset/")
        self.assertEqual(email_post.status_code, 405)

    def test_legacy_email_reset_links_return_to_phone_reset_page(self):
        """历史邮件令牌链接不得处理令牌，只能返回手机号重置入口。"""

        response = self.client.get("/accounts/password/reset/key/demo-token/")

        self.assertRedirects(
            response,
            reverse("phone_password_reset"),
            fetch_redirect_response=False,
        )


class PhoneApiSecurityTests(TestCase):
    """验证手机号 API 不泄露账户状态、限制短信滥用且拒绝停用账户登录。"""

    def setUp(self):
        """清空本地测试缓存，防止限流状态跨用例污染。"""

        cache.clear()

    def tearDown(self):
        """清理本用例写入的限流键，保证其他测试拥有独立窗口。"""

        cache.clear()

    def test_phone_code_response_does_not_disclose_registration_state(self):
        """存在与不存在的手机号请求登录验证码时，外部响应必须完全一致。"""

        registered_phone = "13800139001"
        missing_phone = "13800139002"
        register_user_by_phone(registered_phone, password="StrongPass!2026")

        registered_response = self._send_code(registered_phone, "login")
        missing_response = self._send_code(missing_phone, "login")

        self.assertEqual(registered_response.status_code, 200)
        self.assertEqual(missing_response.status_code, 200)
        self.assertEqual(registered_response.json(), missing_response.json())
        self.assertTrue(PhoneVerificationCode.objects.filter(phone=registered_phone).exists())
        self.assertFalse(PhoneVerificationCode.objects.filter(phone=missing_phone).exists())

    @patch("accounts.api_views.issue_phone_verification_code")
    def test_phone_code_send_has_per_phone_rate_limit(self, issue_code):
        """同一号码即使切换客户端也不能在一小时内绕过短信发送上限。"""

        phone = "13800139003"
        register_user_by_phone(phone, password="StrongPass!2026")

        responses = [
            self.client.post(
                reverse("api:phone_code_send"),
                data=json.dumps({"phone": phone, "purpose": "login"}),
                content_type="application/json",
                REMOTE_ADDR=f"203.0.113.{index + 1}",
            )
            for index in range(6)
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)
        self.assertEqual(issue_code.call_count, 5)

    def test_inactive_phone_account_cannot_create_session(self):
        """停用账号即使持有有效短信验证码也必须返回拒绝且不写入会话。"""

        phone = "13800139004"
        user = register_user_by_phone(phone, password="StrongPass!2026")
        user.is_active = False
        user.save(update_fields=["is_active"])
        PhoneVerificationCode.objects.create(
            phone=phone,
            code=_phone_code_digest(phone, PhoneVerificationCode.PURPOSE_LOGIN, "123456"),
            purpose=PhoneVerificationCode.PURPOSE_LOGIN,
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        response = self.client.post(
            reverse("api:phone_login"),
            data=json.dumps({"phone": phone, "code": "123456"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.client.session.get("_auth_user_id"))
        self.assertTrue(UserProfile.objects.filter(user=user, phone=phone).exists())

    def _send_code(self, phone, purpose):
        """向真实 API 提交一个验证码发送请求，供响应保密性测试复用。"""

        return self.client.post(
            reverse("api:phone_code_send"),
            data=json.dumps({"phone": phone, "purpose": purpose}),
            content_type="application/json",
        )


class PasswordLoginSecurityTests(TestCase):
    """验证所有密码登录入口都拒绝未验证邮箱并共享限流规则。"""

    def setUp(self):
        """清空认证限流缓存，确保每个密码登录用例互不影响。"""

        cache.clear()

    def tearDown(self):
        """删除测试创建的限流键。"""

        cache.clear()

    def test_unverified_email_is_rejected_by_json_and_allauth_login(self):
        """未验证邮箱不能通过 JSON API 或无 JavaScript 的 allauth 表单登录。"""

        user = User.objects.create_user(
            username="unverified-user",
            email="unverified@example.test",
            password="StrongPass!2026",
        )
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            verified=False,
            primary=True,
        )

        json_response = self.client.post(
            reverse("api:password_login"),
            data=json.dumps({"login": user.email, "password": "StrongPass!2026"}),
            content_type="application/json",
        )
        form_response = self.client.post(
            reverse("account_login"),
            {"login": user.email, "password": "StrongPass!2026"},
        )

        self.assertEqual(json_response.status_code, 400)
        self.assertEqual(form_response.status_code, 200)
        self.assertIsNone(self.client.session.get("_auth_user_id"))

    def test_verified_primary_email_can_log_in(self):
        """已验证且为主邮箱的地址仍可通过 JSON 与 allauth 表单登录。"""

        user = User.objects.create_user(
            username="verified-user",
            email="verified@example.test",
            password="StrongPass!2026",
        )
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            verified=True,
            primary=True,
        )

        response = self.client.post(
            reverse("api:password_login"),
            data=json.dumps({"login": user.email, "password": "StrongPass!2026"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.id))

        self.client.logout()
        form_response = self.client.post(
            reverse("account_login"),
            {"login": user.email, "password": "StrongPass!2026"},
        )
        self.assertRedirects(form_response, reverse("shop:home"), fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.id))

    def test_legacy_form_login_supports_login_and_username_fields(self):
        """兼容旧 ``username`` 字段，同时优先支持模板当前使用的 ``login`` 字段。"""

        user = User.objects.create_user(username="legacy-user", password="StrongPass!2026")

        response = self.client.post(
            reverse("api:login"),
            {"username": user.username, "password": "StrongPass!2026"},
        )

        self.assertRedirects(response, reverse("shop:home"))
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.id))

    @patch("accounts.api_views.authenticate_by_login_identifier")
    def test_legacy_form_login_uses_password_rate_limit(self, authenticate_mock):
        """旧表单第 21 次失败请求不得再进入认证函数。"""

        authenticate_mock.return_value = None
        responses = [
            self.client.post(
                reverse("api:login"),
                {"login": "missing-user", "password": "WrongPass!2026"},
            )
            for _ in range(21)
        ]

        self.assertEqual(authenticate_mock.call_count, 20)
        self.assertContains(responses[-1], "尝试次数过多")


class AvatarPrivacyTests(TransactionTestCase):
    """验证头像替换、删除和读取均遵循私有存储生命周期。"""

    def setUp(self):
        """为每个测试创建独立媒体目录，避免写入真实项目 media。"""

        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

    def _profile_with_avatar(self, username="avatar-owner"):
        """创建带有实际存储头像的用户资料，供访问和清理测试复用。"""

        user = User.objects.create_user(username=username, password="StrongPass!2026")
        profile = UserProfile.objects.create(user=user, avatar=self._avatar_upload("old-avatar.jpg"))
        return user, profile

    @staticmethod
    def _avatar_upload(name):
        """构造最小 JPEG 文件；模型直接保存时只需验证存储生命周期。"""

        return SimpleUploadedFile(name, b"avatar-test-content", content_type="image/jpeg")

    def test_replacing_avatar_deletes_old_file_only_after_commit(self):
        """头像替换提交后删除旧文件，保留刚写入的新文件。"""

        _, profile = self._profile_with_avatar()
        storage = profile.avatar.storage
        old_name = profile.avatar.name
        self.assertTrue(storage.exists(old_name))

        with transaction.atomic():
            profile.avatar = self._avatar_upload("new-avatar.jpg")
            profile.save()
            self.assertTrue(storage.exists(old_name))

        self.assertFalse(storage.exists(old_name))
        self.assertTrue(storage.exists(profile.avatar.name))

    def test_user_deletion_cleans_profile_avatar_after_commit(self):
        """程序化删除用户时，级联资料删除也会在提交后清理头像。"""

        user, profile = self._profile_with_avatar()
        storage = profile.avatar.storage
        avatar_name = profile.avatar.name

        with transaction.atomic():
            user.delete()
            self.assertTrue(storage.exists(avatar_name))

        self.assertFalse(storage.exists(avatar_name))

    def test_non_avatar_partial_update_never_deletes_current_avatar(self):
        """只更新简介的部分保存不能因内存旧值而误删仍被资料引用的头像。"""

        _, profile = self._profile_with_avatar()
        storage = profile.avatar.storage
        avatar_name = profile.avatar.name

        with transaction.atomic():
            profile.avatar = ""
            profile.bio = "只更新简介"
            profile.save(update_fields=["bio"])

        self.assertTrue(storage.exists(avatar_name))

    def test_private_avatar_route_allows_only_owner_or_staff(self):
        """私有头像 URL 只向所有者和后台员工返回文件，其他访问统一 404。"""

        owner, profile = self._profile_with_avatar()
        other = User.objects.create_user(username="avatar-other", password="StrongPass!2026")
        staff = User.objects.create_user(
            username="avatar-staff",
            password="StrongPass!2026",
            is_staff=True,
        )
        avatar_path = profile.avatar.name.removeprefix("avatars/")
        avatar_url = reverse("private_avatar", args=[avatar_path])

        self.client.force_login(owner)
        owner_response = self.client.get(avatar_url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response["Cache-Control"], "private, no-store")
        owner_response.close()

        self.client.force_login(other)
        self.assertEqual(self.client.get(avatar_url).status_code, 404)

        self.client.logout()
        self.assertEqual(self.client.get(avatar_url).status_code, 404)

        self.client.force_login(staff)
        staff_response = self.client.get(avatar_url)
        self.assertEqual(staff_response.status_code, 200)
        staff_response.close()

    def test_private_avatar_rejects_path_traversal(self):
        """头像路由不能作为任意媒体文件读取入口。"""

        owner, _ = self._profile_with_avatar()
        self.client.force_login(owner)

        response = self.client.get("/media/avatars/../settings.py")

        self.assertEqual(response.status_code, 404)


class EmailChangeSafetyTests(TestCase):
    """验证资料表单不会在 dummy 邮件后端下伪装成功地更换邮箱。"""

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
    def test_profile_form_rejects_email_change_without_real_delivery(self):
        """生产默认 dummy backend 下，新邮箱保持未写入状态并显示表单错误。"""

        user = User.objects.create_user(
            username="email-change-user",
            email="old@example.test",
            password="StrongPass!2026",
        )
        profile = UserProfile.objects.create(user=user)
        form = ProfileSettingsForm(
            data={
                "nickname": "新昵称",
                "email": "new@example.test",
                "bio": "",
                "order_notifications": "on",
            },
            instance=profile,
            user=user,
            request=object(),
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(any("邮箱验证服务暂未配置" in error for error in form.errors["email"]))
        user.refresh_from_db()
        self.assertEqual(user.email, "old@example.test")


class UserAdminDeletionProtectionTests(TestCase):
    """验证 Django 内置用户后台仍可访问，但不提供物理删除入口。"""

    def test_user_delete_view_is_forbidden(self):
        """超级管理员也只能停用用户，不能触发级联物理删除。"""

        administrator = User.objects.create_superuser(
            username="protected-admin",
            email="protected-admin@example.test",
            password="StrongPass!2026",
        )
        target = User.objects.create_user(username="protected-target", password="StrongPass!2026")
        self.client.force_login(administrator)

        response = self.client.post(reverse("admin:auth_user_delete", args=[target.pk]), {"post": "yes"})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())
