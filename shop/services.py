# 业务逻辑层
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from random import SystemRandom
import re

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils.crypto import constant_time_compare, get_random_string, salted_hmac
from django.utils import timezone

from .models import (
    Address,
    CartItem,
    Coupon,
    Notification,
    Order,
    OrderItem,
    PaymentTransaction,
    PhoneVerificationCode,
    Product,
    RefundRequest,
    UserCoupon,
    UserProfile,
)

# 常量
FREE_SHIPPING = Decimal("0.00")    # Decimal("xxx")数据类型转化，带引号数据更精确
DISCOUNT_THRESHOLD = Decimal("299.00")
DISCOUNT_AMOUNT = Decimal("30.00")
MIN_QUANTITY = 1
MAX_QUANTITY = 99
PHONE_CODE_TTL_MINUTES = 5
PHONE_CODE_RESEND_SECONDS = 60
PHONE_CODE_MAX_ATTEMPTS = 5
PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")  # 手机号正则
_random = SystemRandom()    # 创建密码学安全的随机数生成器

'''
语法	                含义
@dataclass	        自动生成 __init__、__repr__ 等方法，不用手写 def __init__(self, ...)
frozen=True	        实例化后不可修改，类似只读对象，防止金额被意外篡改
subtotal: Decimal	类型注解，表示这个字段应该传 Decimal 类型，功能类似注释
'''
# 购物车结算数据类
@dataclass(frozen=True)
class CartTotals:
    subtotal: Decimal            # 小计
    discount: Decimal            # 总优惠
    promotion_discount: Decimal  # 满减优惠
    coupon_discount: Decimal     # 优惠券优惠
    shipping_fee: Decimal        # 运费
    payable: Decimal             # 最终应付


# 限制传入参数在 1-99 之间，防恶意传参
def parse_quantity(value, *, default=1):    # *号强制关键字传参，如parse_quantity("5", default=3)
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(MIN_QUANTITY, min(quantity, MAX_QUANTITY))  # 常量：MIN_QUANTITY = 1  MAX_QUANTITY = 99


# 安全地将传入参数转为Decimal类型
def parse_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# 去掉空格、去掉 +86 前缀、校验格式
def normalize_phone(phone):
    phone = re.sub(r"\s+", "", str(phone or ""))    # 清洗手机号中的空白字符
    if phone.startswith("+86"):      # startswith()判断字符串是否以指定的内容开头
        phone = phone[3:]
    if not PHONE_PATTERN.match(phone):     # 正则匹配输入手机号，正确返回True
        raise ValidationError("请输入有效的中国大陆手机号")
    return phone


# 验证码发送
def issue_phone_verification_code(phone, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):   # purpose不固定，可以是登录，也可以是注册
    phone = normalize_phone(phone)
    rate_key = f"sms-send:{purpose}:{phone}"
    if not cache.add(rate_key, "1", timeout=PHONE_CODE_RESEND_SECONDS):   # "1"占位，无实际意义，rate_key存在返回False
        raise ValidationError("验证码发送太频繁，请稍后再试")
    try:
        _assert_phone_code_can_send(phone, purpose)
        raw_code = f"{_random.randint(0, 999999):06d}"      # 生成安全随机验证码，:06d不足6位前面补 0
        verification = PhoneVerificationCode.objects.create(
            phone=phone,
            code=_phone_code_digest(phone, purpose, raw_code),
            purpose=purpose,
            expires_at=timezone.now() + timezone.timedelta(minutes=PHONE_CODE_TTL_MINUTES),
            sent_to_backend=send_phone_verification_code(phone, raw_code, purpose),
        )
    except Exception:
        cache.delete(rate_key)
        raise
    # 仅供当前进程的控制台开发与自动化测试使用，不会写入数据库。
    verification._raw_code = raw_code
    return verification


