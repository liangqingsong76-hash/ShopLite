from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

try:
    import stripe
except ImportError:
    stripe = None

from .models import CartItem, Category, Favorite, Order, Product

if stripe:
    stripe.api_key = "your_stripe_secret_key"


FALLBACK_CATEGORIES = [
    {"id": 1, "name": "家居日用", "icon": "home"},
    {"id": 2, "name": "数码家电", "icon": "monitor"},
    {"id": 3, "name": "服饰箱包", "icon": "bag"},
    {"id": 4, "name": "美妆护肤", "icon": "bottle"},
    {"id": 5, "name": "母婴用品", "icon": "baby"},
    {"id": 6, "name": "运动户外", "icon": "bike"},
    {"id": 7, "name": "食品生鲜", "icon": "shopping-bag"},
    {"id": 8, "name": "图书文娱", "icon": "book"},
]

FALLBACK_PRODUCTS = [
    {"id": 1, "name": "简约电热水壶", "price": Decimal("199"), "original_price": Decimal("299"), "rating": "4.9", "sales": "2.3万", "review_count": 128},
    {"id": 2, "name": "极简石英手表", "price": Decimal("299"), "original_price": Decimal("699"), "rating": "4.8", "sales": "1.1万", "review_count": 86},
    {"id": 3, "name": "休闲双肩包", "price": Decimal("159"), "original_price": Decimal("229"), "rating": "4.8", "sales": "8200", "review_count": 64},
    {"id": 4, "name": "台式静音风扇", "price": Decimal("169"), "original_price": Decimal("249"), "rating": "4.7", "sales": "1.6万", "review_count": 93},
    {"id": 5, "name": "保湿喷雾水", "price": Decimal("129"), "original_price": Decimal("199"), "rating": "4.9", "sales": "9500", "review_count": 75},
    {"id": 6, "name": "北欧陶瓷马克杯", "price": Decimal("49"), "original_price": None, "rating": "4.8", "sales": "3.7万", "review_count": 43},
    {"id": 7, "name": "纯棉毛巾三条装", "price": Decimal("79"), "original_price": None, "rating": "4.9", "sales": "1.9万", "review_count": 51},
    {"id": 8, "name": "玻璃密封储物罐", "price": Decimal("39"), "original_price": None, "rating": "4.7", "sales": "2.6万", "review_count": 38},
]


def safe_categories():
    try:
        categories = list(Category.objects.filter(is_active=True, parent__isnull=True).order_by("sort_order", "id"))
        return categories or FALLBACK_CATEGORIES
    except Exception:
        return FALLBACK_CATEGORIES


def product_queryset():
    return Product.objects.select_related("category").filter(is_active=True)


def safe_products(limit=None, *, hot=False, new=False, recommended=False, category_name=None, keyword=None):
    try:
        products = product_queryset()
        if hot:
            products = products.filter(is_hot=True)
        if new:
            products = products.filter(is_new=True)
        if recommended:
            products = products.filter(is_recommended=True)
        if category_name:
            products = products.filter(category__name=category_name)
        if keyword:
            products = products.filter(name__icontains=keyword)
        products = list(products.order_by("-created_at"))
        if products:
            return products[:limit] if limit else products
    except Exception:
        pass
    return FALLBACK_PRODUCTS[:limit] if limit else FALLBACK_PRODUCTS


def get_product_or_fallback(product_id):
    try:
        return get_object_or_404(product_queryset().prefetch_related("images"), id=product_id)
    except Exception:
        return next((item for item in FALLBACK_PRODUCTS if item["id"] == product_id), FALLBACK_PRODUCTS[0])


def cart_count(request):
    if not request.user.is_authenticated:
        return 0
    try:
        return CartItem.objects.filter(user=request.user).aggregate(total=Count("id"))["total"] or 0
    except Exception:
        return 0


def base_context(request):
    return {
        "cart_count": cart_count(request),
    }


