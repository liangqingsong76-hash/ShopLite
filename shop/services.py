import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from random import SystemRandom
import re

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils.crypto import get_random_string
from django.utils import timezone

from .models import Address, CartItem, Order, OrderItem, PhoneVerificationCode, Product, UserProfile

FREE_SHIPPING = Decimal("0.00")
DISCOUNT_THRESHOLD = Decimal("299.00")
DISCOUNT_AMOUNT = Decimal("30.00")
MIN_QUANTITY = 1
MAX_QUANTITY = 99
PHONE_CODE_TTL_MINUTES = 5
PHONE_CODE_RESEND_SECONDS = 60
PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
_random = SystemRandom()


@dataclass(frozen=True)
class CartTotals:
    subtotal: Decimal
    discount: Decimal
    shipping_fee: Decimal
    payable: Decimal


def parse_quantity(value, *, default=1):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(MIN_QUANTITY, min(quantity, MAX_QUANTITY))


def parse_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def normalize_phone(phone):
    phone = re.sub(r"\s+", "", str(phone or ""))
    if phone.startswith("+86"):
        phone = phone[3:]
    if not PHONE_PATTERN.match(phone):
        raise ValidationError("请输入有效的中国大陆手机号")
    return phone


def issue_phone_verification_code(phone, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):
    phone = normalize_phone(phone)
    _assert_phone_code_can_send(phone, purpose)
    code = f"{_random.randint(0, 999999):06d}"
    verification = PhoneVerificationCode.objects.create(
        phone=phone,
        code=code,
        purpose=purpose,
        expires_at=timezone.now() + timezone.timedelta(minutes=PHONE_CODE_TTL_MINUTES),
        sent_to_backend=send_phone_verification_code(phone, code, purpose),
    )
    return verification


def validate_phone_code_request(phone, purpose, *, user=None):
    phone = normalize_phone(phone)
    existing_user = get_user_by_phone(phone)

    if purpose == PhoneVerificationCode.PURPOSE_REGISTER and existing_user:
        raise ValidationError("该手机号已注册，请直接登录")
    if purpose == PhoneVerificationCode.PURPOSE_LOGIN and not existing_user:
        raise ValidationError("该手机号未注册，请先注册")
    if purpose == PhoneVerificationCode.PURPOSE_BIND:
        qs = UserProfile.objects.filter(phone=phone)
        if user and getattr(user, "is_authenticated", False):
            qs = qs.exclude(user=user)
        if qs.exists():
            raise ValidationError("该手机号已绑定其他账号")
    return phone


def send_phone_verification_code(phone, code, purpose):
    provider = getattr(settings, "SMS_PROVIDER", "console")
    if provider == "console":
        print(f"[ShopLite SMS] phone={phone} purpose={purpose} code={code}")
        return True
    if provider == "tencent":
        return _send_tencent_sms(phone, code)
    raise ValidationError("短信服务未配置")


def _send_tencent_sms(phone, code):
    sms_secret_id = getattr(settings, "TENCENT_SMS_SECRET_ID", "")
    sms_secret_key = getattr(settings, "TENCENT_SMS_SECRET_KEY", "")
    sms_sdk_app_id = getattr(settings, "TENCENT_SMS_SDK_APP_ID", "")
    sms_sign_name = getattr(settings, "TENCENT_SMS_SIGN_NAME", "")
    sms_template_id = getattr(settings, "TENCENT_SMS_TEMPLATE_ID", "")

    if not all([sms_secret_id, sms_secret_key, sms_sdk_app_id, sms_sign_name, sms_template_id]):
        raise ValidationError("腾讯云短信参数未配置完整")

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.sms.v20210111 import models as sms_models
        from tencentcloud.sms.v20210111.sms_client import SmsClient
    except ImportError as exc:
        raise ValidationError("请先安装腾讯云短信 SDK：pip install tencentcloud-sdk-python") from exc

    try:
        cred = credential.Credential(sms_secret_id, sms_secret_key)
        client = SmsClient(cred, getattr(settings, "TENCENT_SMS_REGION", "ap-guangzhou"))
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
        message = getattr(first_status, "Message", "未知错误")
        raise ValidationError(f"短信发送失败：{message}")
    return True


