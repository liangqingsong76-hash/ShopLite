"""账户 JSON API 视图，等待项目总路由接入。

依赖流向：HTTP 请求 -> accounts.api_views -> accounts.services/models。该模块
不直接依赖商品、订单或支付应用；路由层后续只需将现有 ``/api/auth/...``
路径指向这些同名视图即可。
"""

# Python 标准库依赖：解析 JSON、散列手机号限流键，以及规范化客户端 IP。
import hashlib
import ipaddress
import json

# Django HTTP、认证、缓存和配置依赖。
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

# 本领域服务与模型依赖：API 只编排账户认证流程。
from .models import PhoneVerificationCode, UserProfile
from .services import (
    authenticate_by_login_identifier,
    bind_phone_with_code,
    change_phone_with_codes,
    get_user_by_phone,
    issue_phone_verification_code,
    normalize_phone,
    register_user_with_phone_code,
    reset_password_with_phone_code,
    validate_phone_code_request,
    verify_phone_code,
)


# 认证接口只需要接收很小的 JSON 对象；尽早拒绝超大正文，避免无意义地占用解析内存。
MAX_AUTH_JSON_BODY_BYTES = 16 * 1024
# 登录、注册、重置等用途共用此响应，避免通过 API 响应判断手机号是否已注册或已绑定。
PHONE_CODE_ACCEPTED_RESPONSE = {
    "success": True,
    "expires_in": 300,
    "message": "如果该手机号可用于此操作，验证码将很快发送。",
}


def login_view(request):
    """渲染并处理带 IP 限流的传统用户名、邮箱或手机号密码登录表单。

    这是 ``/api/login/`` 的兼容入口；它必须复用 JSON 密码登录相同的限流范围，
    不能成为绕过暴力破解防护的旧表单路径。
    """

    if request.method == "POST":
        if not _allow_request(request, "password-login", limit=20, window=600):
            messages.error(request, "尝试次数过多，请稍后再试")
        else:
            try:
                user = authenticate_by_login_identifier(
                    request,
                    request.POST.get("login") or request.POST.get("username"),
                    request.POST.get("password"),
                )
            except ValidationError:
                user = None
            if user:
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect("shop:home")
            messages.error(request, "用户名或密码错误")
    return render(request, "account/login.html")


@require_POST
def logout_view(request):
    """结束当前会话并跳转至 allauth 登录页。"""

    logout(request)
    return redirect("account_login")


@require_POST
def phone_code_send(request):
    """校验用途和双维度限流后请求发送手机号验证码。

    注册状态、绑定归属等业务前置条件不会反映到响应中，以免接口成为手机号
    枚举工具；不能发送时同样返回通用受理结果。
    """

    data = _json_payload(request)
    purpose = data.get("purpose") or PhoneVerificationCode.PURPOSE_LOGIN
    valid_purposes = {
        PhoneVerificationCode.PURPOSE_REGISTER,
        PhoneVerificationCode.PURPOSE_LOGIN,
        PhoneVerificationCode.PURPOSE_BIND,
        PhoneVerificationCode.PURPOSE_CHANGE_OLD,
        PhoneVerificationCode.PURPOSE_CHANGE_NEW,
        PhoneVerificationCode.PURPOSE_RESET,
    }
    if not isinstance(purpose, str) or purpose not in valid_purposes:
        return JsonResponse({"error": "验证码用途无效"}, status=400)
    account_phone_purposes = {
        PhoneVerificationCode.PURPOSE_BIND,
        PhoneVerificationCode.PURPOSE_CHANGE_OLD,
        PhoneVerificationCode.PURPOSE_CHANGE_NEW,
    }
    if purpose in account_phone_purposes and not request.user.is_authenticated:
        return JsonResponse({"error": "请先登录后再操作手机号"}, status=401)

    # 旧号验证码只能发送到服务端读取的当前绑定号码，不能信任客户端传入的旧号。
    candidate_phone = data.get("phone")
    if purpose == PhoneVerificationCode.PURPOSE_CHANGE_OLD:
        profile = UserProfile.objects.filter(user=request.user).only("phone").first()
        candidate_phone = profile.phone if profile else None
    try:
        phone = normalize_phone(candidate_phone)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    if not _allow_request(request, "sms", limit=10, window=600):
        return JsonResponse({"error": "请求过于频繁，请稍后再试"}, status=429)
    if not _allow_phone_request(phone, purpose, limit=5, window=3600):
        return JsonResponse({"error": "该手机号请求过于频繁，请稍后再试"}, status=429)
    try:
        validate_phone_code_request(phone, purpose, user=request.user)
    except ValidationError as exc:
        # 不泄露手机号是已注册、未注册还是已经绑定到另一个账户。
        return JsonResponse(PHONE_CODE_ACCEPTED_RESPONSE)
    try:
        issue_phone_verification_code(phone, purpose=purpose)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    return JsonResponse(PHONE_CODE_ACCEPTED_RESPONSE)


