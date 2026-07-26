"""账户认证与手机号验证的业务服务。

依赖流向：最上层 HTTP 视图调用本模块；本模块只写入 ``accounts`` 模型及
Django 认证用户，不依赖商品、订单、支付或展示层。短信 SDK 仅在选择腾讯云
通道时动态导入，避免本地控制台验证码模式被可选依赖阻塞。
"""

# Python 标准库依赖：生成安全随机验证码、处理手机号文本与异常类型。
import re
from random import SystemRandom

# django-allauth 依赖：已验证邮箱记录是邮箱登录和邮箱变更的唯一可信来源。
from allauth.account.internal.flows.email_verification import send_verification_email_to_address
from allauth.account.models import EmailAddress

# Django 认证与安全依赖：缓存限流、事务、密码规则和 HMAC 摘要。
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

# 本领域模型依赖：服务层只通过本应用模型保存资料和验证码。
from .models import PhoneVerificationCode, UserProfile


PHONE_CODE_TTL_MINUTES = 5
PHONE_CODE_RESEND_SECONDS = 60
PHONE_CODE_MAX_ATTEMPTS = 5
PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
_random = SystemRandom()
_DUMMY_EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"


def normalize_phone(phone):
    """规范化并校验中国大陆手机号，返回不含 ``+86`` 的十一位号码。"""

    normalized_phone = re.sub(r"\s+", "", str(phone or ""))
    if normalized_phone.startswith("+86"):
        normalized_phone = normalized_phone[3:]
    if not PHONE_PATTERN.fullmatch(normalized_phone):
        raise ValidationError("请输入有效的中国大陆手机号")
    return normalized_phone


def issue_phone_verification_code(phone, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):
    """生成、发送并记录一条验证码摘要，返回仅供本地测试读取的记录对象。

    调用方应先使用 :func:`validate_phone_code_request` 校验注册、登录或绑定的
    业务前置条件；本函数负责频率限制、短信通道调用和验证码落库。
    """

    normalized_phone = normalize_phone(phone)
    rate_key = f"sms-send:{purpose}:{normalized_phone}"
    if not cache.add(rate_key, "1", timeout=PHONE_CODE_RESEND_SECONDS):
        raise ValidationError("验证码发送太频繁，请稍后再试")

    try:
        _assert_phone_code_can_send(normalized_phone, purpose)
        raw_code = f"{_random.randint(0, 999999):06d}"
        verification = PhoneVerificationCode.objects.create(
            phone=normalized_phone,
            code=_phone_code_digest(normalized_phone, purpose, raw_code),
            purpose=purpose,
            expires_at=timezone.now() + timezone.timedelta(minutes=PHONE_CODE_TTL_MINUTES),
            sent_to_backend=send_phone_verification_code(normalized_phone, raw_code, purpose),
        )
    except Exception:
        cache.delete(rate_key)
        raise

    # 该临时属性不会写入数据库，仅为开发控制台和自动化测试提供验证码。
    verification._raw_code = raw_code
    return verification


def validate_phone_code_request(phone, purpose, *, user=None):
    """校验指定用途是否允许向该手机号发送验证码，并返回规范化号码。"""

    normalized_phone = normalize_phone(phone)
    existing_user = get_user_by_phone(normalized_phone)
    is_authenticated = bool(user and getattr(user, "is_authenticated", False))

    if purpose == PhoneVerificationCode.PURPOSE_REGISTER and existing_user:
        raise ValidationError("该手机号已注册，请直接登录")
    if purpose == PhoneVerificationCode.PURPOSE_LOGIN and not existing_user:
        raise ValidationError("该手机号未注册，请先注册")
    if purpose == PhoneVerificationCode.PURPOSE_RESET and not existing_user:
        raise ValidationError("该手机号未注册")
    if purpose in {
        PhoneVerificationCode.PURPOSE_BIND,
        PhoneVerificationCode.PURPOSE_CHANGE_OLD,
        PhoneVerificationCode.PURPOSE_CHANGE_NEW,
    }:
        if not is_authenticated:
            raise ValidationError("请先登录后再操作手机号")
        profile = UserProfile.objects.filter(user=user).only("phone").first()
        current_phone = profile.phone if profile else None

        if purpose == PhoneVerificationCode.PURPOSE_BIND and current_phone:
            raise ValidationError("当前账号已绑定手机号，请使用换绑流程")
        if purpose == PhoneVerificationCode.PURPOSE_CHANGE_OLD:
            if not current_phone:
                raise ValidationError("当前账号尚未绑定手机号")
            if current_phone != normalized_phone:
                raise ValidationError("当前手机号与登录账号不匹配")
        if purpose == PhoneVerificationCode.PURPOSE_CHANGE_NEW:
            if not current_phone:
                raise ValidationError("当前账号尚未绑定手机号，请先完成首次绑定")
            if current_phone == normalized_phone:
                raise ValidationError("新手机号不能与当前手机号相同")

        if purpose != PhoneVerificationCode.PURPOSE_CHANGE_OLD and UserProfile.objects.filter(
            phone=normalized_phone
        ).exclude(user=user).exists():
            raise ValidationError("该手机号已绑定其他账号")
    return normalized_phone