# 发送验证码前的业务校验
def validate_phone_code_request(phone, purpose, *, user=None):
    phone = normalize_phone(phone)
    existing_user = get_user_by_phone(phone)

    if purpose == PhoneVerificationCode.PURPOSE_REGISTER and existing_user:
        raise ValidationError("该手机号已注册，请直接登录")
    if purpose == PhoneVerificationCode.PURPOSE_LOGIN and not existing_user:
        raise ValidationError("该手机号未注册，请先注册")
    if purpose == PhoneVerificationCode.PURPOSE_RESET and not existing_user:
        raise ValidationError("该手机号未注册")
    if purpose == PhoneVerificationCode.PURPOSE_BIND:
        qs = UserProfile.objects.filter(phone=phone)
        if user and getattr(user, "is_authenticated", False):
            qs = qs.exclude(user=user)
        if qs.exists():
            raise ValidationError("该手机号已绑定其他账号")
    return phone


# 验证码发送策略分发函数
def send_phone_verification_code(phone, code, purpose):
    provider = getattr(settings, "SMS_PROVIDER", "console")
    if provider == "console":
        print(f"[ShopLite SMS] phone={phone} purpose={purpose} code={code}")
        return True
    if provider == "tencent":
        return _send_tencent_sms(phone, code)
    raise ValidationError("短信服务未配置")


# 腾讯云短信SDK发送真实短信的函数
def _send_tencent_sms(phone, code):
    # 读取.env参数
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


# 控制台短信发送函数
def _send_console_sms(phone, code, purpose):
    print(f"[ShopLite SMS] phone={phone} purpose={purpose} code={code}")
    return True


