import logging
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

try:
    import stripe
except ImportError:
    stripe = None

from .models import Address, CartItem, Category, Favorite, Order, OrderItem, Product

if stripe:
    stripe.api_key = "your_stripe_secret_key"


def safe_categories(with_children=False):
    try:
        categories = list(Category.objects.filter(is_active=True, parent__isnull=True).order_by("sort_order", "id"))
        if with_children:
            category_ids = [cat.id for cat in categories]
            children = Category.objects.filter(is_active=True, parent_id__in=category_ids).order_by("parent_id", "sort_order")
            child_map = {}
            for child in children:
                child_map.setdefault(child.parent_id, []).append(child)
            for cat in categories:
                cat.subcategories = child_map.get(cat.id, [])
        return categories
    except Exception as e:
        logger.warning(f"safe_categories error: {e}")
        return []


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
            try:
                category = Category.objects.get(name=category_name)
                if category.parent:
                    products = products.filter(category__name=category_name)
                else:
                    child_ids = list(category.children.values_list("id", flat=True))
                    products = products.filter(category_id__in=[category.id] + child_ids)
            except Category.DoesNotExist:
                products = products.filter(category__name=category_name)
        if keyword:
            products = products.filter(name__icontains=keyword)
        products = list(products.order_by("-created_at"))
        return products[:limit] if limit else products
    except Exception as e:
        logger.warning(f"safe_products error: {e}")
        return []


def get_product_or_fallback(product_id):
    return get_object_or_404(product_queryset().prefetch_related("images"), id=product_id)


def cart_count(request):
    if not request.user.is_authenticated:
        return 0
    try:
        return CartItem.objects.filter(user=request.user).aggregate(total=Count("id"))["total"] or 0
    except Exception:
        return 0


def base_context(request):
    cart_items = []
    cart_total = 0
    if request.user.is_authenticated:
        try:
            cart_items = list(CartItem.objects.select_related("product").filter(user=request.user)[:3])
            cart_total = sum(item.product.price * item.quantity for item in cart_items)
        except Exception:
            pass
    return {
        "cart_count": cart_count(request),
        "cart_items": cart_items,
        "cart_total": cart_total,
    }


@login_required(login_url="account_login")
def home(request):
    context = {
        **base_context(request),
        "categories": safe_categories(),
        "hot_products": safe_products(10, hot=True),
        "new_products": safe_products(10, new=True),
        "recommended_products": safe_products(10, recommended=True),
    }
    return render(request, "home.html", context)


@login_required(login_url="account_login")
def new_products(request):
    products = safe_products(new=True)
    context = {
        **base_context(request),
        "products": products,
    }
    return render(request, "new_products.html", context)


@login_required(login_url="account_login")
def hot_products(request):
    products = safe_products(hot=True)
    context = {
        **base_context(request),
        "products": products,
    }
    return render(request, "hot_products.html", context)


@login_required(login_url="account_login")
def brand_page(request):
    try:
        brands_qs = Product.objects.filter(is_active=True).values("brand").annotate(count=Count("id")).order_by("-count")[:12]
        brands = [{"name": b["brand"] or "其他品牌", "count": b["count"]} for b in brands_qs if b["brand"]]
    except Exception:
        brands = []
    products = safe_products(recommended=True)
    context = {
        **base_context(request),
        "brands": brands,
        "products": products,
    }
    return render(request, "brand_page.html", context)


