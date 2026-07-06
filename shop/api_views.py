import json
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CartItem, Favorite, Product
from .selectors import list_products
from .services import add_product_to_cart, create_order_from_cart, parse_quantity


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("shop:home")
        else:
            from django.contrib import messages
            messages.error(request, "用户名或密码错误")
    return render(request, "account/login.html")


def logout_view(request):
    logout(request)
    return redirect("account_login")


# ── 商品 API ──

def product_list(request):
    sort = request.GET.get("sort", "")
    products = list_products(
        50,
        category_name=request.GET.get("category") or None,
        keyword=request.GET.get("q") or None,
        hot=sort == "hot",
        new=sort in ("", "new"),
    )
    data = [_serialize_product(product) for product in products]
    return JsonResponse({"count": len(data), "results": data})


def product_detail_api(request, product_id):
    p = get_object_or_404(Product.objects.select_related("category").prefetch_related("images"), id=product_id, is_active=True)
    return JsonResponse({
        "id": p.id,
        "name": p.name,
        "category": p.category.name if p.category else "",
        "brand": p.brand,
        "price": str(p.price),
        "original_price": str(p.original_price) if p.original_price else None,
        "image": p.image.url if p.image else "",
        "sales": p.sales,
        "rating": str(p.rating),
        "review_count": p.review_count,
        "description": p.description,
        "specs": p.specs,
        "is_hot": p.is_hot,
        "is_new": p.is_new,
        "images": [img.image.url for img in p.images.all()],
    })


# ── 购物车 API ──

@require_POST
@login_required
def cart_add(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "无效请求"}, status=400)

    try:
        item = add_product_to_cart(
            request.user,
            data.get("product_id"),
            quantity=data.get("quantity", 1),
            color=data.get("color", ""),
            specs=data.get("specs", ""),
        )
    except Product.DoesNotExist:
        return JsonResponse({"error": "商品不存在或已下架"}, status=404)

    count = CartItem.objects.filter(user=request.user).aggregate(total=Count("id"))["total"] or 0
    return JsonResponse({"success": True, "cart_count": count, "item_id": item.id})


@require_POST
@login_required
def cart_update(request):
    try:
        data = json.loads(request.body)
        item_id = data.get("item_id")
        quantity = parse_quantity(data.get("quantity", 1))
    except json.JSONDecodeError:
        return JsonResponse({"error": "无效请求"}, status=400)

    item = get_object_or_404(CartItem, id=item_id, user=request.user)
    if quantity > 0:
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    else:
        item.delete()
    return JsonResponse({"success": True})


@require_POST
@login_required
def cart_delete(request):
    try:
        data = json.loads(request.body)
        item_id = data.get("item_id")
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "无效请求"}, status=400)

    CartItem.objects.filter(id=item_id, user=request.user).delete()
    count = CartItem.objects.filter(user=request.user).aggregate(total=Count("id"))["total"] or 0
    return JsonResponse({"success": True, "cart_count": count})


# ── 订单 API ──

@require_POST
@login_required
def order_create(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = {}

    try:
        order = create_order_from_cart(request.user, address_id=data.get("address_id"))
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

    return JsonResponse({
        "success": True,
        "order_id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "pay_amount": str(order.pay_amount),
    })


# ── 收藏 API ──

@require_POST
@login_required
def favorite_toggle(request):
    try:
        data = json.loads(request.body)
        product_id = data.get("product_id")
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "无效请求"}, status=400)

    product = get_object_or_404(Product, id=product_id, is_active=True)
    fav, created = Favorite.objects.get_or_create(user=request.user, product=product)
    if not created:
        fav.delete()
        return JsonResponse({"success": True, "favorited": False})
    return JsonResponse({"success": True, "favorited": True})


def search_suggest(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 1:
        return JsonResponse({"results": []})

    products = Product.objects.filter(is_active=True, name__icontains=q)[:6]
    data = [
        {
            "id": p.id,
            "name": p.name,
            "price": str(p.price),
            "image": p.image.url if p.image else "",
        }
        for p in products
    ]
    return JsonResponse({"results": data})


def _serialize_product(product):
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category.name if product.category else "",
        "brand": product.brand,
        "price": str(product.price),
        "original_price": str(product.original_price) if product.original_price else None,
        "image": product.image.url if product.image else "",
        "sales": product.sales,
        "rating": str(product.rating),
        "review_count": product.review_count,
        "is_hot": product.is_hot,
        "is_new": product.is_new,
    }


def _first_error(exc):
    if hasattr(exc, "messages") and exc.messages:
        return exc.messages[0]
    return str(exc)
