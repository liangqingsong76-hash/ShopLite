"""旧 ``shop.services`` 的兼容导出层。

依赖流向：旧 storefront/API/admin -> 本模块 -> accounts/commerce/payments 领域服务。
新业务代码应直接引用所属领域；本模块只保证迁移期间的旧导入路径可用。
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.crypto import get_random_string

from accounts.models import UserProfile
from accounts.services import (
    _phone_code_digest,
    authenticate_by_login_identifier,
    bind_phone_to_user,
    bind_phone_with_code,
    change_phone_with_codes,
    get_user_by_phone,
    issue_phone_verification_code,
    normalize_phone,
    register_user_by_phone,
    register_user_with_phone_code,
    reset_password_with_phone_code,
    validate_phone_code_request,
    verify_phone_code,
)
from commerce.services import (
    CartTotals,
    add_product_to_cart,
    approve_refund_request,
    available_checkout_payment_methods,
    calculate_cart_totals,
    calculate_coupon_discount,
    cancel_pending_order,
    claim_coupon,
    complete_order,
    complete_refund,
    create_order_from_cart,
    create_refund_request,
    mark_order_shipped,
    parse_decimal,
    parse_quantity,
    reject_refund_request,
)
from notifications.services import create_notification
from payments.services import generate_payment_no, mark_order_paid


@transaction.atomic
def get_or_create_user_by_wechat(uid, *, nickname="微信用户", extra_data=None):
    """兼容仅限本地模拟模式的微信账号创建入口。

    真实 OAuth 始终交给 django-allauth；生产配置禁止启用 mock，因此该函数不会
    代替开放平台验签或授权回调。
    """

    try:
        from allauth.socialaccount.models import SocialAccount
    except ImportError as exc:
        raise ValidationError("微信登录依赖 django-allauth，请先安装并启用") from exc

    normalized_uid = str(uid or "").strip()
    if not normalized_uid:
        raise ValidationError("微信用户标识不能为空")

    social_account = (
        SocialAccount.objects.select_related("user")
        .filter(provider="weixin", uid=normalized_uid)
        .first()
    )
    if social_account:
        return social_account.user, False

    user = User.objects.create_user(username=_build_social_username("wx"))
    if nickname:
        user.first_name = str(nickname)[:150]
        user.save(update_fields=["first_name"])
    SocialAccount.objects.create(
        user=user,
        provider="weixin",
        uid=normalized_uid,
        extra_data=extra_data or {"nickname": nickname},
    )
    UserProfile.objects.get_or_create(user=user)
    return user, True


def mock_wechat_uid():
    """返回仅供显式本地 mock 模式使用的固定测试标识。"""

    return "mock-wechat-openid"


def _build_social_username(prefix):
    """生成不会与现有 Django 用户名冲突的社交账号用户名。"""

    while True:
        username = f"{prefix}_{get_random_string(12).lower()}"
        if not User.objects.filter(username=username).exists():
            return username


__all__ = (
    "CartTotals",
    "_phone_code_digest",
    "add_product_to_cart",
    "approve_refund_request",
    "authenticate_by_login_identifier",
    "available_checkout_payment_methods",
    "bind_phone_to_user",
    "bind_phone_with_code",
    "calculate_cart_totals",
    "calculate_coupon_discount",
    "cancel_pending_order",
    "change_phone_with_codes",
    "claim_coupon",
    "complete_order",
    "complete_refund",
    "create_notification",
    "create_order_from_cart",
    "create_refund_request",
    "generate_payment_no",
    "get_or_create_user_by_wechat",
    "get_user_by_phone",
    "issue_phone_verification_code",
    "mark_order_paid",
    "mark_order_shipped",
    "mock_wechat_uid",
    "normalize_phone",
    "parse_decimal",
    "parse_quantity",
    "register_user_by_phone",
    "register_user_with_phone_code",
    "reject_refund_request",
    "reset_password_with_phone_code",
    "validate_phone_code_request",
    "verify_phone_code",
)