def _send_console_sms(phone, code, purpose):
    print(f"[ShopLite SMS] phone={phone} purpose={purpose} code={code}")
    return True


def verify_phone_code(phone, code, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):
    phone = normalize_phone(phone)
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValidationError("请输入 6 位短信验证码")

    verification = (
        PhoneVerificationCode.objects.filter(phone=phone, purpose=purpose, used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if not verification or verification.code != code:
        raise ValidationError("短信验证码不正确")
    if verification.is_expired:
        raise ValidationError("短信验证码已过期")

    verification.used_at = timezone.now()
    verification.save(update_fields=["used_at"])
    return verification


def get_user_by_phone(phone):
    phone = normalize_phone(phone)
    profile = UserProfile.objects.select_related("user").filter(phone=phone).first()
    if profile:
        return profile.user
    return None


@transaction.atomic
def register_user_by_phone(phone, *, password=None):
    phone = normalize_phone(phone)
    if UserProfile.objects.filter(phone=phone).exists():
        raise ValidationError("该手机号已注册，请直接登录")

    username = _build_phone_username(phone)
    user = User.objects.create_user(username=username, password=password or get_random_string(32))
    UserProfile.objects.create(user=user, phone=phone, phone_verified_at=timezone.now())
    return user


def authenticate_by_login_identifier(request, identifier, password):
    identifier = str(identifier or "").strip()
    if not identifier or not password:
        raise ValidationError("请输入账号和密码")

    username = identifier
    try:
        user = get_user_by_phone(identifier)
    except ValidationError:
        user = None
    if user:
        username = user.username

    user = authenticate(request, username=username, password=password)
    if not user:
        raise ValidationError("账号或密码错误")
    return user


@transaction.atomic
def get_or_create_user_by_wechat(uid, *, nickname="微信用户", extra_data=None):
    try:
        from allauth.socialaccount.models import SocialAccount
    except ImportError as exc:
        raise ValidationError("微信登录依赖 django-allauth，请先安装并启用") from exc

    uid = str(uid or "").strip()
    if not uid:
        raise ValidationError("微信用户标识不能为空")

    social_account = SocialAccount.objects.select_related("user").filter(provider="weixin", uid=uid).first()
    if social_account:
        return social_account.user, False

    username = _build_social_username("wx")
    user = User.objects.create_user(username=username)
    if nickname:
        user.first_name = str(nickname)[:150]
        user.save(update_fields=["first_name"])
    SocialAccount.objects.create(
        user=user,
        provider="weixin",
        uid=uid,
        extra_data=extra_data or {"nickname": nickname},
    )
    UserProfile.objects.get_or_create(user=user)
    return user, True


def mock_wechat_uid():
    return "mock-wechat-openid"


def bind_phone_to_user(user, phone):
    phone = normalize_phone(phone)
    if UserProfile.objects.filter(phone=phone).exclude(user=user).exists():
        raise ValidationError("该手机号已绑定其他账号")

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.phone = phone
    profile.phone_verified_at = timezone.now()
    profile.save(update_fields=["phone", "phone_verified_at", "updated_at"])
    return profile


def _assert_phone_code_can_send(phone, purpose):
    latest = PhoneVerificationCode.objects.filter(phone=phone, purpose=purpose).order_by("-created_at").first()
    if not latest:
        return
    elapsed = (timezone.now() - latest.created_at).total_seconds()
    if elapsed < PHONE_CODE_RESEND_SECONDS:
        wait_seconds = int(PHONE_CODE_RESEND_SECONDS - elapsed)
        raise ValidationError(f"验证码发送太频繁，请 {wait_seconds} 秒后再试")


def _build_phone_username(phone):
    base = f"phone_{phone}"
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}_{suffix}"
    return username


def _build_social_username(prefix):
    base = f"{prefix}_{get_random_string(12).lower()}"
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}_{suffix}"
    return username


