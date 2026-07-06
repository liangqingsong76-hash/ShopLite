import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Address, CartItem, Order, OrderItem, Product

FREE_SHIPPING = Decimal("0.00")
DISCOUNT_THRESHOLD = Decimal("299.00")
DISCOUNT_AMOUNT = Decimal("30.00")
MIN_QUANTITY = 1
MAX_QUANTITY = 99


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
