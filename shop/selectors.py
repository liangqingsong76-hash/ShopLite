"""storefront 的只读查询兼容层。

依赖流向：模板视图 -> 本模块 -> catalog/commerce/notifications 查询边界。
领域选择器不反向依赖 storefront，避免页面上下文污染核心查询规则。
"""

from django.conf import settings

from catalog.selectors import (
    active_categories,
    active_product_queryset,
    list_products,
    popular_brands,
    product_detail,
    product_queryset,
    product_spec_context,
    review_stats,
)
from commerce.selectors import cart_count, cart_queryset, cart_subtotal, favorite_count, order_stats
from notifications.models import Notification


def base_context(request):
    """构造所有商城页面共享的购物车与通知摘要。"""

    cart_items = []
    cart_total = 0
    unread_notifications = 0
    recent_notifications = []

    if request.user.is_authenticated:
        # 页面只预览三项，但金额必须统计整个购物车，不能因切片而少算。
        cart_items = list(cart_queryset(request.user)[:3])
        cart_total = cart_subtotal(request.user)
        unread_notifications = Notification.objects.filter(
            user=request.user,
            is_read=False,
        ).count()
        recent_notifications = list(Notification.objects.filter(user=request.user)[:3])

    return {
        "cart_count": cart_count(request.user),
        "cart_items": cart_items,
        "cart_total": cart_total,
        "unread_notifications": unread_notifications,
        "recent_notifications": recent_notifications,
        "mock_payment_enabled": settings.DEBUG or settings.ENABLE_MOCK_PAYMENT,
    }


__all__ = (
    "active_categories",
    "active_product_queryset",
    "base_context",
    "cart_count",
    "cart_queryset",
    "cart_subtotal",
    "favorite_count",
    "list_products",
    "order_stats",
    "popular_brands",
    "product_detail",
    "product_queryset",
    "product_spec_context",
    "review_stats",
)
