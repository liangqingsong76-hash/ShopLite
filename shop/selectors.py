import json
import logging
from django.conf import settings

from django.db.models import Count
from django.shortcuts import get_object_or_404

from .models import CartItem, Category, Favorite, Notification, Order, Product

logger = logging.getLogger(__name__)


def active_product_queryset():
    return Product.objects.select_related("category").filter(is_active=True)


def active_categories(*, with_children=False):
    categories = list(
        Category.objects.filter(is_active=True, parent__isnull=True).order_by("sort_order", "id")
    )

    if not with_children:
        return categories

    category_ids = [category.id for category in categories]
    children = Category.objects.filter(is_active=True, parent_id__in=category_ids).order_by(
        "parent_id", "sort_order", "id"
    )
    child_map = {}
    for child in children:
        child_map.setdefault(child.parent_id, []).append(child)

    for category in categories:
        category.subcategories = child_map.get(category.id, [])
    return categories


def list_products(
    limit=None,
    *,
    hot=False,
    new=False,
    recommended=False,
    category_name=None,
    keyword=None,
    brand=None,
    price_min=None,
    price_max=None,
    sort_by=None,
):
    products = active_product_queryset()

    if hot:
        products = products.filter(is_hot=True)
    if new:
        products = products.filter(is_new=True)
    if recommended:
        products = products.filter(is_recommended=True)
    if category_name:
        products = _filter_by_category_name(products, category_name)
    if keyword:
        products = products.filter(name__icontains=keyword)
    if brand:
        products = products.filter(brand=brand)
    if price_min is not None:
        products = products.filter(price__gte=price_min)
    if price_max is not None:
        products = products.filter(price__lte=price_max)

    products = products.order_by(*_product_ordering(sort_by))
    if limit:
        products = products[:limit]
    return list(products)


def _filter_by_category_name(products, category_name):
    try:
        category = Category.objects.get(name=category_name)
    except Category.DoesNotExist:
        return products.filter(category__name=category_name)

    if category.parent_id:
        return products.filter(category=category)

    child_ids = list(category.children.values_list("id", flat=True))
    return products.filter(category_id__in=[category.id, *child_ids])


def _product_ordering(sort_by):
    ordering_map = {
        "sales": ("-sales", "-created_at"),
        "price_asc": ("price", "-created_at"),
        "price_desc": ("-price", "-created_at"),
        "rating": ("-rating", "-created_at"),
    }
    return ordering_map.get(sort_by, ("-created_at",))


def product_detail(product_id):
    return get_object_or_404(
        active_product_queryset().prefetch_related("images", "reviews", "category__parent"),
        id=product_id,
    )


def popular_brands(limit=10):
    brands = (
        Product.objects.filter(is_active=True)
        .exclude(brand="")
        .values("brand")
        .annotate(count=Count("id"))
        .order_by("-count", "brand")[:limit]
    )
    return [{"name": item["brand"], "count": item["count"]} for item in brands]


def cart_queryset(user):
    return CartItem.objects.select_related("product").filter(user=user).order_by("-updated_at", "-id")


def cart_count(user):
    if not user.is_authenticated:
        return 0
    return CartItem.objects.filter(user=user).count()


def base_context(request):
    cart_items = []
    cart_total = 0
    unread_notifications = 0
    recent_notifications = []

    if request.user.is_authenticated:
        cart_items = list(cart_queryset(request.user)[:3])
        cart_total = sum(item.subtotal for item in cart_items)
        unread_notifications = Notification.objects.filter(user=request.user, is_read=False).count()
        recent_notifications = list(Notification.objects.filter(user=request.user)[:3])

    return {
        "cart_count": cart_count(request.user),
        "cart_items": cart_items,
        "cart_total": cart_total,
        "unread_notifications": unread_notifications,
        "recent_notifications": recent_notifications,
        "mock_payment_enabled": settings.DEBUG or settings.ENABLE_MOCK_PAYMENT,
    }


def order_stats(user):
    stats = {
        "all": 0,
        Order.STATUS_PENDING: 0,
        Order.STATUS_PAID: 0,
        Order.STATUS_SHIPPED: 0,
        Order.STATUS_COMPLETED: 0,
        Order.STATUS_CANCELLED: 0,
        Order.STATUS_REFUND: 0,
    }
    if not user.is_authenticated:
        return stats

    orders = Order.objects.filter(user=user)
    stats["all"] = orders.count()
    for status in (
        Order.STATUS_PENDING,
        Order.STATUS_PAID,
        Order.STATUS_SHIPPED,
        Order.STATUS_COMPLETED,
        Order.STATUS_CANCELLED,
        Order.STATUS_REFUND,
    ):
        stats[status] = orders.filter(status=status).count()
    return stats


def favorite_count(user):
    if not user.is_authenticated:
        return 0
    return Favorite.objects.filter(user=user).count()


def product_spec_context(product):
    spec_dict = {}
    spec_options = []

    if product.specs:
        try:
            raw_specs = json.loads(product.specs)
        except (json.JSONDecodeError, TypeError):
            raw_specs = {}

        selectable_keys = {"颜色", "规格", "尺寸", "版本", "容量", "款式", "型号", "尺码"}
        for key, value in raw_specs.items():
            if key in selectable_keys and isinstance(value, str) and "/" in value:
                spec_options.append({"label": key, "values": [item.strip() for item in value.split("/") if item.strip()]})
            else:
                spec_dict[key] = value

    if not spec_options:
        spec_options.append({"label": "规格", "values": ["标准版"]})

    return {"spec_dict": spec_dict, "spec_options": spec_options}


def review_stats(product, *, limit=20):
    reviews = list(product.reviews.all()[:limit])
    if not reviews:
        return reviews, []

    total = len(reviews)
    star_counts = {star: 0 for star in range(1, 6)}
    for review in reviews:
        if review.rating in star_counts:
            star_counts[review.rating] += 1

    stats = []
    for star in [5, 4, 3, 2, 1]:
        count = star_counts[star]
        stats.append((star, count, int(count * 100 / total)))
    return reviews, stats
