"""交易领域的只读查询函数。

上游：storefront 视图和模板上下文调用本模块。
下游：仅读取 commerce/catelog 模型，不产生订单、扣库存等副作用。
"""

from decimal import Decimal

from .models import CartItem, Favorite, Order


def cart_queryset(user):
    """返回用户按最近修改排序的购物车查询集。"""

    return CartItem.objects.select_related("product").filter(user=user).order_by("-updated_at", "-id")


def cart_count(user):
    """返回已登录用户的购物车条目数，匿名用户返回零。"""

    if not user.is_authenticated:
        return 0
    return CartItem.objects.filter(user=user).count()


def cart_subtotal(user):
    """返回购物车全部条目的商品小计，不受顶部预览条目数量限制。"""

    if not user.is_authenticated:
        return Decimal("0")
    rows = CartItem.objects.filter(user=user).values_list("quantity", "product__price")
    return sum((quantity * price for quantity, price in rows), Decimal("0"))


def order_stats(user):
    """按订单状态聚合个人中心所需的订单数量。"""

    stats = {"all": 0, **{status: 0 for status, _ in Order.STATUS_CHOICES}}
    if not user.is_authenticated:
        return stats
    orders = Order.objects.filter(user=user)
    stats["all"] = orders.count()
    for status, _ in Order.STATUS_CHOICES:
        stats[status] = orders.filter(status=status).count()
    return stats


def favorite_count(user):
    """返回已登录用户的收藏数量，匿名用户返回零。"""

    if not user.is_authenticated:
        return 0
    return Favorite.objects.filter(user=user).count()