# 验证码校验函数
def verify_phone_code(phone, code, *, purpose=PhoneVerificationCode.PURPOSE_LOGIN):
    # 输入清洗与格式校验
    phone = normalize_phone(phone)
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValidationError("请输入 6 位短信验证码")

    # 准备变量
    error = None
    verification = None
    # 事务 + 行锁查询
    with transaction.atomic():
        verification = (
            PhoneVerificationCode.objects.select_for_update()
            .filter(phone=phone, purpose=purpose, used_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if not verification:
            error = "短信验证码不正确或已失效"
        elif verification.is_expired:
            verification.used_at = timezone.now()
            verification.save(update_fields=["used_at"])
            error = "短信验证码已过期"
        elif verification.attempt_count >= PHONE_CODE_MAX_ATTEMPTS:
            verification.used_at = timezone.now()
            verification.save(update_fields=["used_at"])
            error = "验证码尝试次数过多，请重新获取"
        elif not constant_time_compare(
            verification.code,
            _phone_code_digest(phone, purpose, code),
        ):
            verification.attempt_count += 1
            verification.last_attempt_at = timezone.now()
            fields = ["attempt_count", "last_attempt_at"]
            if verification.attempt_count >= PHONE_CODE_MAX_ATTEMPTS:
                verification.used_at = timezone.now()
                fields.append("used_at")
                error = "验证码尝试次数过多，请重新获取"
            else:
                error = "短信验证码不正确"
            verification.save(update_fields=fields)
        else:
            verification.used_at = timezone.now()
            verification.last_attempt_at = verification.used_at
            verification.save(update_fields=["used_at", "last_attempt_at"])
    if error:
        raise ValidationError(error)
    return verification


# 哈希加密函数
def _phone_code_digest(phone, purpose, code):
    value = f"{phone}|{purpose}|{code}"
    # salted_hmac()Django 提供的一个加盐哈希函数，shoplite.phone-verification随便编的
    return salted_hmac("shoplite.phone-verification", value).hexdigest()


# 通过手机号查询用户函数
def get_user_by_phone(phone):
    phone = normalize_phone(phone)
    profile = UserProfile.objects.select_related("user").filter(phone=phone).first()
    if profile:
        return profile.user
    return None


# 通过手机号注册用户函数
@transaction.atomic   # 整个函数在一个数据库事务中执行。如果中间任何一步失败，已写入的数据全部回滚，不会出现"用户建了但 Profile 没建成"的数据不一致情况
def register_user_by_phone(phone, *, password=None):
    phone = normalize_phone(phone)
    # 防止重复注册
    if UserProfile.objects.filter(phone=phone).exists():
        raise ValidationError("该手机号已注册，请直接登录")
    # 检查密码
    if not password:
        raise ValidationError("请设置登录密码")
    # 生成用户名
    username = _build_phone_username(phone)
    # 创建 User 对象并校验密码
    user = User(username=username)    # 创建一个 Python 对象
    validate_password(password, user=user)    # Django 内置的密码强度校验
    user.set_password(password)    # Django 自动对密码做加盐哈希
    # 写入数据库
    try:
        user.save()
        UserProfile.objects.create(user=user, phone=phone, phone_verified_at=timezone.now())
    except IntegrityError as exc:
        raise ValidationError("该手机号已注册，请直接登录") from exc
    return user


# 验证码注册封装
def register_user_with_phone_code(phone, code, password):
    phone = normalize_phone(phone)
    # 防重复注册
    if UserProfile.objects.filter(phone=phone).exists():
        raise ValidationError("该手机号已注册，请直接登录")
    # 提前检验密码强度
    candidate = User(username=_build_phone_username(phone))
    validate_password(password, user=candidate)
    # 校验验证码
    verify_phone_code(phone, code, purpose=PhoneVerificationCode.PURPOSE_REGISTER)
    return register_user_by_phone(phone, password=password)


# 智能登录验证函数
def authenticate_by_login_identifier(request, identifier, password):
    # 清洗输入
    identifier = str(identifier or "").strip()
    if not identifier or not password:
        raise ValidationError("请输入账号和密码")

    username = identifier
    # 手机号查询验证
    try:
        user = get_user_by_phone(identifier)
    except ValidationError:
        user = None
    if user:
        username = user.username
    # 邮箱查询验证
    elif "@" in identifier:
        email_users = User.objects.filter(email__iexact=identifier, is_active=True)
        if email_users.count() == 1:
            username = email_users.first().username

    # django认证，authenticate() 是 Django 内置的函数，在 User 表中验证用户名 + 密码
    user = authenticate(request, username=username, password=password)
    if not user:
        raise ValidationError("账号或密码错误")
    return user


# # 微信登录
# @transaction.atomic
# def get_or_create_user_by_wechat(uid, *, nickname="微信用户", extra_data=None):
#     try:
#         from allauth.socialaccount.models import SocialAccount
#     except ImportError as exc:
#         raise ValidationError("微信登录依赖 django-allauth，请先安装并启用") from exc
#
#     uid = str(uid or "").strip()
#     if not uid:
#         raise ValidationError("微信用户标识不能为空")
#
#     social_account = SocialAccount.objects.select_related("user").filter(provider="weixin", uid=uid).first()
#     if social_account:
#         return social_account.user, False
#
#     username = _build_social_username("wx")
#     user = User.objects.create_user(username=username)
#     if nickname:
#         user.first_name = str(nickname)[:150]
#         user.save(update_fields=["first_name"])
#     SocialAccount.objects.create(
#         user=user,
#         provider="weixin",
#         uid=uid,
#         extra_data=extra_data or {"nickname": nickname},
#     )
#     UserProfile.objects.get_or_create(user=user)
#     return user, True


# # 模拟微信UID
# def mock_wechat_uid():
#     return "mock-wechat-openid"


# 将手机号绑定到已有用户
def bind_phone_to_user(user, phone):
    phone = normalize_phone(phone)
    if UserProfile.objects.filter(phone=phone).exclude(user=user).exists():
        raise ValidationError("该手机号已绑定其他账号")

    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.phone = phone
    profile.phone_verified_at = timezone.now()
    profile.save(update_fields=["phone", "phone_verified_at", "updated_at"])
    return profile


def bind_phone_with_code(user, phone, code):
    validate_phone_code_request(phone, PhoneVerificationCode.PURPOSE_BIND, user=user)
    verify_phone_code(phone, code, purpose=PhoneVerificationCode.PURPOSE_BIND)
    return bind_phone_to_user(user, phone)


def reset_password_with_phone_code(phone, code, new_password):
    user = get_user_by_phone(phone)
    if not user:
        raise ValidationError("该手机号未注册")
    validate_password(new_password, user=user)
    verify_phone_code(phone, code, purpose=PhoneVerificationCode.PURPOSE_RESET)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    return user


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


def calculate_cart_totals(cart_items, *, user_coupon=None):
    subtotal = sum((item.subtotal for item in cart_items), Decimal("0.00"))
    promotion_discount = DISCOUNT_AMOUNT if subtotal >= DISCOUNT_THRESHOLD else Decimal("0.00")
    coupon_discount = calculate_coupon_discount(user_coupon, subtotal)
    discount = min(subtotal, promotion_discount + coupon_discount)
    payable = subtotal - discount + FREE_SHIPPING
    return CartTotals(
        subtotal=subtotal,
        discount=discount,
        promotion_discount=promotion_discount,
        coupon_discount=coupon_discount,
        shipping_fee=FREE_SHIPPING,
        payable=payable,
    )


def calculate_coupon_discount(user_coupon, subtotal):
    if not user_coupon:
        return Decimal("0.00")
    if user_coupon.used_at or user_coupon.coupon.valid_until <= timezone.now():
        raise ValidationError("优惠券已使用或已过期")
    coupon = user_coupon.coupon
    if not coupon.is_active or coupon.valid_from > timezone.now():
        raise ValidationError("优惠券当前不可用")
    if subtotal < coupon.minimum_spend:
        raise ValidationError(f"该优惠券需满 ¥{coupon.minimum_spend} 使用")
    if coupon.discount_type == Coupon.TYPE_PERCENT:
        value = max(Decimal("0"), min(coupon.value, Decimal("100")))
        # value=90 表示九折，即减免原价的 10%。
        return (subtotal * (Decimal("100") - value) / Decimal("100")).quantize(Decimal("0.01"))
    return min(subtotal, max(Decimal("0"), coupon.value))


@transaction.atomic
def claim_coupon(user, coupon_id):
    coupon = Coupon.objects.select_for_update().get(id=coupon_id)
    existing = UserCoupon.objects.filter(user=user, coupon=coupon).first()
    if existing:
        return existing, False
    if not coupon.is_available:
        raise ValidationError("优惠券已领完或不在有效期")
    user_coupon = UserCoupon.objects.create(user=user, coupon=coupon)
    Coupon.objects.filter(id=coupon.id).update(claimed_count=F("claimed_count") + 1)
    Notification.objects.create(
        user=user,
        category=Notification.TYPE_COUPON,
        title="优惠券领取成功",
        content=f"{coupon.name} 已放入您的账户，请在有效期内使用。",
        link="/coupons/",
    )
    return user_coupon, True


def build_address_snapshot(address):
    if not address:
        return ""
    return f"{address.receiver} {address.phone} {address.province}{address.city}{address.district} {address.detail}".strip()


@transaction.atomic
def add_product_to_cart(user, product_id, *, quantity=1, color="", specs=""):
    quantity = parse_quantity(quantity)
    product = Product.objects.select_for_update().get(id=product_id, is_active=True)
    if product.stock < 1:
        raise ValidationError("商品库存不足")
    if quantity > product.stock:
        raise ValidationError(f"库存仅剩 {product.stock} 件")
    variant = _build_variant_text(color, specs)

    item, created = CartItem.objects.get_or_create(
        user=user,
        product=product,
        color=variant,
        defaults={"quantity": min(quantity, product.stock)},
    )
    if not created:
        if item.quantity + quantity > product.stock:
            raise ValidationError(f"购物车已有 {item.quantity} 件，库存仅剩 {product.stock} 件")
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
def create_order_from_cart(user, *, address_id=None, user_coupon_id=None, payment_method=Order.PAYMENT_MOCK):
    cart_items = list(
        CartItem.objects.select_related("product")
        .select_for_update()
        .filter(user=user)
        .order_by("id")
    )
    if not cart_items:
        raise ValidationError("购物车为空")

    address = Address.objects.filter(id=address_id, user=user).first() if address_id else None
    if not address:
        raise ValidationError("请选择有效的收货地址")

    for item in cart_items:
        product = Product.objects.select_for_update().get(id=item.product_id)
        if not product.is_active or product.stock < item.quantity:
            raise ValidationError(f"商品库存不足或已下架：{product.name}")

    if payment_method not in dict(Order.PAYMENT_CHOICES):
        raise ValidationError("支付方式无效")
    if payment_method != Order.PAYMENT_MOCK:
        raise ValidationError("该支付方式暂未开放")

    user_coupon = None
    if user_coupon_id:
        user_coupon = (
            UserCoupon.objects.select_for_update()
            .select_related("coupon")
            .filter(id=user_coupon_id, user=user)
            .first()
        )
        if not user_coupon:
            raise ValidationError("优惠券不存在")

    totals = calculate_cart_totals(cart_items, user_coupon=user_coupon)
    order = Order.objects.create(
        user=user,
        order_no=generate_order_no(),
        total_amount=totals.subtotal,
        discount_amount=totals.discount,
        shipping_fee=totals.shipping_fee,
        pay_amount=totals.payable,
        address_text=build_address_snapshot(address),
        coupon=user_coupon,
        payment_method=payment_method,
        status=Order.STATUS_PENDING,
    )

    OrderItem.objects.bulk_create([_build_order_item(order, item) for item in cart_items])
    if user_coupon:
        user_coupon.used_at = timezone.now()
        user_coupon.save(update_fields=["used_at"])
    CartItem.objects.filter(id__in=[item.id for item in cart_items]).delete()
    Notification.objects.create(
        user=user,
        category=Notification.TYPE_ORDER,
        title="订单创建成功",
        content=f"订单 {order.order_no} 已创建，请尽快完成支付。",
        link=f"/order/{order.id}/",
    )
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
def mark_order_paid(order, *, paid_at=None, payment_no="", provider=Order.PAYMENT_MOCK, raw_payload=None):
    if provider not in dict(Order.PAYMENT_CHOICES):
        raise ValidationError("支付渠道无效")
    locked_order = (
        Order.objects.select_for_update()
        .prefetch_related("items__product")
        .get(id=order.id)
    )

    if locked_order.status in {Order.STATUS_PAID, Order.STATUS_SHIPPED, Order.STATUS_COMPLETED}:
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

    payment_no = str(payment_no or generate_payment_no(provider))[:64]
    existing_payment = PaymentTransaction.objects.filter(transaction_no=payment_no).first()
    if existing_payment and existing_payment.order_id != locked_order.id:
        raise ValidationError("支付流水号已被其他订单使用")

    locked_order.status = Order.STATUS_PAID
    locked_order.paid_at = paid_at or timezone.now()
    locked_order.payment_method = provider
    locked_order.payment_no = payment_no
    locked_order.save(update_fields=["status", "paid_at", "payment_method", "payment_no"])
    PaymentTransaction.objects.update_or_create(
        transaction_no=payment_no,
        defaults={
            "order": locked_order,
            "provider": provider,
            "amount": locked_order.pay_amount,
            "status": PaymentTransaction.STATUS_SUCCESS,
            "raw_payload": raw_payload or {},
            "completed_at": locked_order.paid_at,
        },
    )
    Notification.objects.create(
        user=locked_order.user,
        category=Notification.TYPE_ORDER,
        title="支付成功",
        content=f"订单 {locked_order.order_no} 支付成功，商家将尽快发货。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


def generate_payment_no(provider):
    return f"{provider.upper()}{timezone.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:10].upper()}"


@transaction.atomic
def cancel_pending_order(order):
    locked_order = Order.objects.select_for_update().select_related("coupon").get(id=order.id)
    if locked_order.status != Order.STATUS_PENDING:
        return locked_order, False
    locked_order.status = Order.STATUS_CANCELLED
    locked_order.save(update_fields=["status"])
    if locked_order.coupon_id and locked_order.coupon.used_at:
        locked_order.coupon.used_at = None
        locked_order.coupon.save(update_fields=["used_at"])
    Notification.objects.create(
        user=locked_order.user,
        category=Notification.TYPE_ORDER,
        title="订单已取消",
        content=f"订单 {locked_order.order_no} 已取消。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def mark_order_shipped(order):
    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status != Order.STATUS_PAID:
        return locked_order, False
    locked_order.status = Order.STATUS_SHIPPED
    locked_order.save(update_fields=["status"])
    Notification.objects.create(
        user=locked_order.user,
        category=Notification.TYPE_ORDER,
        title="订单已发货",
        content=f"订单 {locked_order.order_no} 已发货，请留意物流信息。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def complete_order(order):
    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status != Order.STATUS_SHIPPED:
        return locked_order, False
    locked_order.status = Order.STATUS_COMPLETED
    locked_order.save(update_fields=["status"])
    Notification.objects.create(
        user=locked_order.user,
        category=Notification.TYPE_ORDER,
        title="订单已完成",
        content=f"订单 {locked_order.order_no} 已确认收货。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def create_refund_request(user, order, *, reason, description=""):
    locked_order = Order.objects.select_for_update().get(id=order.id, user=user)
    if locked_order.status not in {Order.STATUS_PAID, Order.STATUS_SHIPPED, Order.STATUS_COMPLETED}:
        raise ValidationError("当前订单状态不能申请退款")
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError("请选择退款原因")
    refund, created = RefundRequest.objects.get_or_create(
        order=locked_order,
        defaults={
            "user": user,
            "refund_no": f"AS{timezone.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}",
            "reason": reason[:100],
            "description": str(description or "").strip(),
            "amount": locked_order.pay_amount,
            "order_status_before": locked_order.status,
        },
    )
    if not created:
        raise ValidationError("该订单已提交过售后申请")
    locked_order.status = Order.STATUS_REFUND
    locked_order.save(update_fields=["status"])
    Notification.objects.create(
        user=user,
        category=Notification.TYPE_ORDER,
        title="售后申请已提交",
        content=f"订单 {locked_order.order_no} 的售后申请正在审核。",
        link="/refunds/",
    )
    return refund


@transaction.atomic
def complete_refund(refund):
    locked_refund = RefundRequest.objects.select_for_update().select_related("order").get(id=refund.id)
    if locked_refund.status == RefundRequest.STATUS_COMPLETED:
        return locked_refund, False
    if locked_refund.status not in {RefundRequest.STATUS_PENDING, RefundRequest.STATUS_APPROVED}:
        raise ValidationError("当前售后状态不能完成退款")
    for item in locked_refund.order.items.select_related("product"):
        if item.product_id:
            Product.objects.filter(id=item.product_id).update(
                stock=F("stock") + item.quantity,
                sales=Greatest(F("sales") - item.quantity, 0),
            )
    locked_refund.status = RefundRequest.STATUS_COMPLETED
    locked_refund.completed_at = timezone.now()
    locked_refund.save(update_fields=["status", "completed_at", "updated_at"])
    Notification.objects.create(
        user=locked_refund.user,
        category=Notification.TYPE_ORDER,
        title="退款已完成",
        content=f"售后单 {locked_refund.refund_no} 已完成退款。",
        link="/refunds/",
    )
    return locked_refund, True