@require_POST
def phone_register(request):
    """使用短信验证码创建手机号账户并建立登录会话。"""

    data = _json_payload(request)
    password = data.get("password")
    password_confirm = data.get("password_confirm")
    if not isinstance(password, str) or not isinstance(password_confirm, str):
        return JsonResponse({"error": "密码格式无效"}, status=400)
    if password != password_confirm:
        return JsonResponse({"error": "两次输入的密码不一致"}, status=400)
    if not _allow_request(request, "phone-register", limit=10, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        user = register_user_with_phone_code(data.get("phone"), data.get("code"), password)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse(
        {
            "success": True,
            "action": "registered",
            "redirect_url": _safe_redirect_url(request, data.get("next")),
        }
    )


@require_POST
def phone_login(request):
    """使用已注册手机号和短信验证码建立登录会话。"""

    data = _json_payload(request)
    if not _allow_request(request, "phone-login", limit=20, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        verify_phone_code(data.get("phone"), data.get("code"), purpose=PhoneVerificationCode.PURPOSE_LOGIN)
        user = get_user_by_phone(data.get("phone"))
        if not user:
            raise ValidationError("手机号或验证码错误")
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    if not user.is_active:
        return JsonResponse({"error": "该账号已停用，请联系平台客服"}, status=403)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse(
        {
            "success": True,
            "action": "logged_in",
            "redirect_url": _safe_redirect_url(request, data.get("next")),
        }
    )


@require_POST
def password_login(request):
    """使用用户名、邮箱或手机号和密码建立登录会话。"""

    data = _json_payload(request)
    if not isinstance(data.get("login"), str) or not isinstance(data.get("password"), str):
        return JsonResponse({"error": "账号或密码格式无效"}, status=400)
    if not _allow_request(request, "password-login", limit=20, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        user = authenticate_by_login_identifier(request, data.get("login"), data.get("password"))
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse(
        {
            "success": True,
            "action": "logged_in",
            "redirect_url": _safe_redirect_url(request, data.get("next")),
        }
    )


@require_POST
def phone_password_reset(request):
    """验证短信验证码后重置账户密码。"""

    data = _json_payload(request)
    password = data.get("password")
    password_confirm = data.get("password_confirm")
    if not isinstance(password, str) or not isinstance(password_confirm, str):
        return JsonResponse({"error": "密码格式无效"}, status=400)
    if password != password_confirm:
        return JsonResponse({"error": "两次输入的密码不一致"}, status=400)
    if not _allow_request(request, "phone-reset", limit=10, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        reset_password_with_phone_code(data.get("phone"), data.get("code"), password)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    return JsonResponse({"success": True, "redirect_url": "/accounts/login/"})


@require_POST
@login_required
def phone_bind(request):
    """首次绑定用一个验证码；已有号码换绑必须同时验证当前号码和目标号码。"""

    data = _json_payload(request)
    try:
        current_profile = UserProfile.objects.filter(user=request.user).only("phone").first()
        if current_profile and current_profile.phone:
            profile = change_phone_with_codes(
                request.user,
                data.get("current_code"),
                data.get("new_phone"),
                data.get("new_code"),
            )
            action = "changed"
        else:
            profile = bind_phone_with_code(request.user, data.get("phone"), data.get("code"))
            action = "bound"
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    return JsonResponse({"success": True, "action": action, "phone": profile.phone})


def wechat_login(request):
    """保留微信登录 UI 入口，但在接入 allauth 回调前安全返回未开放状态。

    TODO：完成微信开放平台资质、回调域名和 allauth provider 配置后，再由此入口
    重定向到授权流程；不得恢复模拟用户创建逻辑。
    """

    return JsonResponse({"error": "微信登录暂未开放"}, status=503)


def alipay_login(request):
    """保留支付宝登录 UI 入口，但在 OAuth 设计完成前不执行认证。

    TODO：取得支付宝开放平台 OAuth 资质后实现授权回调及账户绑定策略。
    """

    return JsonResponse({"error": "支付宝登录暂未开放"}, status=503)


def _json_payload(request):
    """解析受大小限制的对象 JSON；无效载荷一律返回空字典。"""

    body = request.body
    if not body or len(body) > MAX_AUTH_JSON_BODY_BYTES:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_error(exc):
    """从 Django ``ValidationError`` 中提取第一个面向用户的错误文本。"""

    if hasattr(exc, "messages") and exc.messages:
        return exc.messages[0]
    return str(exc)


def _safe_redirect_url(request, candidate):
    """只接受同站相对路径或同主机 URL，阻止认证成功后的开放重定向。"""

    if isinstance(candidate, str) and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return "/"


def _allow_request(request, scope, *, limit, window):
    """以客户端 IP 和业务范围为键执行缓存计数限流。"""

    candidate_ip = request.META.get("REMOTE_ADDR", "unknown")
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        candidate_ip = request.META.get("HTTP_X_REAL_IP", candidate_ip)
    try:
        client_ip = str(ipaddress.ip_address(candidate_ip))
    except ValueError:
        client_ip = "unknown"
    return _allow_rate_key(f"rate:{scope}:{client_ip}", limit=limit, window=window)


def _allow_phone_request(phone, purpose, *, limit, window):
    """按用途和手机号散列执行第二层短信限流，不在缓存键中保存明文手机号。"""

    phone_digest = hashlib.sha256(f"{purpose}:{phone}".encode("utf-8")).hexdigest()
    return _allow_rate_key(f"rate:sms-phone:{phone_digest}", limit=limit, window=window)


def _allow_rate_key(key, *, limit, window):
    """对指定缓存键执行固定窗口计数限流，供 IP 与手机号维度共同复用。"""

    if cache.add(key, 1, timeout=window):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, timeout=window)
        return True
