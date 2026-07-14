import json
import ipaddress
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import CartItem, Favorite, PhoneVerificationCode, Product
from .selectors import list_products
from .services import (
    add_product_to_cart,
    authenticate_by_login_identifier,
    bind_phone_with_code,
    create_order_from_cart,
    get_user_by_phone,
    issue_phone_verification_code,
    get_or_create_user_by_wechat,
    mock_wechat_uid,
    parse_quantity,
    register_user_with_phone_code,
    reset_password_with_phone_code,
    validate_phone_code_request,
    verify_phone_code,
)


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        try:
            user = authenticate_by_login_identifier(request, username, password)
        except ValidationError:
            user = None
        if user:
            login(request, user)
            return redirect("shop:home")

        from django.contrib import messages
        messages.error(request, "用户名或密码错误")
    return render(request, "account/login.html")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("account_login")


@require_POST
def phone_code_send(request):
    data = _json_payload(request)
    purpose = data.get("purpose") or PhoneVerificationCode.PURPOSE_LOGIN
    if purpose not in {
        PhoneVerificationCode.PURPOSE_REGISTER,
        PhoneVerificationCode.PURPOSE_LOGIN,
        PhoneVerificationCode.PURPOSE_BIND,
        PhoneVerificationCode.PURPOSE_RESET,
    }:
        return JsonResponse({"error": "验证码用途无效"}, status=400)
    if purpose == PhoneVerificationCode.PURPOSE_BIND and not request.user.is_authenticated:
        return JsonResponse({"error": "请先登录后再绑定手机号"}, status=401)

    if not _allow_request(request, "sms", limit=10, window=600):
        return JsonResponse({"error": "请求过于频繁，请稍后再试"}, status=429)
    try:
        validate_phone_code_request(data.get("phone"), purpose, user=request.user)
        issue_phone_verification_code(data.get("phone"), purpose=purpose)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    return JsonResponse({"success": True, "expires_in": 300})


@require_POST
def phone_register(request):
    data = _json_payload(request)
    password = data.get("password")
    password_confirm = data.get("password_confirm")
    if password != password_confirm:
        return JsonResponse({"error": "两次输入的密码不一致"}, status=400)

    if not _allow_request(request, "phone-register", limit=10, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        user = register_user_with_phone_code(data.get("phone"), data.get("code"), password)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"success": True, "action": "registered", "redirect_url": "/"})


@require_POST
def phone_login(request):
    data = _json_payload(request)
    if not _allow_request(request, "phone-login", limit=20, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        verify_phone_code(data.get("phone"), data.get("code"), purpose=PhoneVerificationCode.PURPOSE_LOGIN)
        user = get_user_by_phone(data.get("phone"))
        if not user:
            raise ValidationError("该手机号未注册，请先注册")
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"success": True, "action": "logged_in", "redirect_url": "/"})


@require_POST
def password_login(request):
    data = _json_payload(request)
    if not _allow_request(request, "password-login", limit=20, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        user = authenticate_by_login_identifier(request, data.get("login"), data.get("password"))
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"success": True, "action": "logged_in", "redirect_url": "/"})


@require_POST
def phone_password_reset(request):
    data = _json_payload(request)
    password = data.get("password")
    if password != data.get("password_confirm"):
        return JsonResponse({"error": "两次输入的密码不一致"}, status=400)
    if not _allow_request(request, "phone-reset", limit=10, window=600):
        return JsonResponse({"error": "尝试次数过多，请稍后再试"}, status=429)
    try:
        reset_password_with_phone_code(data.get("phone"), data.get("code"), password)
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)
    return JsonResponse({"success": True, "redirect_url": "/accounts/login/"})


def wechat_login(request):
    mode = getattr(settings, "WECHAT_LOGIN_MODE", "disabled")
    if mode == "allauth":
        return redirect(reverse("weixin_login"))
    if mode != "mock" or not settings.DEBUG:
        return JsonResponse({"error": "微信登录暂未开放"}, status=503)

    user, _ = get_or_create_user_by_wechat(
        mock_wechat_uid(),
        nickname="微信测试用户",
        extra_data={"nickname": "微信测试用户", "mock": True},
    )
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("shop:home")


def alipay_login(request):
    """支付宝 OAuth 预留入口；取得开放平台资质后在此接入授权回调。"""
    return JsonResponse({"error": "支付宝登录暂未开放"}, status=503)


@require_POST
@login_required
def phone_bind(request):
    data = _json_payload(request)
    try:
        profile = bind_phone_with_code(request.user, data.get("phone"), data.get("code"))
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

    return JsonResponse({"success": True, "phone": profile.phone})


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
    except ValidationError as exc:
        return JsonResponse({"error": _first_error(exc)}, status=400)

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
    if quantity > item.product.stock:
        return JsonResponse({"error": f"库存仅剩 {item.product.stock} 件"}, status=400)
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
        order = create_order_from_cart(
            request.user,
            address_id=data.get("address_id"),
            user_coupon_id=data.get("user_coupon_id"),
            payment_method=data.get("payment_method", "mock"),
        )
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


def _json_payload(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return {}


def _first_error(exc):
    if hasattr(exc, "messages") and exc.messages:
        return exc.messages[0]
    return str(exc)


def _allow_request(request, scope, *, limit, window):
    # 仅在部署明确声明可信反向代理时读取 Nginx 写入的 X-Real-IP。
    candidate = request.META.get("REMOTE_ADDR", "unknown")
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        candidate = request.META.get("HTTP_X_REAL_IP", candidate)
    try:
        ip = str(ipaddress.ip_address(candidate))
    except ValueError:
        ip = "unknown"
    key = f"rate:{scope}:{ip}"
    if cache.add(key, 1, timeout=window):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, timeout=window)
        return True
