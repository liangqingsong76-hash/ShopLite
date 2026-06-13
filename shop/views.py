from decimal import Decimal

from django.shortcuts import get_object_or_404, redirect, render

try:
    import stripe
except ImportError:
    stripe = None

from .models import CartItem, Category, Order, Product

if stripe:
    stripe.api_key = "your_stripe_secret_key"


FALLBACK_CATEGORIES = [
    {"name": "家居日用", "icon": "home"},
    {"name": "数码家电", "icon": "monitor"},
    {"name": "服饰箱包", "icon": "bag"},
    {"name": "美妆护肤", "icon": "bottle"},
    {"name": "母婴用品", "icon": "baby"},
    {"name": "运动户外", "icon": "bike"},
    {"name": "食品生鲜", "icon": "shopping-bag"},
    {"name": "图书文娱", "icon": "book"},
]

FALLBACK_PRODUCTS = [
    {"id": 1, "name": "简约电热水壶", "price": Decimal("199"), "original_price": Decimal("299"), "rating": "4.9", "sales": "2.3万"},
    {"id": 2, "name": "极简石英手表", "price": Decimal("299"), "original_price": Decimal("699"), "rating": "4.8", "sales": "1.1万"},
    {"id": 3, "name": "休闲双肩包", "price": Decimal("159"), "original_price": Decimal("229"), "rating": "4.8", "sales": "8200"},
    {"id": 4, "name": "台式静音风扇", "price": Decimal("169"), "original_price": Decimal("249"), "rating": "4.7", "sales": "1.6万"},
    {"id": 5, "name": "保湿喷雾水", "price": Decimal("129"), "original_price": Decimal("199"), "rating": "4.9", "sales": "9500"},
    {"id": 6, "name": "北欧陶瓷马克杯", "price": Decimal("49"), "original_price": None, "rating": "4.8", "sales": "3.7万"},
    {"id": 7, "name": "纯棉毛巾三条装", "price": Decimal("79"), "original_price": None, "rating": "4.9", "sales": "1.9万"},
    {"id": 8, "name": "玻璃密封储物罐", "price": Decimal("39"), "original_price": None, "rating": "4.7", "sales": "2.6万"},
]


def safe_categories():
    try:
        categories = list(Category.objects.all())
        return categories or FALLBACK_CATEGORIES
    except Exception:
        return FALLBACK_CATEGORIES


def safe_products(limit=None):
    try:
        products = list(Product.objects.select_related("category").order_by("-created_at"))
        if products:
            return products[:limit] if limit else products
    except Exception:
        pass
    return FALLBACK_PRODUCTS[:limit] if limit else FALLBACK_PRODUCTS


def home(request):
    context = {
        "categories": safe_categories(),
        "hot_products": safe_products(5),
    }
    return render(request, "home.html", context)


def category(request):
    products = safe_products()
    context = {
        "categories": safe_categories(),
        "products": products,
        "active_category": request.GET.get("category", "家居日用"),
    }
    return render(request, "category.html", context)


def product_detail(request, product_id):
    try:
        product = get_object_or_404(Product, id=product_id)
    except Exception:
        product = next((item for item in FALLBACK_PRODUCTS if item["id"] == product_id), FALLBACK_PRODUCTS[0])

    context = {
        "product": product,
        "related_products": safe_products(5),
    }
    return render(request, "product_detail.html", context)


def cart(request):
    try:
        cart_items = []
        if request.user.is_authenticated:
            cart_items = list(CartItem.objects.select_related("product").filter(user=request.user))
    except Exception:
        cart_items = []

    fallback_items = [
        {"product": FALLBACK_PRODUCTS[0], "quantity": 1},
        {"product": FALLBACK_PRODUCTS[1], "quantity": 1},
    ]
    display_items = cart_items or fallback_items
    subtotal = sum(item.product.price * item.quantity if hasattr(item, "product") else item["product"]["price"] * item["quantity"] for item in display_items)

    context = {
        "cart_items": display_items,
        "recommendations": safe_products(5),
        "subtotal": subtotal,
        "discount": Decimal("30") if subtotal >= 299 else Decimal("0"),
    }
    context["total"] = context["subtotal"] - context["discount"]
    return render(request, "cart.html", context)


def profile(request):
    context = {
        "recent_products": safe_products(4),
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
                    "unit_amount": int(order.total_amount * 100),
                },
                "quantity": 1,
            },
        ],
        mode="payment",
        success_url=f"http://localhost:8000/payment/success/{order.id}",
        cancel_url=f"http://localhost:8000/payment/cancel/{order.id}",
    )
    return redirect(checkout_session.url)
