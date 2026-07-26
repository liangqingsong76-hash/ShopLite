"""账户资料数据库事务与头像文件生命周期的回归测试。"""

import base64
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings

from .forms import ProfileSettingsForm
from .models import UserProfile


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProfileAvatarRollbackTests(TestCase):
    """保证邮件等后续步骤失败时不遗留新头像文件。"""

    def setUp(self):
        """为每个测试隔离媒体目录和账户资料。"""

        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)
        self.user = get_user_model().objects.create_user(
            username="profile-rollback",
            email="old@example.test",
            password="StrongPass123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.user,
            avatar=self._upload("old.png"),
        )

    @staticmethod
    def _upload(name):
        """构造可通过 Pillow 校验的一像素 PNG 上传。"""

        return SimpleUploadedFile(name, ONE_PIXEL_PNG, content_type="image/png")

    def test_email_delivery_failure_removes_new_avatar_and_keeps_old_reference(self):
        """邮箱验证发送失败回滚数据库时，新写头像也必须立即清理。"""

        old_avatar_name = self.profile.avatar.name
        request = RequestFactory().post("/settings/")
        request.user = self.user
        form = ProfileSettingsForm(
            data={
                "nickname": "回滚测试",
                "email": "new@example.test",
                "bio": "",
                "marketing_notifications": "on",
                "order_notifications": "on",
            },
            files={"avatar": self._upload("new.png")},
            instance=self.profile,
            user=self.user,
            request=request,
        )
        with patch("accounts.forms.email_verification_delivery_configured", return_value=True):
            self.assertTrue(form.is_valid(), form.errors)

        with patch("accounts.forms.request_email_change", side_effect=RuntimeError("mail unavailable")):
            with self.assertRaisesRegex(RuntimeError, "mail unavailable"):
                form.save()

        self.profile.refresh_from_db()
        self.user.refresh_from_db()
        stored_files = [
            path
            for path in Path(self.media_directory.name).rglob("*")
            if path.is_file()
        ]
        self.assertEqual(self.profile.avatar.name, old_avatar_name)
        self.assertEqual(self.user.first_name, "")
        self.assertEqual(len(stored_files), 1)
