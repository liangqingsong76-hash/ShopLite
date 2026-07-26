"""交易领域的异步任务。

上游：Celery Beat 按 settings 中的计划调用。
下游：任务只调用 commerce 服务取消超时订单，避免直接篡改订单状态。
"""

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Order
from .services import cancel_pending_order


@shared_task
def cancel_expired_pending_orders(minutes=30):
    """取消创建超过指定分钟数的待付款订单，并返回实际取消数量。"""

    deadline = timezone.now() - timedelta(minutes=minutes)
    order_ids = list(
        Order.objects.filter(status=Order.STATUS_PENDING, created_at__lt=deadline).values_list("id", flat=True)
    )
    cancelled = 0
    for order_id in order_ids:
        # 列表查询与逐单锁定之间订单可能已被并发操作删除或支付；缺失记录无需重试，
        # 其余订单仍继续通过统一取消服务释放预占库存。
        order = Order.objects.filter(id=order_id).first()
        if not order:
            continue
        _, changed = cancel_pending_order(order)
        cancelled += int(changed)
    return cancelled