@login_required
def home(request):
    context = {
        **base_context(request),
        "categories": safe_categories(),
        "hot_products": safe_products(5, hot=True),
    }
    return render(request, "home.html", context)


def category(request):
    active_category = request.GET.get("category", "家居日用")
    keyword = request.GET.get("q", "").strip()
    products = safe_products(category_name=active_category if not keyword else None, keyword=keyword or None)
    context = {
        **base_context(request),
        "categories": safe_categories(),
        "products": products,
        "active_category": active_category,
        "keyword": keyword,
        "breadcrumbs": [{"name": "首页", "url": "shop:home"}, {"name": active_category}],
    }
    return render(request, "category.html", context)


def product_detail(request, product_id):
    product = get_product_or_fallback(product_id)
    context = {
        **base_context(request),
        "product": product,
        "related_products": safe_products(5, recommended=True),
        "breadcrumbs": [
            {"name": "首页", "url": "shop:home"},
            {"name": "家居日用", "url": "shop:category"},
            {"name": getattr(product, "name", product["name"])},
        ],
    }
    return render(request, "product_detail.html", context)


def cart(request):
    cart_items = []
    if request.user.is_authenticated:
        try:
            cart_items = list(CartItem.objects.select_related("product").filter(user=request.user))
        except Exception:
            cart_items = []

    fallback_items = [
        {"product": FALLBACK_PRODUCTS[0], "quantity": 1, "subtotal": FALLBACK_PRODUCTS[0]["price"]},
        {"product": FALLBACK_PRODUCTS[1], "quantity": 1, "subtotal": FALLBACK_PRODUCTS[1]["price"]},
    ]
    display_items = cart_items or fallback_items
    subtotal = sum(getattr(item, "subtotal", item["subtotal"]) for item in display_items)
    discount = Decimal("30") if subtotal >= 299 else Decimal("0")

    context = {
        **base_context(request),
        "cart_items": display_items,
        "recommendations": safe_products(5, recommended=True),
        "subtotal": subtotal,
        "discount": discount,
        "total": subtotal - discount,
    }
    return render(request, "cart.html", context)


@login_required
def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    quantity = int(request.POST.get("quantity", 1))
    color = request.POST.get("color", "")
    item, created = CartItem.objects.get_or_create(
        user=request.user,
        product=product,
        color=color,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity += quantity
        item.save(update_fields=["quantity", "updated_at"])
    return redirect("shop:cart")


@login_required
def remove_cart_item(request, item_id):
    CartItem.objects.filter(id=item_id, user=request.user).delete()
    return redirect("shop:cart")


def profile(request):
    order_stats = {
        "all": 12,
        "pending": 5,
        "paid": 3,
        "shipped": 2,
        "refund": 1,
    }
    if request.user.is_authenticated:
        try:
            orders = Order.objects.filter(user=request.user)
            order_stats = {
                "all": orders.count(),
                "pending": orders.filter(status=Order.STATUS_PENDING).count(),
                "paid": orders.filter(status=Order.STATUS_PAID).count(),
                "shipped": orders.filter(status=Order.STATUS_SHIPPED).count(),
                "refund": orders.filter(status=Order.STATUS_REFUND).count(),
            }
        except Exception:
            pass

    context = {
        **base_context(request),
        "recent_products": safe_products(4, recommended=True),
        "favorite_count": Favorite.objects.filter(user=request.user).count() if request.user.is_authenticated else 0,
        "order_stats": order_stats,
    }
    return render(request, "profile.html", context)


def create_checkout_session(request, order_id):
    if stripe is None:
        return redirect("shop:cart")

    order = get_object_or_404(Order, id=order_id)
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "cny",
                    "product_data": {"name": f"ShopLite 订单 #{order.id}"},
                    "unit_amount": int(order.pay_amount * 100),
                },
                "quantity": 1,
            },
        ],
        mode="payment",
        success_url=request.build_absolute_uri(reverse("shop:profile")),
        cancel_url=request.build_absolute_uri(reverse("shop:cart")),
    )
    return redirect(checkout_session.url)
