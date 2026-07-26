"""支付成功后的订单状态、库存和流水编排服务。

上游：模拟支付页和未来真实支付回调验证器。
下游：锁定 commerce.Order、更新 catalog.Product、写 PaymentTransaction 与 Notification。
禁止：页面视图或网关适配器不得自行扣库存。
"""

import logging
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from commerce.models import Order
from commerce.services import _lock_products, _quantities_by_product
from notifications.services import create_notification

from .models import PaymentTransaction


logger = logging.getLogger(__name__)


def generate_payment_no(provider):
    """生成渠道无关的本地支付流水号。"""

    return f"{provider.upper()}{timezone.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:10].upper()}"


def _assert_matching_payment_record(*, payment, order, provider, status):
    """验证已存在的流水仍属于当前订单和同一笔渠道交易。

    支付流水是审计事实而不是可覆盖的缓存。特别是不能用第二张订单的回调去更新已经
    写入的 ``order``、金额或原始回调摘要，否则唯一交易号会被并发请求悄悄改绑。
    """

    if payment.order_id != order.id:
        raise ValidationError("支付流水号已被其他订单使用")
    if payment.provider != provider:
        raise ValidationError("支付流水号的支付渠道与本次回调不一致")
    if payment.amount != order.pay_amount:
        raise ValidationError("支付流水号的支付金额与订单不一致")
    if payment.status != status:
        raise ValidationError("支付流水号的审计状态与当前订单状态冲突")


def _record_payment_audit(*, order, payment_no, provider, raw_payload, completed_at, status):
    """仅创建或验证渠道流水，绝不更新已经存在的流水。

    ``transaction_no`` 是全局唯一的渠道幂等键。MySQL 默认的 ``READ COMMITTED`` 下，
    两个事务都可能在“先查流水”时读到空值；因此这里以直接 ``create`` 作为唯一事实
    写入，使用内层保存点回滚竞争失败的插入，再锁定赢家记录并逐项比较归属。不能使用
    ``update_or_create``：其内部会吞掉唯一键异常并把赢家记录按 defaults 覆盖。
    """

    existing_payment = (
        PaymentTransaction.objects.select_for_update().filter(transaction_no=payment_no).first()
    )
    if existing_payment:
        _assert_matching_payment_record(
            payment=existing_payment,
            order=order,
            provider=provider,
            status=status,
        )
        return existing_payment, False

    try:
        with transaction.atomic():
            payment = PaymentTransaction.objects.create(
                order=order,
                provider=provider,
                transaction_no=payment_no,
                amount=order.pay_amount,
                status=status,
                raw_payload=raw_payload or {},
                completed_at=completed_at,
            )
    except IntegrityError:
        # 仅内层保存点回滚；外层订单事务仍可读取并比较并发插入的赢家。
        existing_payment = (
            PaymentTransaction.objects.select_for_update().filter(transaction_no=payment_no).first()
        )
        if existing_payment is None:
            # 不是本唯一键竞争导致的 IntegrityError，交由 Django 保留原始数据库错误。
            raise
        _assert_matching_payment_record(
            payment=existing_payment,
            order=order,
            provider=provider,
            status=status,
        )
        return existing_payment, False
    return payment, True


def _record_successful_payment(*, order, payment_no, provider, raw_payload, completed_at):
    """创建或验证一笔可推进订单状态的成功支付流水。"""

    return _record_payment_audit(
        order=order,
        payment_no=payment_no,
        provider=provider,
        raw_payload=raw_payload,
        completed_at=completed_at,
        status=PaymentTransaction.STATUS_SUCCESS,
    )


def _record_reconciliation_payment(*, order, payment_no, provider, raw_payload, completed_at):
    """审计取消订单的迟到成功支付，且不改变订单、库存或销量。"""

    return _record_payment_audit(
        order=order,
        payment_no=payment_no,
        provider=provider,
        raw_payload=raw_payload,
        completed_at=completed_at,
        status=PaymentTransaction.STATUS_RECONCILIATION,
    )


def _log_late_payment_reconciliation(*, order_id, payment_id, provider):
    """提交事务后记录不含网关载荷和用户隐私的结构化高优先级告警。"""

    logger.critical(
        "late_successful_payment_requires_reconciliation",
        extra={
            "event": "late_successful_payment_requires_reconciliation",
            "order_id": order_id,
            "payment_transaction_id": payment_id,
            "payment_provider": provider,
        },
    )


def _validate_paid_order_idempotency(order, *, submitted_payment_no, provider):
    """拒绝已付款订单收到不同渠道交易号或渠道的“成功”回调。"""

    if not submitted_payment_no:
        # 没有渠道交易号的内部重复调用只能查询状态，不能覆盖既有支付身份。
        return
    if not order.payment_no or submitted_payment_no != order.payment_no:
        raise ValidationError("订单已付款，但本次支付流水号与已记录流水号不一致")
    if provider != order.payment_method:
        raise ValidationError("订单已付款，但本次支付渠道与已记录渠道不一致")
    payment = (
        PaymentTransaction.objects.select_for_update()
        .filter(transaction_no=submitted_payment_no)
        .first()
    )
    if payment:
        _assert_matching_payment_record(
            payment=payment,
            order=order,
            provider=provider,
            status=PaymentTransaction.STATUS_SUCCESS,
        )


