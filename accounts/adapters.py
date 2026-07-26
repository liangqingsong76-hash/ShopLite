"""django-allauth 与 ShopLite 手机号注册策略之间的适配器。

依赖流向：allauth 注册请求 -> 本适配器 -> 禁止默认注册；手机号注册继续由
``accounts.api_views.phone_register`` 经短信验证后处理。
"""

# Django 校验异常依赖：将账户服务的通用认证失败转换为 allauth 的失败结果。
from django.core.exceptions import ValidationError

from allauth.account.adapter import DefaultAccountAdapter

# 本领域认证服务依赖：allauth 表单和 JSON/旧表单共用已验证邮箱认证规则。
from .services import authenticate_by_login_identifier


class PhoneOnlyAccountAdapter(DefaultAccountAdapter):
    """关闭 allauth 默认注册页，确保新账户只能走手机号验证码流程。"""

    def is_open_for_signup(self, request):
        """始终拒绝默认注册入口，保留 allauth 的登录和社交账户能力。"""

        return False

    def authenticate(self, request, **credentials):
        """为 allauth 登录表单应用 ShopLite 的已验证邮箱认证规则。

        设置中不再注册 allauth 默认认证后端，避免它回退到未验证的 ``User.email``。
        此处保留 allauth 自己的登录失败限流，同时把用户名和已验证邮箱的密码认证
        委托给账户服务；因此无 JavaScript 的 ``/accounts/login/`` 也不会成为绕过。
        """

        self.pre_authenticate(request, **credentials)
        identifier = credentials.get("username") or credentials.get("email")
        password = credentials.get("password")
        try:
            user = authenticate_by_login_identifier(request, identifier, password)
        except ValidationError:
            user = None

        if user:
            self._rollback_login_failed_rl_usage()
            return user

        # ``authenticate_by_login_identifier`` 已在未知账号时执行一次密码散列，
        # 与 DefaultAccountAdapter 的失败路径保持近似的计时防护语义。
        self.authentication_failed(request, **credentials)
        return None