@login_required(login_url="account_login")
def category(request):
    active_category = request.GET.get("category", "")
    active_subcategory = request.GET.get("subcategory", "")
    keyword = request.GET.get("q", "").strip()
    sort_type = request.GET.get("sort", "")
    filter_type = request.GET.get("filter", "")
    brand_filter = request.GET.get("brand", "")
    price_min = request.GET.get("price_min", "")
    price_max = request.GET.get("price_max", "")

    page_title = "商品分类"
    breadcrumbs = [{"name": "首页", "url": "shop:home"}]

    # 确定筛选目标分类
    filter_category = active_subcategory or active_category

    if sort_type == "new":
        page_title = "新品上市"
        products = safe_products(new=True)
        breadcrumbs.append({"name": "新品"})
    elif sort_type == "hot":
        page_title = "热门商品"
        products = safe_products(hot=True)
        breadcrumbs.append({"name": "热门"})
    elif filter_type == "brand":
        page_title = "品牌馆"
        products = safe_products(recommended=True)
        breadcrumbs.append({"name": "品牌"})
    elif keyword:
        page_title = f"搜索结果: {keyword}"
        products = safe_products(keyword=keyword)
        breadcrumbs.append({"name": f"搜索: {keyword}"})
    else:
        if filter_category:
            page_title = filter_category
            products = safe_products(category_name=filter_category)
        else:
            products = safe_products()
        breadcrumbs.append({"name": page_title})

    # 品牌筛选
    if brand_filter and sort_type not in ("new", "hot"):
        products = [p for p in products if p.brand == brand_filter]

    # 价格筛选
    if price_min and sort_type not in ("new", "hot"):
        try:
            min_p = float(price_min)
            products = [p for p in products if p.price >= min_p]
        except ValueError:
            pass
    if price_max and sort_type not in ("new", "hot"):
        try:
            max_p = float(price_max)
            products = [p for p in products if p.price <= max_p]
        except ValueError:
            pass

    # 排序
    sort_by = request.GET.get("sort_by", "")
    if sort_by == "sales":
        products.sort(key=lambda p: p.sales, reverse=True)
    elif sort_by == "price_asc":
        products.sort(key=lambda p: p.price)
    elif sort_by == "price_desc":
        products.sort(key=lambda p: p.price, reverse=True)
    elif sort_by == "rating":
        products.sort(key=lambda p: p.rating, reverse=True)

    brands = []
    try:
        brands_qs = Product.objects.filter(is_active=True).values("brand").annotate(count=Count("id")).order_by("-count")[:10]
        brands = [{"name": b["brand"] or "其他品牌", "count": b["count"]} for b in brands_qs if b["brand"]]
    except Exception:
        pass

    context = {
        **base_context(request),
        "categories": safe_categories(with_children=True),
        "products": products,
        "active_category": active_category,
        "active_subcategory": active_subcategory,
        "keyword": keyword,
        "page_title": page_title,
        "breadcrumbs": breadcrumbs,
        "current_sort": sort_type,
        "current_filter": filter_type,
        "brands": brands,
        "brand_filter": brand_filter,
        "price_min": price_min,
        "price_max": price_max,
        "sort_by": sort_by,
    }
    return render(request, "category.html", context)


@login_required(login_url="account_login")
def product_detail(request, product_id):
    product = get_product_or_fallback(product_id)
    breadcrumbs = [{"name": "首页", "url": "shop:home"}]
    if product.category:
        if product.category.parent:
            breadcrumbs.append({"name": product.category.parent.name, "url": f"{reverse('shop:category')}?category={product.category.parent.name}"})
        breadcrumbs.append({"name": product.category.name, "url": f"{reverse('shop:category')}?category={product.category.name}"})
    breadcrumbs.append({"name": product.name})
    context = {
        **base_context(request),
        "product": product,
        "related_products": safe_products(5, recommended=True),
        "breadcrumbs": breadcrumbs,
    }
    return render(request, "product_detail.html", context)


@login_required(login_url="account_login")
def cart(request):
    cart_items = []
    if request.user.is_authenticated:
        try:
            cart_items = list(CartItem.objects.select_related("product").filter(user=request.user))
        except Exception:
            cart_items = []

    subtotal = sum(item.subtotal for item in cart_items) if cart_items else Decimal("0")
    discount = Decimal("30") if subtotal >= 299 else Decimal("0")

    context = {
        **base_context(request),
        "cart_items": cart_items,
        "recommendations": safe_products(5, recommended=True),
        "subtotal": subtotal,
        "discount": discount,
        "total": subtotal - discount,
    }
    return render(request, "cart.html", context)


def add_to_cart(request, product_id):
    if not request.user.is_authenticated:
        return redirect("account_login")

    try:
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
    except Exception:
        pass
    return redirect("shop:cart")


def remove_cart_item(request, item_id):
    if request.user.is_authenticated:
        try:
            CartItem.objects.filter(id=item_id, user=request.user).delete()
        except Exception:
            pass
    return redirect("shop:cart")