def calculate_cart_totals(cart_items):
    subtotal = sum((item.subtotal for item in cart_items), Decimal("0.00"))
    discount = DISCOUNT_AMOUNT if subtotal >= DISCOUNT_THRESHOLD else Decimal("0.00")
    payable = subtotal - discount + FREE_SHIPPING
    return CartTotals(
        subtotal=subtotal,
        discount=discount,
        shipping_fee=FREE_SHIPPING,
        payable=payable,
    )


def build_address_snapshot(address):
    if not address:
        return ""
    return f"{address.receiver} {address.phone} {address.province}{address.city}{address.district} {address.detail}".strip()


def add_product_to_cart(user, product_id, *, quantity=1, color="", specs=""):
    quantity = parse_quantity(quantity)
    product = Product.objects.get(id=product_id, is_active=True)
    variant = _build_variant_text(color, specs)

    item, created = CartItem.objects.get_or_create(
        user=user,
        product=product,
        color=variant,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity = min(item.quantity + quantity, MAX_QUANTITY)
        item.save(update_fields=["quantity", "updated_at"])
    return item


def _build_variant_text(color, specs):
    color = (color or "").strip()
    specs = (specs or "").strip()
    if color and specs:
        return f"{color} | {specs}"
    return color or specs


@transaction.atomic
def create_order_from_cart(user, *, address_id=None):
    cart_items = list(
        CartItem.objects.select_related("product")
        .select_for_update()
        .filter(user=user)
        .order_by("id")
    )
    if not cart_items:
        raise ValidationError("购物车为空")

    address = None
    if address_id:
        address = Address.objects.filter(id=address_id, user=user).first()

    totals = calculate_cart_totals(cart_items)
    order = Order.objects.create(
        user=user,
        order_no=generate_order_no(),
        total_amount=totals.subtotal,
        discount_amount=totals.discount,
        shipping_fee=totals.shipping_fee,
        pay_amount=totals.payable,
        address_text=build_address_snapshot(address),
        status=Order.STATUS_PENDING,
    )

    OrderItem.objects.bulk_create([_build_order_item(order, item) for item in cart_items])
    CartItem.objects.filter(id__in=[item.id for item in cart_items]).delete()
    return order


def _build_order_item(order, cart_item):
    product = cart_item.product
    return OrderItem(
        order=order,
        product=product,
        product_name=product.name,
        product_image=product.image.url if product.image else "",
        price=product.price,
        quantity=cart_item.quantity,
        subtotal=product.price * cart_item.quantity,
    )


def generate_order_no():
    return timezone.now().strftime("SL%Y%m%d%H%M%S") + uuid.uuid4().hex[:8].upper()


@transaction.atomic
def mark_order_paid(order, *, paid_at=None, payment_no=""):
    locked_order = (
        Order.objects.select_for_update()
        .prefetch_related("items__product")
        .get(id=order.id)
    )

    if locked_order.status == Order.STATUS_PAID:
        return locked_order, False
    if locked_order.status != Order.STATUS_PENDING:
        raise ValidationError("只有待付款订单可以确认支付")

    for item in locked_order.items.select_related("product"):
        if not item.product_id:
            continue
        updated = Product.objects.filter(
            id=item.product_id,
            stock__gte=item.quantity,
        ).update(
            stock=F("stock") - item.quantity,
            sales=F("sales") + item.quantity,
        )
        if updated != 1:
            raise ValidationError(f"商品库存不足：{item.product_name}")

    locked_order.status = Order.STATUS_PAID
    locked_order.paid_at = paid_at or timezone.now()
    locked_order.save(update_fields=["status", "paid_at"])
    return locked_order, True


def cancel_pending_order(order):
    if order.status != Order.STATUS_PENDING:
        return order, False
    order.status = Order.STATUS_CANCELLED
    order.save(update_fields=["status"])
    return order, True
