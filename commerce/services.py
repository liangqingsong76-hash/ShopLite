"""购物车、优惠券、订单与售后写入服务。

上游：storefront 页面/API、管理后台、Celery 任务。
下游：本模块在事务中读写 commerce/catalog 模型，并通过 notifications 服务产生站内通知。
禁止：不要在视图中绕过本模块直接修改订单、库存或优惠券状态。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from catalog.models import Product
from notifications.services import create_notification

from .models import Address, CartItem, Coupon, Order, OrderItem, RefundRequest, UserCoupon

FREE_SHIPPING = Decimal("0.00")
DISCOUNT_THRESHOLD = Decimal("299.00")
DISCOUNT_AMOUNT = Decimal("30.00")
MIN_QUANTITY = 1
MAX_QUANTITY = 99


@dataclass(frozen=True)
class CartTotals:
    """结算页使用的金额快照，所有金额均为 ``Decimal``。"""

    subtotal: Decimal
    discount: Decimal
    promotion_discount: Decimal
    coupon_discount: Decimal
    shipping_fee: Decimal
    payable: Decimal


def available_checkout_payment_methods():
    """返回当前环境实际可创建订单的支付方式。

    支付方式枚举包含未来支付宝和微信的预留值，但在相应网关、验签和对账能力接入前，
    它们不能被视为可用渠道。模拟支付也只能在开发环境或显式本地测试开关开启时使用；
    因此生产环境没有任何真实渠道时必须拒绝创建订单，不能预占库存后让用户无法付款。
    """

    if settings.DEBUG or getattr(settings, "ENABLE_MOCK_PAYMENT", False):
        return (Order.PAYMENT_MOCK,)
    return ()


def parse_quantity(value, *, default=1):
    """将外部数量限制到安全的 1 至 99 区间。"""

    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(MIN_QUANTITY, min(quantity, MAX_QUANTITY))


def parse_decimal(value):
    """把查询参数安全转换为 ``Decimal``，无效时返回 ``None``。"""

    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # ``Decimal('NaN')`` 和 ``Decimal('Infinity')`` 能被构造，却无法安全参与 ORM
    # 金额比较；必须在请求边界拒绝，避免分类价格筛选触发数据库/decimal 500。
    return parsed if parsed.is_finite() else None


def calculate_cart_totals(cart_items, *, user_coupon=None):
    """计算商品小计、平台满减、优惠券、运费和应付金额。"""

    subtotal = sum((item.subtotal for item in cart_items), Decimal("0.00"))
    promotion_discount = DISCOUNT_AMOUNT if subtotal >= DISCOUNT_THRESHOLD else Decimal("0.00")
    coupon_discount = calculate_coupon_discount(user_coupon, subtotal)
    discount = min(subtotal, promotion_discount + coupon_discount)
    return CartTotals(
        subtotal=subtotal,
        discount=discount,
        promotion_discount=promotion_discount,
        coupon_discount=coupon_discount,
        shipping_fee=FREE_SHIPPING,
        payable=subtotal - discount + FREE_SHIPPING,
    )


def calculate_coupon_discount(user_coupon, subtotal):
    """校验指定用户券并计算其可抵扣金额。"""

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
        return (subtotal * (Decimal("100") - value) / Decimal("100")).quantize(Decimal("0.01"))
    return min(subtotal, max(Decimal("0"), coupon.value))


@transaction.atomic
def claim_coupon(user, coupon_id):
    """原子领取优惠券，并创建一条优惠券通知。

    幂等：同一用户重复领取同一券时返回已有实例和 ``False``。
    """

    coupon = Coupon.objects.select_for_update().get(id=coupon_id)
    existing = UserCoupon.objects.filter(user=user, coupon=coupon).first()
    if existing:
        return existing, False
    if not coupon.is_available:
        raise ValidationError("优惠券已领完或不在有效期")
    user_coupon = UserCoupon.objects.create(user=user, coupon=coupon)
    Coupon.objects.filter(id=coupon.id).update(claimed_count=F("claimed_count") + 1)
    create_notification(
        user=user,
        category="coupon",
        title="优惠券领取成功",
        content=f"{coupon.name} 已放入您的账户，请在有效期内使用。",
        link="/coupons/",
    )
    return user_coupon, True


def build_address_snapshot(address):
    """将地址对象序列化为订单不可变文本快照。"""

    if not address:
        return ""
    return f"{address.receiver} {address.phone} {address.province}{address.city}{address.district} {address.detail}".strip()


@transaction.atomic
def add_product_to_cart(user, product_id, *, quantity=1, color="", specs=""):
    """锁定商品库存后新增或累加购物车变体。"""

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
        defaults={"quantity": quantity},
    )
    if not created:
        if item.quantity + quantity > product.stock:
            raise ValidationError(f"购物车已有 {item.quantity} 件，库存仅剩 {product.stock} 件")
        item.quantity = min(item.quantity + quantity, MAX_QUANTITY)
        item.save(update_fields=["quantity", "updated_at"])
    return item


def _build_variant_text(color, specs):
    """生成用于购物车唯一约束的颜色/规格文本。"""

    color = (color or "").strip()
    specs = (specs or "").strip()
    if color and specs:
        return f"{color} | {specs}"
    return color or specs


def _quantities_by_product(items):
    """汇总订单/购物车项的商品数量，供库存检查和按固定顺序加锁使用。

    一个商品可以因颜色或规格不同出现在多条购物车项中；库存必须按商品汇总后检查，
    不能逐项分别比较。返回字典按调用方需要再交给 :func:`_lock_products` 排序锁定。
    """

    quantities = {}
    for item in items:
        if item.product_id:
            quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    return quantities


def _lock_products(product_ids):
    """按商品主键升序获取排他锁，作为所有多商品库存操作的统一锁顺序。

    结算、支付确认和取消订单都通过此函数取得 ``Product`` 锁，避免两个订单包含相同
    商品但顺序不同而形成数据库死锁。返回值以商品主键为键，便于调用方按快照项回填。
    """

    normalized_ids = sorted({int(product_id) for product_id in product_ids if product_id})
    if not normalized_ids:
        return {}
    locked_products = Product.objects.select_for_update().filter(id__in=normalized_ids).order_by("id")
    return {product.id: product for product in locked_products}


def _reserve_products(products_by_id, quantities):
    """验证并扣减可售库存，表示待付款订单已成功预占商品。

    调用方必须已通过 :func:`_lock_products` 以固定顺序锁住全部商品，且处于同一事务。
    任一商品下架、缺失或可售库存不足时抛出异常，外层事务会撤销此前已做的预占。
    """

    for product_id in sorted(quantities):
        product = products_by_id.get(product_id)
        quantity = quantities[product_id]
        if not product or not product.is_active or product.stock < quantity:
            product_name = product.name if product else "商品"
            raise ValidationError(f"商品库存不足或已下架：{product_name}")
    for product_id in sorted(quantities):
        product = products_by_id[product_id]
        product.stock -= quantities[product_id]
        product.save(update_fields=["stock", "updated_at"])


def _release_reserved_products(items):
    """归还仍关联商品的待付款预占库存，并保持统一的商品行锁顺序。

    商品在订单创建后可能被管理员删除，届时订单项的 ``product`` 会变为 ``NULL``；该类
    历史快照没有可归还的商品行，因此安全跳过。调用方须在订单状态锁已持有的事务内调用。
    """

    quantities = _quantities_by_product(items)
    products_by_id = _lock_products(quantities)
    for product_id in sorted(quantities):
        product = products_by_id.get(product_id)
        if not product:
            continue
        product.stock += quantities[product_id]
        product.save(update_fields=["stock", "updated_at"])


@transaction.atomic
def create_order_from_cart(user, *, address_id=None, user_coupon_id=None, payment_method=Order.PAYMENT_MOCK):
    """从用户购物车创建待付款订单并清空已下单项。

    副作用：先按商品主键顺序锁定并预占可售库存，再创建订单与快照、标记用户券、
    删除本次锁定的购物车项并创建通知。预占成功后 ``Order.stock_reserved=True``；
    取消或超时会归还库存，支付成功只把该预占最终计入销量。
    """

    if payment_method not in dict(Order.PAYMENT_CHOICES):
        raise ValidationError("支付方式无效")
    available_payment_methods = available_checkout_payment_methods()
    if not available_payment_methods:
        raise ValidationError("当前没有可用支付渠道，暂不能创建订单")
    if payment_method not in available_payment_methods:
        raise ValidationError("该支付方式当前不可用")

    # 先取本次结算的稳定购物车 ID，再按商品主键锁库存。这样与加购流程保持“商品锁
    # -> 购物车锁”的顺序，避免结算和加购并发时互相等待。结算期间新加入的购物车项
    # 自然留到下一次结算，符合用户预期。
    cart_item_ids = list(
        CartItem.objects.filter(user=user).order_by("product_id", "id").values_list("id", flat=True)
    )
    if not cart_item_ids:
        raise ValidationError("购物车为空")
    cart_product_ids = list(
        CartItem.objects.filter(id__in=cart_item_ids, user=user).values_list("product_id", flat=True)
    )
    products_by_id = _lock_products(cart_product_ids)
    cart_items = list(
        CartItem.objects.select_related("product")
        .select_for_update()
        .filter(id__in=cart_item_ids, user=user)
        .order_by("product_id", "id")
    )
    if not cart_items:
        raise ValidationError("购物车为空")
    address = Address.objects.filter(id=address_id, user=user).first() if address_id else None
    if not address:
        raise ValidationError("请选择有效的收货地址")
    quantities = _quantities_by_product(cart_items)
    if set(quantities) - set(products_by_id):
        raise ValidationError("商品不存在或已下架")
    _reserve_products(products_by_id, quantities)
    # 用已锁定的最新商品数据生成订单快照，避免使用下单前的 select_related 旧值。
    for item in cart_items:
        item.product = products_by_id[item.product_id]
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
        stock_reserved=True,
        status=Order.STATUS_PENDING,
    )
    OrderItem.objects.bulk_create([_build_order_item(order, item) for item in cart_items])
    if user_coupon:
        user_coupon.used_at = timezone.now()
        user_coupon.save(update_fields=["used_at"])
    CartItem.objects.filter(id__in=[item.id for item in cart_items]).delete()
    create_notification(
        user=user,
        category="order",
        title="订单创建成功",
        content=f"订单 {order.order_no} 已创建，请尽快完成支付。",
        link=f"/order/{order.id}/",
    )
    return order


def _build_order_item(order, cart_item):
    """从购物车项构建包含商品名称、图片和价格快照的订单明细。"""

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
    """生成当前项目使用的唯一展示订单号。"""

    return timezone.now().strftime("SL%Y%m%d%H%M%S") + uuid.uuid4().hex[:8].upper()


@transaction.atomic
def cancel_pending_order(order):
    """取消待付款订单，按订单、商品、用户券的固定锁顺序回补资源。

    订单锁查询不得 ``select_related("coupon")``，否则部分数据库会同时锁住关联用户券，
    与结算流程的商品后用户券顺序相反而增加死锁风险。重复调用安全返回且不会重复加库存。
    """

    # 锁顺序 1/3：只锁订单行，禁止通过 JOIN 提前锁住用户优惠券。
    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status != Order.STATUS_PENDING:
        return locked_order, False
    if locked_order.stock_reserved:
        # 锁顺序 2/3：订单项先按商品主键汇总，内部按主键升序取得 Product 锁。
        _release_reserved_products(list(locked_order.items.exclude(product_id=None).order_by("product_id", "id")))
        locked_order.stock_reserved = False
    # 锁顺序 3/3：商品库存全部回补后，最后锁定并释放用户优惠券。
    locked_coupon = None
    if locked_order.coupon_id:
        locked_coupon = UserCoupon.objects.select_for_update().get(id=locked_order.coupon_id)
    locked_order.status = Order.STATUS_CANCELLED
    locked_order.save(update_fields=["status", "stock_reserved"])
    if locked_coupon and locked_coupon.used_at:
        locked_coupon.used_at = None
        locked_coupon.save(update_fields=["used_at"])
    create_notification(
        user=locked_order.user,
        category="order",
        title="订单已取消",
        content=f"订单 {locked_order.order_no} 已取消。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def mark_order_shipped(order):
    """由后台将已付款订单推进为待收货状态。"""

    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status != Order.STATUS_PAID:
        return locked_order, False
    locked_order.status = Order.STATUS_SHIPPED
    locked_order.save(update_fields=["status"])
    create_notification(
        user=locked_order.user,
        category="order",
        title="订单已发货",
        content=f"订单 {locked_order.order_no} 已发货，请留意物流信息。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def complete_order(order):
    """由用户确认收货，将待收货订单推进为已完成。"""

    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status != Order.STATUS_SHIPPED:
        return locked_order, False
    locked_order.status = Order.STATUS_COMPLETED
    locked_order.save(update_fields=["status"])
    create_notification(
        user=locked_order.user,
        category="order",
        title="订单已完成",
        content=f"订单 {locked_order.order_no} 已确认收货。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True


@transaction.atomic
def create_refund_request(user, order, *, reason, description=""):
    """为已支付/发货/完成订单创建唯一售后申请。"""

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
    create_notification(
        user=user,
        category="order",
        title="售后申请已提交",
        content=f"订单 {locked_order.order_no} 的售后申请正在审核。",
        link="/refunds/",
    )
    return refund


REFUND_RESTORABLE_ORDER_STATUSES = frozenset(
    {
        Order.STATUS_PAID,
        Order.STATUS_SHIPPED,
        Order.STATUS_COMPLETED,
    }
)


def _lock_refund_and_order(refund):
    """按 ``Order -> RefundRequest`` 的固定顺序取得售后审核所需的行锁。

    创建售后申请已经先锁订单再创建退款单；后台审核和未来渠道退款回调必须复用同一
    顺序，避免订单状态流转与退款审核并发时形成死锁。退款单的订单归属不可变，因此
    先以最小查询读取 ``order_id``，再在当前事务内重新锁定订单和退款单。
    """

    refund_id = getattr(refund, "id", None)
    if not refund_id:
        raise RefundRequest.DoesNotExist("退款申请不存在")
    refund_reference = RefundRequest.objects.only("order_id").get(id=refund_id)
    locked_order = Order.objects.select_for_update().get(id=refund_reference.order_id)
    locked_refund = RefundRequest.objects.select_for_update().get(id=refund_id)
    return locked_refund, locked_order


@transaction.atomic
def approve_refund_request(refund):
    """原子同意一笔待审核售后申请，保留订单在售后状态等待真实渠道退款。

    返回 ``(refund, changed)``。非待审核、订单不在售后或历史原状态损坏时返回
    ``changed=False``，不覆盖其他管理员已经提交的审核结论。
    """

    locked_refund, locked_order = _lock_refund_and_order(refund)
    if (
        locked_refund.status != RefundRequest.STATUS_PENDING
        or locked_order.status != Order.STATUS_REFUND
        or locked_refund.order_status_before not in REFUND_RESTORABLE_ORDER_STATUSES
    ):
        return locked_refund, False
    locked_refund.status = RefundRequest.STATUS_APPROVED
    locked_refund.save(update_fields=["status", "updated_at"])
    return locked_refund, True


@transaction.atomic
def reject_refund_request(refund):
    """原子拒绝一笔待审核售后申请，并恢复订单发起售后前的有效状态。

    订单恢复与退款状态写入同处一个事务；任一步失败都会回滚，避免留下“退款已拒绝、
    订单仍售后中”的半完成状态。
    """

    locked_refund, locked_order = _lock_refund_and_order(refund)
    if (
        locked_refund.status != RefundRequest.STATUS_PENDING
        or locked_order.status != Order.STATUS_REFUND
        or locked_refund.order_status_before not in REFUND_RESTORABLE_ORDER_STATUSES
    ):
        return locked_refund, False
    locked_order.status = locked_refund.order_status_before
    locked_order.save(update_fields=["status"])
    locked_refund.status = RefundRequest.STATUS_REJECTED
    locked_refund.save(update_fields=["status", "updated_at"])
    return locked_refund, True


@transaction.atomic
def complete_refund(refund):
    """安全阻止人工完成退款，等待未来已验证的真实支付渠道退款回调。

    TODO（支付）：接入支付宝/微信的退款申请、验签回调、渠道退款流水和对账后，由专用
    网关服务在确认资金原路退回后原子调用库存回补逻辑。当前项目没有真实退款渠道，
    因此绝不能仅凭后台点击就把售后单标记为完成或归还库存。
    """

    locked_refund, _locked_order = _lock_refund_and_order(refund)
    if locked_refund.status == RefundRequest.STATUS_COMPLETED:
        return locked_refund, False
    if locked_refund.status != RefundRequest.STATUS_APPROVED:
        raise ValidationError("当前售后状态不能完成退款")
    raise ValidationError("TODO（支付）：真实退款渠道及验签回调尚未接入，售后单会保持“已同意”。")