@login_required(login_url="account_login")
def profile(request):
    order_stats = {
        "all": 0,
        "pending": 0,
        "paid": 0,
        "shipped": 0,
        "refund": 0,
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


def notifications(request):
    context = {
        **base_context(request),
    }
    return render(request, "notifications.html", context)


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


# ── 购物车结算 ──

@login_required(login_url="account_login")
def checkout(request):
    cart_items = []
    if request.user.is_authenticated:
        try:
            cart_items = list(CartItem.objects.select_related("product").filter(user=request.user))
        except Exception:
            cart_items = []

    if not cart_items:
        return redirect("shop:cart")

    subtotal = sum(item.subtotal for item in cart_items)
    discount = Decimal("30") if subtotal >= 299 else Decimal("0")
    total = subtotal - discount

    addresses = []
    try:
        addresses = list(Address.objects.filter(user=request.user).order_by("-is_default", "-created_at"))
    except Exception:
        pass

    context = {
        **base_context(request),
        "cart_items": cart_items,
        "subtotal": subtotal,
        "discount": discount,
        "total": total,
        "addresses": addresses,
    }
    return render(request, "checkout.html", context)


@require_POST
@login_required(login_url="account_login")
def place_order(request):
    cart_items = CartItem.objects.select_related("product").filter(user=request.user)
    if not cart_items.exists():
        return redirect("shop:cart")

    address_id = request.POST.get("address_id")
    address_text = ""
    if address_id:
        try:
            addr = Address.objects.get(id=address_id, user=request.user)
            address_text = f"{addr.receiver} {addr.phone} {addr.province}{addr.city}{addr.district} {addr.detail}"
        except Address.DoesNotExist:
            pass

    total = sum(item.product.price * item.quantity for item in cart_items)
    discount = Decimal("30") if total >= 299 else Decimal("0")
    pay_amount = total - discount

    import uuid
    order = Order.objects.create(
        user=request.user,
        order_no=uuid.uuid4().hex[:16].upper(),
        total_amount=total,
        discount_amount=discount,
        shipping_fee=0,
        pay_amount=pay_amount,
        address_text=address_text,
        status=Order.STATUS_PAID,
    )

    for item in cart_items:
        OrderItem.objects.create(
            order=order,
            product=item.product,
            product_name=item.product.name,
            product_image=item.product.image.url if item.product.image else "",
            price=item.product.price,
            quantity=item.quantity,
            subtotal=item.product.price * item.quantity,
        )
        item.product.sales = item.product.sales + item.quantity
        item.product.save(update_fields=["sales"])

    cart_items.delete()

    return redirect("shop:order_detail", order_id=order.id)


# ── 订单管理 ──

@login_required(login_url="account_login")
def order_list(request):
    status_filter = request.GET.get("status", "")
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    if status_filter:
        orders = orders.filter(status=status_filter)
    context = {
        **base_context(request),
        "orders": list(orders),
        "status_filter": status_filter,
    }
    return render(request, "order_list.html", context)


@login_required(login_url="account_login")
def order_detail(request, order_id):
    order = get_object_or_404(Order.objects.prefetch_related("items"), id=order_id, user=request.user)
    context = {
        **base_context(request),
        "order": order,
    }
    return render(request, "order_detail.html", context)


# ── 收货地址管理 ──

@login_required(login_url="account_login")
def address_list(request):
    addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-created_at")
    context = {
        **base_context(request),
        "addresses": list(addresses),
    }
    return render(request, "address_list.html", context)


@login_required(login_url="account_login")
def address_add(request):
    if request.method == "POST":
        receiver = request.POST.get("receiver", "")
        phone = request.POST.get("phone", "")
        province = request.POST.get("province", "")
        city = request.POST.get("city", "")
        district = request.POST.get("district", "")
        detail = request.POST.get("detail", "")
        is_default = request.POST.get("is_default") == "on"
        if receiver and phone and detail:
            if is_default:
                Address.objects.filter(user=request.user, is_default=True).update(is_default=False)
            Address.objects.create(
                user=request.user, receiver=receiver, phone=phone,
                province=province, city=city, district=district,
                detail=detail, is_default=is_default,
            )
            return redirect("shop:address_list")
    return render(request, "address_form.html", {**base_context(request)})


@login_required(login_url="account_login")
def address_edit(request, address_id):
    addr = get_object_or_404(Address, id=address_id, user=request.user)
    if request.method == "POST":
        addr.receiver = request.POST.get("receiver", addr.receiver)
        addr.phone = request.POST.get("phone", addr.phone)
        addr.province = request.POST.get("province", addr.province)
        addr.city = request.POST.get("city", addr.city)
        addr.district = request.POST.get("district", addr.district)
        addr.detail = request.POST.get("detail", addr.detail)
        is_default = request.POST.get("is_default") == "on"
        if is_default:
            Address.objects.filter(user=request.user, is_default=True).update(is_default=False)
        addr.is_default = is_default
        addr.save()
        return redirect("shop:address_list")
    return render(request, "address_form.html", {**base_context(request), "address": addr, "edit": True})


@login_required(login_url="account_login")
def address_delete(request, address_id):
    addr = get_object_or_404(Address, id=address_id, user=request.user)
    addr.delete()
    return redirect("shop:address_list")


@login_required(login_url="account_login")
def address_set_default(request, address_id):
    addr = get_object_or_404(Address, id=address_id, user=request.user)
    Address.objects.filter(user=request.user, is_default=True).update(is_default=False)
    addr.is_default = True
    addr.save(update_fields=["is_default"])
    return redirect("shop:address_list")


# ── 收藏管理 ──

@login_required(login_url="account_login")
def favorite_list(request):
    favorites = Favorite.objects.select_related("product").filter(user=request.user).order_by("-created_at")
    context = {
        **base_context(request),
        "favorites": list(favorites),
    }
    return render(request, "favorite_list.html", context)


@require_POST
@login_required(login_url="account_login")
def favorite_toggle_view(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    fav, created = Favorite.objects.get_or_create(user=request.user, product=product)
    if not created:
        fav.delete()
    return redirect(request.META.get("HTTP_REFERER", "shop:home"))