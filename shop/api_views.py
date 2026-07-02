import json
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CartItem, Category, Favorite, Order, OrderItem, Product


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
    products = Product.objects.select_related("category").filter(is_active=True)
    cat = request.GET.get("category")
    if cat:
        products = products.filter(category__name=cat)
    keyword = request.GET.get("q")
    if keyword:
        products = products.filter(name__icontains=keyword)
    sort = request.GET.get("sort", "new")
    if sort == "hot":
        products = products.filter(is_hot=True)
    elif sort == "new":
        products = products.filter(is_new=True)
    products = products.order_by("-created_at")[:50]
    data = [
        {
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
            "is_hot": p.is_hot,
            "is_new": p.is_new,
        }
        for p in products
    ]
    return JsonResponse({"count": len(data), "results": data})


def product_detail_api(request, product_id):
    p = get_object_or_404(Product.objects.select_related("category"), id=product_id, is_active=True)
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
        product_id = data.get("product_id")
        quantity = int(data.get("quantity", 1))
        color = data.get("color", "")
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "无效请求"}, status=400)

    product = get_object_or_404(Product, id=product_id, is_active=True)
    item, created = CartItem.objects.get_or_create(
        user=request.user, product=product, color=color,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity += quantity
        item.save(update_fields=["quantity", "updated_at"])

    count = CartItem.objects.filter(user=request.user).aggregate(total=Count("id"))["total"] or 0
    return JsonResponse({"success": True, "cart_count": count, "item_id": item.id})


@require_POST
@login_required
def cart_update(request):
    try:
        data = json.loads(request.body)
        item_id = data.get("item_id")
        quantity = int(data.get("quantity", 1))
    except (json.JSONDecodeError, ValueError):
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
    cart_items = CartItem.objects.select_related("product").filter(user=request.user)
    if not cart_items.exists():
        return JsonResponse({"error": "购物车为空"}, status=400)

    try:
        data = json.loads(request.body)
        address_id = data.get("address_id")
    except (json.JSONDecodeError, ValueError):
        address_id = None

    from .models import Address
    address_text = ""
    if address_id:
        addr = get_object_or_404(Address, id=address_id, user=request.user)
        address_text = f"{addr.receiver} {addr.phone} {addr.province}{addr.city}{addr.district} {addr.detail}"

    total = sum(item.product.price * item.quantity for item in cart_items)
    discount = 30 if total >= 299 else 0
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

    return JsonResponse({
        "success": True,
        "order_id": order.id,
        "order_no": order.order_no,
        "pay_amount": str(pay_amount),
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