def send_phone_verification_code(phone, code, purpose):
    """按环境配置将验证码交给控制台或腾讯云短信通道。"""

    provider = str(getattr(settings, "SMS_PROVIDER", "console")).lower()
    if provider == "console":
        return _send_console_sms(phone, code, purpose)
    if provider == "tencent":
        return _send_tencent_sms(phone, code)
    raise ValidationError("短信服务未配置")


def _send_tencent_sms(phone, code):
    """通过腾讯云短信 SDK 发送验证码；缺少生产配置时返回可读错误。"""

    sms_secret_id = getattr(settings, "TENCENT_SMS_SECRET_ID", "")
    sms_secret_key = getattr(settings, "TENCENT_SMS_SECRET_KEY", "")
    sms_sdk_app_id = getattr(settings, "TENCENT_SMS_SDK_APP_ID", "")
    sms_sign_name = getattr(settings, "TENCENT_SMS_SIGN_NAME", "")
    sms_template_id = getattr(settings, "TENCENT_SMS_TEMPLATE_ID", "")
    if not all([sms_secret_id, sms_secret_key, sms_sdk_app_id, sms_sign_name, sms_template_id]):
        raise ValidationError("腾讯云短信参数未配置完整")

    try:
        # 依赖流向：accounts -> 可选腾讯云 SDK，仅在真实短信模式下加载。
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.sms.v20210111 import models as sms_models
        from tencentcloud.sms.v20210111.sms_client import SmsClient
    except ImportError as exc:
        raise ValidationError("请先安装腾讯云短信 SDK：tencentcloud-sdk-python") from exc

    try:
        credentials = credential.Credential(sms_secret_id, sms_secret_key)
        client = SmsClient(credentials, getattr(settings, "TENCENT_SMS_REGION", "ap-guangzhou"))
        request = sms_models.SendSmsRequest()
        request.SmsSdkAppId = sms_sdk_app_id
        request.SignName = sms_sign_name
        request.TemplateId = sms_template_id
        request.TemplateParamSet = [code, str(PHONE_CODE_TTL_MINUTES)]
        request.PhoneNumberSet = [f"+86{phone}"]
        response = client.SendSms(request)
    except TencentCloudSDKException as exc:
        raise ValidationError(f"短信发送失败：{exc}") from exc

    statuses = getattr(response, "SendStatusSet", None) or []
    if not statuses:
        raise ValidationError("短信发送失败：腾讯云未返回发送结果")
    first_status = statuses[0]
    if getattr(first_status, "Code", "") != "Ok":
        raise ValidationError(f"短信发送失败：{getattr(first_status, 'Message', '未知错误')}")
    return True


def _send_console_sms(phone, code, purpose):
    """将验证码输出到本地控制台，仅用于开发和自动化测试。"""

    print(f"[ShopLite SMS] phone={phone} purpose={purpose} code={code}")
    return True


def verify_phone_code(phone, code, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):
    """在事务和行锁中校验并一次性消费一条最新可用验证码。"""

    normalized_phone = normalize_phone(phone)
    raw_code = _normalize_phone_code(code)

    with transaction.atomic():
        verification, error_message = _check_phone_code_locked(normalized_phone, raw_code, purpose)
        if verification:
            _consume_phone_codes(verification)

    if error_message:
        raise ValidationError(error_message)
    return verification


def _normalize_phone_code(code):
    """校验短信验证码的传输格式，返回六位数字文本。"""

    raw_code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", raw_code):
        raise ValidationError("请输入 6 位短信验证码")
    return raw_code


