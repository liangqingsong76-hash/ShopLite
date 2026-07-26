"""微信 allauth 配置的启动失败与 settings-backed SocialApp 回归测试。

依赖流向：环境配置构造器 -> ``SOCIALACCOUNT_PROVIDERS`` -> allauth adapter。
测试不调用微信网络接口，也不把真实应用凭据写入数据库或测试日志。
"""

import os
import subprocess
import sys
from pathlib import Path

from allauth.socialaccount.adapter import get_adapter
from django.test import SimpleTestCase, TestCase, override_settings

from shoplite.settings import _build_wechat_socialaccount_providers


TEST_WECHAT_PROVIDERS = _build_wechat_socialaccount_providers(
    "allauth",
    "test-wechat-app-id",
    "test-wechat-app-secret",
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class WechatProviderSettingsTests(SimpleTestCase):
    """验证微信登录模式只能生成完整配置或安全关闭。"""

    def test_disabled_mode_does_not_register_provider_credentials(self):
        """禁用模式不能意外向 allauth 暴露微信应用配置。"""

        self.assertEqual(
            _build_wechat_socialaccount_providers(
                "disabled",
                "ignored-app-id",
                "ignored-app-secret",
            ),
            {},
        )

    def test_allauth_mode_fails_closed_when_credentials_are_missing(self):
        """真实 OAuth 模式缺少任一凭据时必须在启动配置阶段失败。"""

        for app_id, app_secret in (
            ("", ""),
            ("configured-id", ""),
            ("", "configured-secret"),
        ):
            with self.subTest(app_id=bool(app_id), app_secret=bool(app_secret)):
                with self.assertRaisesMessage(
                    RuntimeError,
                    "WECHAT_LOGIN_MODE=allauth 时必须配置",
                ):
                    _build_wechat_socialaccount_providers(
                        "allauth",
                        app_id,
                        app_secret,
                    )

    def test_allauth_mode_builds_weixin_settings_backed_app(self):
        """完整凭据必须映射到 allauth 65 使用的 provider APP 结构。"""

        self.assertEqual(
            TEST_WECHAT_PROVIDERS,
            {
                "weixin": {
                    "APP": {
                        "client_id": "test-wechat-app-id",
                        "secret": "test-wechat-app-secret",
                        "key": "",
                    }
                }
            },
        )

    def test_settings_module_applies_environment_credentials(self):
        """独立启动进程必须把服务器环境变量真正写入项目 provider 设置。"""

        environment = os.environ.copy()
        environment.update(
            {
                "WECHAT_LOGIN_MODE": "allauth",
                "WECHAT_APP_ID": "process-check-app-id",
                "WECHAT_APP_SECRET": "process-check-app-secret",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from shoplite import settings; "
                    "app = settings.SOCIALACCOUNT_PROVIDERS['weixin']['APP']; "
                    "assert app['client_id'] == 'process-check-app-id'; "
                    "assert app['secret'] == 'process-check-app-secret'"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settings_module_rejects_incomplete_allauth_environment(self):
        """缺少 secret 的真实模式不能把错误延迟到用户发起登录时。"""

        environment = os.environ.copy()
        environment.update(
            {
                "WECHAT_LOGIN_MODE": "allauth",
                "WECHAT_APP_ID": "configured-id",
                "WECHAT_APP_SECRET": "",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", "import shoplite.settings"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WECHAT_LOGIN_MODE=allauth", result.stderr)


class WechatAllauthResolutionTests(TestCase):
    """验证 allauth 能直接解析 settings 配置而不依赖数据库 SocialApp。"""

    @override_settings(SOCIALACCOUNT_PROVIDERS=TEST_WECHAT_PROVIDERS)
    def test_adapter_resolves_weixin_app_without_database_row(self):
        """配置完整时不能因缺少后台 SocialApp 记录而返回 500。"""

        app = get_adapter().get_app(request=None, provider="weixin")

        self.assertEqual(app.provider, "weixin")
        self.assertEqual(app.client_id, "test-wechat-app-id")
        self.assertEqual(app.secret, "test-wechat-app-secret")
        self.assertFalse(app.pk)