@transaction.atomic
def mark_order_paid(order, *, paid_at=None, payment_no="", provider=Order.PAYMENT_MOCK, raw_payload=None):
    """幂等确认待付款订单的支付成功。

    新订单在创建待付款订单时已经预占 ``Product.stock``，所以本函数只将预占最终计入
    销量；``stock_reserved=False`` 的上线前旧订单保持一次性扣库存的兼容路径。商品行锁
    复用 commerce 的固定 ``product_id`` 顺序，避免多商品订单在支付回调中发生死锁。

    副作用：必要时原子扣库存、增加销量、更新订单、创建支付流水和订单通知。
    """

    if provider not in dict(Order.PAYMENT_CHOICES):
        raise ValidationError("支付渠道无效")
    submitted_payment_no = str(payment_no or "").strip()[:64]
    effective_payment_no = submitted_payment_no or generate_payment_no(provider)
    completed_at = paid_at or timezone.now()
    locked_order = Order.objects.select_for_update().get(id=order.id)
    if locked_order.status in {Order.STATUS_PAID, Order.STATUS_SHIPPED, Order.STATUS_COMPLETED}:
        _validate_paid_order_idempotency(
            locked_order,
            submitted_payment_no=submitted_payment_no,
            provider=provider,
        )
        return locked_order, False
    if provider != locked_order.payment_method:
        raise ValidationError("支付渠道与订单支付方式不一致")
    if locked_order.status == Order.STATUS_CANCELLED:
        # 网关可能在本地取消/关单后才送达已经扣款的成功通知。保留完整、不可变的审计
        # 记录，并让后台显式人工对账；绝不复活订单、发货、重新扣库存或增加销量。
        if not submitted_payment_no:
            raise ValidationError("取消订单的迟到支付通知必须提供渠道流水号")
        reconciliation_payment, payment_created = _record_reconciliation_payment(
            order=locked_order,
            payment_no=effective_payment_no,
            provider=provider,
            raw_payload=raw_payload,
            completed_at=completed_at,
        )
        if not locked_order.payment_reconciliation_required:
            locked_order.payment_reconciliation_required = True
            locked_order.save(update_fields=["payment_reconciliation_required"])
        if payment_created:
            create_notification(
                user=locked_order.user,
                category="order",
                title="订单已取消但收到付款",
                content=(
                    f"订单 {locked_order.order_no} 取消后收到支付成功通知，"
                    "平台已转人工核对，请勿重复付款并留意后续处理结果。"
                ),
                link=f"/order/{locked_order.id}/",
            )
            # 日志属于事务外部副作用，只能在流水、订单标记和通知全部提交后发送。
            transaction.on_commit(
                lambda order_id=locked_order.id,
                payment_id=reconciliation_payment.id,
                payment_provider=provider: _log_late_payment_reconciliation(
                    order_id=order_id,
                    payment_id=payment_id,
                    provider=payment_provider,
                )
            )
        return locked_order, False
    if locked_order.status != Order.STATUS_PENDING:
        raise ValidationError("只有待付款订单可以确认支付")

    items = list(locked_order.items.select_related("product").order_by("product_id", "id"))
    quantities = _quantities_by_product(items)
    products_by_id = _lock_products(quantities)
    if not locked_order.stock_reserved:
        # 兼容添加库存预占字段前创建的历史待付款订单：它们没有在下单时扣减库存，
        # 因此仍须在支付确认这一刻执行一次完整的可售库存检查和扣减。
        for product_id in sorted(quantities):
            product = products_by_id.get(product_id)
            if not product or product.stock < quantities[product_id]:
                product_name = product.name if product else "商品"
                raise ValidationError(f"商品库存不足：{product_name}")
        for product_id in sorted(quantities):
            product = products_by_id[product_id]
            product.stock -= quantities[product_id]
            product.save(update_fields=["stock", "updated_at"])
        locked_order.stock_reserved = True
    # 写流水发生在订单状态变更前。同一流水号若已被另一订单抢占，外层事务会回滚
    # 本订单此前的旧订单扣库存，避免两个订单都变成已付款。
    _record_successful_payment(
        order=locked_order,
        payment_no=effective_payment_no,
        provider=provider,
        raw_payload=raw_payload,
        completed_at=completed_at,
    )
    # 无论库存是在下单预占还是这里为旧订单扣减，支付成功都只在此处增加一次销量。
    for product_id in sorted(quantities):
        product = products_by_id.get(product_id)
        if not product:
            continue
        product.sales += quantities[product_id]
        product.save(update_fields=["sales", "updated_at"])
    locked_order.status = Order.STATUS_PAID
    locked_order.paid_at = completed_at
    locked_order.payment_method = provider
    locked_order.payment_no = effective_payment_no
    locked_order.save(
        update_fields=["status", "paid_at", "payment_method", "payment_no", "stock_reserved"]
    )
    create_notification(
        user=locked_order.user,
        category="order",
        title="支付成功",
        content=f"订单 {locked_order.order_no} 支付成功，商家将尽快发货。",
        link=f"/order/{locked_order.id}/",
    )
    return locked_order, True
