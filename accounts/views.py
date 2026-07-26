"""账户页面级视图。

依赖流向：浏览器访问旧的 allauth 注册入口 -> 本模块 -> 登录页的既有手机注册页签。
注册提交仍由 ``accounts.api_views.phone_register`` 与 ``accounts.services`` 处理，
本模块不创建用户，也不绕过短信验证。
"""

# Python 标准库依赖：验证媒体子路径并推断受保护头像的响应类型。
import mimetypes
from pathlib import PurePosixPath

# Django HTTP、重定向、模板渲染与路由反解依赖：将遗留页面入口导向既有手机号认证页面。
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

# 本领域模型依赖：头像读取只能依据当前资料记录授权，不能按任意媒体路径读取。
from .models import UserProfile


@require_safe
def phone_signup_redirect(request):
    """将旧 allauth 注册 URL 导向登录页中的手机号注册页签。

    用户名、邮箱和密码登录继续由 allauth 保留；根据当前账号策略，创建新账号必须
    进入现有的手机号短信验证流程。TODO（OAuth）：微信/支付宝完成资质和回调配置后，
    再在该页增加对应入口，不得创建模拟账号。
    """

    return redirect(f"{reverse('account_login')}?tab=register")


@require_safe
def phone_password_reset_page(request):
    """渲染仅通过手机号短信验证码完成的密码重置页面。

    该入口必须排在 allauth 默认邮件重置 URL 之前；浏览器提交的验证码随后流向
    ``accounts.api_views.phone_password_reset``，避免产生邮件重置令牌或日志泄露。
    """

    return render(request, "account/password_reset.html")


@require_safe
def phone_password_reset_legacy_redirect(request, legacy_reset_path):
    """拦截 allauth 的旧邮件重置链接并返回统一的手机号重置入口。

    ``legacy_reset_path`` 只用于匹配历史 URL，不参与重定向目标构造，从而避免开放
    重定向；POST 请求由 ``require_safe`` 拒绝，不能借此触发任何邮件发送。
    """

    return redirect("phone_password_reset")


@require_safe
def private_avatar(request, avatar_path):
    """按资料归属读取受保护头像，仅允许本人或后台员工访问。

    路由层应将 ``/media/avatars/<path>`` 指向此视图，并排在 Django 开发媒体
    路由之前；产品图仍由普通媒体路径提供。未知、未登录或未获授权请求统一返回
    404，避免通过响应差异枚举用户头像。
    """

    avatar_name = _avatar_storage_name(avatar_path)
    if not avatar_name or not request.user.is_authenticated:
        raise Http404("头像不存在")

    profile = UserProfile.objects.only("user_id", "avatar").filter(avatar=avatar_name).first()
    if not profile or (profile.user_id != request.user.id and not request.user.is_staff):
        raise Http404("头像不存在")

    try:
        avatar_file = profile.avatar.storage.open(profile.avatar.name, "rb")
    except (FileNotFoundError, OSError):
        raise Http404("头像不存在")

    content_type, _ = mimetypes.guess_type(profile.avatar.name)
    response = FileResponse(avatar_file, content_type=content_type or "application/octet-stream")
    # 头像属于个人资料，不能被共享代理或浏览器长期缓存到其他会话。
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _avatar_storage_name(avatar_path):
    """将 URL 中的头像子路径转为经过遍历校验的存储名。"""

    raw_path = str(avatar_path or "")
    if not raw_path or "\\" in raw_path:
        return ""
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return f"avatars/{path.as_posix()}"