def _check_phone_code_locked(phone, raw_code, purpose):
    """在调用方事务中锁定并检查验证码，但把成功消费留给业务操作统一提交。

    校验失败的尝试计数会在当前事务正常结束时保留；成功记录只有在绑定或换绑的
    其他条件也通过后才会被消费，避免其中一步失败却作废另一条正确验证码。
    """

    verification = (
        PhoneVerificationCode.objects.select_for_update()
        .filter(phone=phone, purpose=purpose, used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if not verification:
        return None, "短信验证码不正确或已失效"
    if verification.is_expired:
        verification.used_at = timezone.now()
        verification.save(update_fields=["used_at"])
        return None, "短信验证码已过期"
    if verification.attempt_count >= PHONE_CODE_MAX_ATTEMPTS:
        verification.used_at = timezone.now()
        verification.save(update_fields=["used_at"])
        return None, "验证码尝试次数过多，请重新获取"
    if constant_time_compare(
        verification.code,
        _phone_code_digest(phone, purpose, raw_code),
    ):
        return verification, None

    verification.attempt_count += 1
    verification.last_attempt_at = timezone.now()
    update_fields = ["attempt_count", "last_attempt_at"]
    if verification.attempt_count >= PHONE_CODE_MAX_ATTEMPTS:
        verification.used_at = timezone.now()
        update_fields.append("used_at")
        error_message = "验证码尝试次数过多，请重新获取"
    else:
        error_message = "短信验证码不正确"
    verification.save(update_fields=update_fields)
    return None, error_message


def _consume_phone_codes(*verifications):
    """在同一事务中将一条或多条已校验验证码标记为已使用。"""

    used_at = timezone.now()
    for verification in verifications:
        verification.used_at = used_at
        verification.last_attempt_at = used_at
        verification.save(update_fields=["used_at", "last_attempt_at"])


def _phone_code_digest(phone, purpose, code):
    """计算手机号、用途和明文验证码的带盐 HMAC 摘要。"""

    return salted_hmac("shoplite.phone-verification", f"{phone}|{purpose}|{code}").hexdigest()


def get_user_by_phone(phone):
    """根据已绑定手机号查询用户；未绑定时返回 ``None``。"""

    normalized_phone = normalize_phone(phone)
    profile = UserProfile.objects.select_related("user").filter(phone=normalized_phone).first()
    return profile.user if profile else None


@transaction.atomic
def register_user_by_phone(phone, *, password=None):
    """创建一个手机号已验证的 Django 用户及其资料记录。"""

    normalized_phone = normalize_phone(phone)
    if UserProfile.objects.filter(phone=normalized_phone).exists():
        raise ValidationError("该手机号已注册，请直接登录")
    if not password:
        raise ValidationError("请设置登录密码")

    user_model = get_user_model()
    username = _build_phone_username(normalized_phone)
    user = user_model(username=username)
    validate_password(password, user=user)
    user.set_password(password)
    try:
        user.save()
        UserProfile.objects.create(
            user=user,
            phone=normalized_phone,
            phone_verified_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise ValidationError("该手机号已注册，请直接登录") from exc
    return user


def register_user_with_phone_code(phone, code, password):
    """先验证注册验证码和密码强度，再原子地创建手机号账户。"""

    normalized_phone = normalize_phone(phone)
    if UserProfile.objects.filter(phone=normalized_phone).exists():
        raise ValidationError("该手机号已注册，请直接登录")
    candidate = get_user_model()(username=_build_phone_username(normalized_phone))
    validate_password(password, user=candidate)
    verify_phone_code(normalized_phone, code, purpose=PhoneVerificationCode.PURPOSE_REGISTER)
    return register_user_by_phone(normalized_phone, password=password)


def authenticate_by_login_identifier(request, identifier, password):
    """使用用户名、已验证邮箱或已绑定手机号验证密码并返回认证用户。

    邮箱只通过 allauth 的 ``EmailAddress(verified=True, primary=True)`` 解析，
    不会因为资料表单刚写入 ``User.email`` 就成为登录标识。用户名优先于邮箱，
    兼容历史上看起来像邮箱的用户名。
    """

    normalized_identifier = str(identifier or "").strip()
    if not normalized_identifier or not password:
        raise ValidationError("请输入账号和密码")

    user = _user_for_login_identifier(normalized_identifier)
    if not user or not user.check_password(password) or not user.is_active:
        # 与 Django 的认证后端保持近似的失败耗时，减少通过响应时间枚举账号的风险。
        if not user:
            get_user_model()().set_password(password)
        raise ValidationError("账号或密码错误")
    return user


def _user_for_login_identifier(identifier):
    """按用户名、已验证邮箱、手机号的固定顺序解析一个唯一认证用户。"""

    user_model = get_user_model()
    username_field = getattr(user_model, "USERNAME_FIELD", "username")
    username_user = user_model.objects.filter(**{username_field: identifier}).first()
    if username_user:
        return username_user

    if "@" in identifier:
        return get_user_by_verified_email(identifier)

    try:
        return get_user_by_phone(identifier)
    except ValidationError:
        return None


def get_user_by_verified_email(email):
    """只返回唯一的、与当前主邮箱一致的已验证账号。

    MySQL 不支持 allauth 的部分条件唯一约束时，异常重复记录会导致本函数返回
    ``None``，从而以拒绝登录而非猜测归属的方式安全失败。
    """

    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None
    matches = list(
        EmailAddress.objects.select_related("user")
        .filter(
            email__iexact=normalized_email,
            verified=True,
            primary=True,
            user__is_active=True,
            user__email__iexact=normalized_email,
        )
        .order_by("id")[:2]
    )
    return matches[0].user if len(matches) == 1 else None


def email_verification_delivery_configured():
    """判断当前环境是否可真实投递 allauth 邮箱验证邮件。

    生产默认 dummy backend 会吞掉邮件；此时必须拒绝变更而不是创建一个用户误以为
    已经发出的待验证地址。开发环境的 console backend 则可用于本地验证流程调试。
    """

    return str(getattr(settings, "EMAIL_BACKEND", "")).strip() != _DUMMY_EMAIL_BACKEND


@transaction.atomic
def request_email_change(user, email, *, request):
    """创建并发送一条 allauth 邮箱变更验证，验证完成前不修改 ``User.email``。

    allauth 的 ``ACCOUNT_CHANGE_EMAIL=True`` 配置会在用户点击验证链接后，将该
    地址提升为主邮箱并同步到 ``User.email``。本服务保留旧已验证邮箱直至新地址
    完成验证，因此中途失败不会让用户失去邮箱登录能力。
    """

    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        raise ValidationError("邮箱不能为空；如需移除邮箱请联系平台客服")
    if request is None:
        raise ValidationError("请通过账户设置页面发起邮箱验证")
    if not email_verification_delivery_configured():
        raise ValidationError("邮箱验证服务暂未配置，暂不能更换邮箱")

    user_model = get_user_model()
    locked_user = user_model.objects.select_for_update().get(pk=user.pk)
    other_user_has_email = user_model.objects.exclude(pk=locked_user.pk).filter(
        email__iexact=normalized_email
    ).exists()
    other_account_has_address = EmailAddress.objects.select_for_update().filter(
        email__iexact=normalized_email
    ).exclude(user_id=locked_user.pk).exists()
    if other_user_has_email or other_account_has_address:
        raise ValidationError("该邮箱已绑定其他账号")

    addresses = EmailAddress.objects.select_for_update().filter(user_id=locked_user.pk)
    verified_address = addresses.filter(email__iexact=normalized_email, verified=True).first()
    if verified_address:
        if not verified_address.primary:
            verified_address.set_as_primary()
        return verified_address

    # 一次只保留一个待验证的新邮箱；切换目标时旧确认链接会随记录一起失效。
    addresses.filter(verified=False).delete()
    try:
        email_address = EmailAddress.objects.create(
            user=locked_user,
            email=normalized_email,
            verified=False,
            primary=False,
        )
        sent = send_verification_email_to_address(request, email_address)
    except Exception as exc:
        # 事务会回滚新地址和确认令牌，不会让 UI 把失败误报成成功。
        raise ValidationError("邮箱验证邮件发送失败，请稍后重试") from exc
    if not sent:
        raise ValidationError("邮箱验证请求过于频繁，请稍后重试")
    return email_address


@transaction.atomic
def bind_phone_to_user(user, phone):
    """将未被其他账号占用的手机号原子地首次绑定到当前用户资料。

    查询阶段会锁定已存在的同号码资料；若另一请求恰好在空隙中抢先写入，唯一
    约束产生的 ``IntegrityError`` 也会转换为可预期的业务校验错误，而不是 500。
    已绑定不同号码的账号必须进入双验证码换绑流程，不能调用本函数直接覆盖。
    """

    normalized_phone = normalize_phone(phone)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile = UserProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.phone and profile.phone != normalized_phone:
        raise ValidationError("当前账号已绑定手机号，请使用换绑流程")
    if profile.phone == normalized_phone:
        return profile
    try:
        # 内层保存点让唯一约束竞争可被安全捕获，外层事务仍能正常完成回滚。
        with transaction.atomic():
            if (
                UserProfile.objects.select_for_update()
                .filter(phone=normalized_phone)
                .exclude(user=user)
                .exists()
            ):
                raise ValidationError("该手机号已绑定其他账号")
            profile.phone = normalized_phone
            profile.phone_verified_at = timezone.now()
            profile.save(update_fields=["phone", "phone_verified_at", "updated_at"])
    except IntegrityError as exc:
        raise ValidationError("该手机号已绑定其他账号") from exc
    return profile


def bind_phone_with_code(user, phone, code):
    """验证绑定用途验证码后，仅为尚未绑定手机号的登录账号完成首次绑定。"""

    normalized_phone = normalize_phone(phone)
    raw_code = _normalize_phone_code(code)
    error_message = None
    result_profile = None

    with transaction.atomic():
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile = UserProfile.objects.select_for_update().get(pk=profile.pk)
        if profile.phone:
            error_message = "当前账号已绑定手机号，请使用换绑流程"
        elif UserProfile.objects.select_for_update().filter(phone=normalized_phone).exclude(user=user).exists():
            error_message = "该手机号已绑定其他账号"
        else:
            verification, error_message = _check_phone_code_locked(
                normalized_phone,
                raw_code,
                PhoneVerificationCode.PURPOSE_BIND,
            )
            if verification:
                try:
                    with transaction.atomic():
                        profile.phone = normalized_phone
                        profile.phone_verified_at = timezone.now()
                        profile.save(update_fields=["phone", "phone_verified_at", "updated_at"])
                except IntegrityError:
                    error_message = "该手机号已绑定其他账号"
                else:
                    _consume_phone_codes(verification)
                    result_profile = profile

    if error_message:
        raise ValidationError(error_message)
    return result_profile


def change_phone_with_codes(user, current_code, new_phone, new_code):
    """在单一事务内验证当前号码和目标号码验证码，并原子地完成手机号换绑。

    当前号码始终从已锁定的用户资料中读取，客户端不能替换其归属。新号码占用
    状态会在验证码校验前后由资料行锁和唯一约束共同保护；任一步失败都不会消费
    另一条正确验证码，也不会改变账号手机号。
    """

    normalized_new_phone = normalize_phone(new_phone)
    raw_current_code = _normalize_phone_code(current_code)
    raw_new_code = _normalize_phone_code(new_code)
    error_message = None
    result_profile = None

    with transaction.atomic():
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile = UserProfile.objects.select_for_update().get(pk=profile.pk)
        current_phone = profile.phone
        if not current_phone:
            error_message = "当前账号尚未绑定手机号，请先完成首次绑定"
        elif current_phone == normalized_new_phone:
            error_message = "新手机号不能与当前手机号相同"
        elif UserProfile.objects.select_for_update().filter(phone=normalized_new_phone).exclude(
            user=user
        ).exists():
            error_message = "该手机号已绑定其他账号"
        else:
            current_verification, error_message = _check_phone_code_locked(
                current_phone,
                raw_current_code,
                PhoneVerificationCode.PURPOSE_CHANGE_OLD,
            )
            new_verification = None
            if current_verification:
                new_verification, error_message = _check_phone_code_locked(
                    normalized_new_phone,
                    raw_new_code,
                    PhoneVerificationCode.PURPOSE_CHANGE_NEW,
                )
            if current_verification and new_verification:
                try:
                    with transaction.atomic():
                        profile.phone = normalized_new_phone
                        profile.phone_verified_at = timezone.now()
                        profile.save(update_fields=["phone", "phone_verified_at", "updated_at"])
                except IntegrityError:
                    error_message = "该手机号已绑定其他账号"
                else:
                    _consume_phone_codes(current_verification, new_verification)
                    result_profile = profile

    if error_message:
        raise ValidationError(error_message)
    return result_profile


def reset_password_with_phone_code(phone, code, new_password):
    """验证重置验证码后更新已绑定手机号账户的密码。"""

    user = get_user_by_phone(phone)
    if not user:
        raise ValidationError("该手机号未注册")
    validate_password(new_password, user=user)
    verify_phone_code(phone, code, purpose=PhoneVerificationCode.PURPOSE_RESET)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    return user


def _assert_phone_code_can_send(phone, purpose):
    """检查同一手机号和用途的最近验证码是否仍在重发冷却期。"""

    latest = PhoneVerificationCode.objects.filter(phone=phone, purpose=purpose).order_by("-created_at").first()
    if not latest:
        return
    elapsed_seconds = (timezone.now() - latest.created_at).total_seconds()
    if elapsed_seconds < PHONE_CODE_RESEND_SECONDS:
        wait_seconds = int(PHONE_CODE_RESEND_SECONDS - elapsed_seconds)
        raise ValidationError(f"验证码发送太频繁，请 {wait_seconds} 秒后再试")


def _build_phone_username(phone):
    """为手机号注册生成唯一的内部用户名，保留用户后续修改昵称的空间。"""

    user_model = get_user_model()
    base = f"phone_{phone}"
    username = base
    suffix = 1
    while user_model.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}_{suffix}"
    return username
