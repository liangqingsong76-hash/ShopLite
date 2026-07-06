from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Order
from .services import cancel_pending_order


@shared_task
def cancel_expired_pending_orders(minutes=30):
    deadline = timezone.now() - timedelta(minutes=minutes)
    order_ids = list(
        Order.objects.filter(status=Order.STATUS_PENDING, created_at__lt=deadline).values_list("id", flat=True)
    )
    cancelled = 0
    for order_id in order_ids:
        order = Order.objects.get(id=order_id)
        _, changed = cancel_pending_order(order)
        if changed:
            cancelled += 1
    return cancelled
