from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .forms import AddressForm, ProfileSettingsForm, RefundRequestForm, SecurePasswordChangeForm
from .models import (
    Address,
    BrowsingHistory,
    CartItem,
    Coupon,
    Favorite,
    Notification,
    Order,
    PaymentTransaction,
    Product,
    RefundRequest,
    UserCoupon,
    UserProfile,
)
from .payments import build_mock_payment_url, handle_payment_notification, parse_payment_payload
from .selectors import (
    active_categories,
    base_context,
    cart_queryset,
    favorite_count,
    list_products,
    order_stats,
    popular_brands,
    product_detail as get_product_detail,
    product_spec_context,
    review_stats,
)
from .services import (
    add_product_to_cart,
    calculate_cart_totals,
    cancel_pending_order,
    claim_coupon,
    complete_order,
    create_refund_request,
    create_order_from_cart,
    mark_order_paid,
    parse_decimal,
    parse_quantity,
)


COLOR_OPTIONS = [
    {"name": "经典黑", "hex": "#1a1a1a"},
    {"name": "珍珠白", "hex": "#f5f0e8"},
    {"name": "深空灰", "hex": "#6b6b6b"},
    {"name": "玫瑰金", "hex": "#e0b9b0"},
    {"name": "宝石蓝", "hex": "#2c5f8a"},
    {"name": "森林绿", "hex": "#4a7c59"},
    {"name": "樱花粉", "hex": "#f4c2c2"},
    {"name": "落日橙", "hex": "#e8924f"},
]


def page_context(request, **kwargs):
    return {**base_context(request), **kwargs}


def health_check(request):
    """供 Docker/Kubernetes/负载均衡探活，不暴露业务数据。"""
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unhealthy"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required(login_url="account_login")
def home(request):
    return render(
        request,
        "home.html",
        page_context(
            request,
            categories=active_categories(),
            hot_products=list_products(10, hot=True),
            new_products=list_products(10, new=True),
            recommended_products=list_products(10, recommended=True),
        ),
    )


@login_required(login_url="account_login")
def new_products(request):
    return render(request, "new_products.html", page_context(request, products=list_products(new=True)))


@login_required(login_url="account_login")
def hot_products(request):
    return render(request, "hot_products.html", page_context(request, products=list_products(hot=True)))


@login_required(login_url="account_login")
def brand_page(request):
    return render(
        request,
        "brand_page.html",
        page_context(
            request,
            brands=popular_brands(12),
            products=list_products(recommended=True),
        ),
    )


@login_required(login_url="account_login")
def category(request):
    filters = _category_filters(request)
    page_title, breadcrumbs, products = _category_products(filters)

    return render(
        request,
        "category.html",
        page_context(
            request,
            categories=active_categories(with_children=True),
            products=products,
            active_category=filters["active_category"],
            active_subcategory=filters["active_subcategory"],
            keyword=filters["keyword"],
            page_title=page_title,
            breadcrumbs=breadcrumbs,
            current_sort=filters["sort_type"],
            current_filter=filters["filter_type"],
            brands=popular_brands(),
            brand_filter=filters["brand_filter"],
            price_min=filters["price_min_raw"],
            price_max=filters["price_max_raw"],
            sort_by=filters["sort_by"],
        ),
    )


def _category_filters(request):
    return {
        "active_category": request.GET.get("category", ""),
        "active_subcategory": request.GET.get("subcategory", ""),
        "keyword": request.GET.get("q", "").strip(),
        "sort_type": request.GET.get("sort", ""),
        "filter_type": request.GET.get("filter", ""),
        "brand_filter": request.GET.get("brand", ""),
        "price_min_raw": request.GET.get("price_min", ""),
        "price_max_raw": request.GET.get("price_max", ""),
        "price_min": parse_decimal(request.GET.get("price_min", "")),
        "price_max": parse_decimal(request.GET.get("price_max", "")),
        "sort_by": request.GET.get("sort_by", ""),
    }


def _category_products(filters):
    breadcrumbs = [{"name": "首页", "url": "shop:home"}]
    filter_category = filters["active_subcategory"] or filters["active_category"]

    if filters["sort_type"] == "new":
        page_title = "新品上市"
        products = list_products(new=True, sort_by=filters["sort_by"])
        breadcrumbs.append({"name": "新品"})
    elif filters["sort_type"] == "hot":
        page_title = "热门商品"
        products = list_products(hot=True, sort_by=filters["sort_by"])
        breadcrumbs.append({"name": "热门"})
    elif filters["filter_type"] == "brand":
        page_title = "品牌馆"
        products = list_products(recommended=True, brand=filters["brand_filter"], sort_by=filters["sort_by"])
        breadcrumbs.append({"name": "品牌"})
    elif filters["keyword"]:
        page_title = f"搜索结果: {filters['keyword']}"
        products = list_products(keyword=filters["keyword"], sort_by=filters["sort_by"])
        breadcrumbs.append({"name": f"搜索: {filters['keyword']}"})
    else:
        page_title = filter_category or "商品分类"
        products = list_products(
            category_name=filter_category or None,
            parent_category_name=filters["active_category"] if filters["active_subcategory"] else None,
            sort_by=filters["sort_by"],
        )
        breadcrumbs.append({"name": page_title})

    if filters["sort_type"] not in ("new", "hot"):
        products = _apply_extra_filters(products, filters)
    return page_title, breadcrumbs, products


def _apply_extra_filters(products, filters):
    if filters["brand_filter"]:
        products = [product for product in products if product.brand == filters["brand_filter"]]
    if filters["price_min"] is not None:
        products = [product for product in products if product.price >= filters["price_min"]]
    if filters["price_max"] is not None:
        products = [product for product in products if product.price <= filters["price_max"]]
    return products


@login_required(login_url="account_login")
def product_detail(request, product_id):
    product = get_product_detail(product_id)
    reviews, stats = review_stats(product)
    breadcrumbs = _product_breadcrumbs(product)
    BrowsingHistory.objects.update_or_create(user=request.user, product=product)

    return render(
        request,
        "product_detail.html",
        page_context(
            request,
            product=product,
            related_products=list_products(5, recommended=True),
            breadcrumbs=breadcrumbs,
            color_options=COLOR_OPTIONS,
            reviews=reviews,
            review_stats=stats,
            **product_spec_context(product),
        ),
    )


def _product_breadcrumbs(product):
    breadcrumbs = [{"name": "首页", "url": "shop:home"}]
    if product.category:
        if product.category.parent:
            parent_name = product.category.parent.name
            breadcrumbs.append(
                {
                    "name": parent_name,
                    "url": f"{reverse('shop:category')}?{urlencode({'category': parent_name})}",
                }
            )
            category_query = urlencode({"category": parent_name, "subcategory": product.category.name})
        else:
            category_query = urlencode({"category": product.category.name})
        breadcrumbs.append({"name": product.category.name, "url": f"{reverse('shop:category')}?{category_query}"})
    breadcrumbs.append({"name": product.name})
    return breadcrumbs


@login_required(login_url="account_login")
def cart(request):
    cart_items = list(cart_queryset(request.user))
    totals = calculate_cart_totals(cart_items)
    return render(
        request,
        "cart.html",
        page_context(
            request,
            cart_items=cart_items,
            recommendations=list_products(5, recommended=True),
            subtotal=totals.subtotal,
            discount=totals.discount,
            total=totals.payable,
        ),
    )


@require_POST
@login_required(login_url="account_login")
def add_to_cart(request, product_id):
    try:
        add_product_to_cart(
            request.user,
            product_id,
            quantity=request.POST.get("quantity", 1),
            color=request.POST.get("color", ""),
            specs=request.POST.get("specs", ""),
        )
    except Product.DoesNotExist:
        messages.error(request, "商品不存在或已下架")
    except ValidationError as exc:
        messages.error(request, _first_error(exc))
    return redirect("shop:cart")


@require_POST
@login_required(login_url="account_login")
def remove_cart_item(request, item_id):
    CartItem.objects.filter(id=item_id, user=request.user).delete()
    return redirect("shop:cart")


@login_required(login_url="account_login")
def profile(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(
        request,
        "profile.html",
        page_context(
            request,
            recent_products=list_products(4, recommended=True),
            favorite_count=favorite_count(request.user),
            order_stats=order_stats(request.user),
            profile_obj=profile_obj,
            history_count=BrowsingHistory.objects.filter(user=request.user).count(),
            coupon_count=UserCoupon.objects.filter(
                user=request.user,
                used_at__isnull=True,
                coupon__valid_until__gt=timezone.now(),
            ).count(),
        ),
    )


@login_required(login_url="account_login")
def settings(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    action = request.POST.get("action") if request.method == "POST" else ""
    settings_form = ProfileSettingsForm(
        request.POST if action == "profile" else None,
        request.FILES if action == "profile" else None,
        instance=profile_obj,
        user=request.user,
    )
    password_form = SecurePasswordChangeForm(request.user, request.POST if action == "password" else None)
    if action == "profile" and settings_form.is_valid():
        settings_form.save()
        messages.success(request, "账户资料已保存")
        return redirect("shop:settings")
    if action == "password" and password_form.is_valid():
        user = password_form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "密码修改成功")
        return redirect("shop:settings")
    return render(
        request,
        "settings.html",
        page_context(request, profile_obj=profile_obj, settings_form=settings_form, password_form=password_form),
    )


@login_required(login_url="account_login")
def notifications(request):
    items = list(Notification.objects.filter(user=request.user)[:100])
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return render(request, "notifications.html", page_context(request, notification_items=items))


@login_required(login_url="account_login")
def history(request):
    period = request.GET.get("period", "all")
    items = BrowsingHistory.objects.select_related("product").filter(user=request.user)
    now = timezone.now()
    if period == "today":
        items = items.filter(viewed_at__date=timezone.localdate())
    elif period == "week":
        items = items.filter(viewed_at__gte=now - timezone.timedelta(days=7))
    elif period == "month":
        items = items.filter(viewed_at__gte=now - timezone.timedelta(days=30))
    elif period != "all":
        period = "all"
    return render(request, "history.html", page_context(request, history_items=list(items[:100]), period=period))


@login_required(login_url="account_login")
def coupons(request):
    user_coupons = list(UserCoupon.objects.select_related("coupon").filter(user=request.user))
    owned_ids = {item.coupon_id for item in user_coupons}
    claimable = [coupon for coupon in Coupon.objects.filter(is_active=True) if coupon.id not in owned_ids and coupon.is_available]
    return render(
        request,
        "coupons.html",
        page_context(request, user_coupons=user_coupons, claimable_coupons=claimable),
    )


@require_POST
@login_required(login_url="account_login")
def coupon_claim(request, coupon_id):
    try:
        _, created = claim_coupon(request.user, coupon_id)
        messages.success(request, "优惠券领取成功" if created else "您已经领取过这张优惠券")
    except (Coupon.DoesNotExist, ValidationError) as exc:
        messages.error(request, _first_error(exc))
    return redirect("shop:coupons")


@login_required(login_url="account_login")
def service(request):
    return render(request, "service.html", page_context(request))


@login_required(login_url="account_login")
def refunds(request):
    refund_items = RefundRequest.objects.select_related("order").prefetch_related("order__items").filter(user=request.user)
    return render(request, "refunds.html", page_context(request, refund_items=list(refund_items)))


@login_required(login_url="account_login")
def bills(request):
    payments = PaymentTransaction.objects.select_related("order").filter(
        order__user=request.user,
        status=PaymentTransaction.STATUS_SUCCESS,
    )
    refunds_qs = RefundRequest.objects.select_related("order").filter(
        user=request.user,
        status=RefundRequest.STATUS_COMPLETED,
    )
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_spend = payments.filter(completed_at__gte=month_start).aggregate(total=Sum("amount"))["total"] or 0
    monthly_refund = refunds_qs.filter(completed_at__gte=month_start).aggregate(total=Sum("amount"))["total"] or 0
    total_spend = payments.aggregate(total=Sum("amount"))["total"] or 0
    entries = sorted(
        [
            {"kind": "payment", "title": "商品购买", "no": p.order.order_no, "amount": p.amount, "time": p.completed_at}
            for p in payments
        ] + [
            {"kind": "refund", "title": "退款到账", "no": r.refund_no, "amount": r.amount, "time": r.completed_at}
            for r in refunds_qs
        ],
        key=lambda item: item["time"],
        reverse=True,
    )
    return render(
        request,
        "bills.html",
        page_context(
            request,
            monthly_spend=monthly_spend,
            monthly_refund=monthly_refund,
            total_spend=total_spend,
            bill_entries=entries[:100],
        ),
    )


@login_required(login_url="account_login")
def help_page(request):
    return render(request, "help_page.html", page_context(request))


@login_required(login_url="account_login")
def checkout(request):
    cart_items = list(cart_queryset(request.user))
    if not cart_items:
        return redirect("shop:cart")

    totals = calculate_cart_totals(cart_items)
    addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-created_at")
    usable_coupons = []
    for user_coupon in UserCoupon.objects.select_related("coupon").filter(user=request.user, used_at__isnull=True):
        try:
            calculate_cart_totals(cart_items, user_coupon=user_coupon)
        except ValidationError:
            continue
        usable_coupons.append(user_coupon)
    return render(
        request,
        "checkout.html",
        page_context(
            request,
            cart_items=cart_items,
            subtotal=totals.subtotal,
            discount=totals.discount,
            total=totals.payable,
            addresses=list(addresses),
            usable_coupons=usable_coupons,
        ),
    )


@require_POST
@login_required(login_url="account_login")
def place_order(request):
    try:
        order = create_order_from_cart(
            request.user,
            address_id=request.POST.get("address_id"),
            user_coupon_id=request.POST.get("user_coupon_id") or None,
            payment_method=request.POST.get("payment_method", Order.PAYMENT_MOCK),
        )
    except ValidationError as exc:
        messages.error(request, _first_error(exc))
        return redirect("shop:cart")

    messages.success(request, "订单已创建，请尽快完成支付")
    return redirect("shop:order_detail", order_id=order.id)


@login_required(login_url="account_login")
def order_list(request):
    status_filter = request.GET.get("status", "")
    orders = Order.objects.prefetch_related("items").filter(user=request.user).order_by("-created_at")
    if status_filter:
        orders = orders.filter(status=status_filter)
    return render(
        request,
        "order_list.html",
        page_context(request, orders=list(orders), status_filter=status_filter),
    )


@login_required(login_url="account_login")
def order_detail(request, order_id):
    order = get_object_or_404(Order.objects.prefetch_related("items"), id=order_id, user=request.user)
    return render(
        request,
        "order_detail.html",
        page_context(
            request,
            order=order,
            payment_url=build_mock_payment_url(request, order) if order.status == Order.STATUS_PENDING else "",
            refund_form=RefundRequestForm(),
        ),
    )


@require_POST
@login_required(login_url="account_login")
def mock_payment(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    try:
        if not (django_settings.DEBUG or django_settings.ENABLE_MOCK_PAYMENT):
            raise ValidationError("模拟支付未开启")
        order, changed = mark_order_paid(order, provider=Order.PAYMENT_MOCK)
    except ValidationError as exc:
        messages.error(request, _first_error(exc))
        return redirect("shop:order_detail", order_id=order.id)

    if changed:
        messages.success(request, "模拟支付成功，订单已进入待发货")
    else:
        messages.info(request, "该订单已经支付，无需重复操作")
    return redirect("shop:order_detail", order_id=order.id)


@csrf_exempt
@require_POST
def payment_notify(request):
    try:
        order, changed = handle_payment_notification(parse_payment_payload(request))
    except (Order.DoesNotExist, ValidationError) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    return JsonResponse(
        {
            "success": True,
            "changed": changed,
            "order_no": order.order_no if order else "",
        }
    )


@csrf_exempt
@require_POST
def alipay_notify(request):
    # 预留支付宝异步通知入口。接入支付宝 SDK 并完成 RSA2 验签前绝不修改订单状态。
    return HttpResponse("not_enabled", status=503)


@login_required(login_url="account_login")
def create_checkout_session(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    if order.status == Order.STATUS_PENDING:
        messages.info(request, "请在订单详情页选择已开放的支付方式")
    return redirect("shop:order_detail", order_id=order.id)


@require_POST
@login_required(login_url="account_login")
def order_cancel(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    _, changed = cancel_pending_order(order)
    messages.success(request, "订单已取消" if changed else "当前订单不能取消")
    return redirect("shop:order_detail", order_id=order.id)


@require_POST
@login_required(login_url="account_login")
def order_complete(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    _, changed = complete_order(order)
    if changed:
        messages.success(request, "已确认收货")
    else:
        messages.error(request, "只有待收货订单可以确认收货")
    return redirect("shop:order_detail", order_id=order.id)


@require_POST
@login_required(login_url="account_login")
def refund_apply(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    form = RefundRequestForm(request.POST)
    if form.is_valid():
        try:
            create_refund_request(request.user, order, **form.cleaned_data)
            messages.success(request, "售后申请已提交")
            return redirect("shop:refunds")
        except ValidationError as exc:
            messages.error(request, _first_error(exc))
    else:
        messages.error(request, "请正确填写售后信息")
    return redirect("shop:order_detail", order_id=order.id)


@login_required(login_url="account_login")
def address_list(request):
    addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-created_at")
    return render(request, "address_list.html", page_context(request, addresses=list(addresses)))


@login_required(login_url="account_login")
def address_add(request):
    form = AddressForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            address = form.save(commit=False)
            address.user = request.user
            _save_default_address(address)
            return redirect("shop:address_list")
    return render(request, "address_form.html", page_context(request, form=form))


@login_required(login_url="account_login")
def address_edit(request, address_id):
    address = get_object_or_404(Address, id=address_id, user=request.user)
    form = AddressForm(request.POST or None, instance=address)
    if request.method == "POST":
        if form.is_valid():
            _save_default_address(form.save(commit=False))
            return redirect("shop:address_list")
    return render(request, "address_form.html", page_context(request, address=address, edit=True, form=form))


@transaction.atomic
def _save_default_address(address):
    if address.is_default or not Address.objects.filter(user=address.user).exclude(id=address.id).exists():
        Address.objects.filter(user=address.user, is_default=True).exclude(id=address.id).update(is_default=False)
        address.is_default = True
    address.save()


def _save_address_from_request(request, address=None):
    data = {
        "receiver": request.POST.get("receiver", "").strip(),
        "phone": request.POST.get("phone", "").strip(),
        "province": request.POST.get("province", "").strip(),
        "city": request.POST.get("city", "").strip(),
        "district": request.POST.get("district", "").strip(),
        "detail": request.POST.get("detail", "").strip(),
        "is_default": request.POST.get("is_default") == "on",
    }
    if not data["receiver"] or not data["phone"] or not data["detail"]:
        messages.error(request, "请填写收货人、手机号和详细地址")
        return False

    if data["is_default"]:
        Address.objects.filter(user=request.user, is_default=True).exclude(id=getattr(address, "id", None)).update(
            is_default=False
        )

    if address:
        for field, value in data.items():
            setattr(address, field, value)
        address.save()
    else:
        Address.objects.create(user=request.user, **data)
    return True


@require_POST
@login_required(login_url="account_login")
def address_delete(request, address_id):
    address = get_object_or_404(Address, id=address_id, user=request.user)
    was_default = address.is_default
    address.delete()
    if was_default:
        replacement = Address.objects.filter(user=request.user).order_by("-created_at").first()
        if replacement:
            replacement.is_default = True
            replacement.save(update_fields=["is_default"])
    return redirect("shop:address_list")


@require_POST
@login_required(login_url="account_login")
def address_set_default(request, address_id):
    address = get_object_or_404(Address, id=address_id, user=request.user)
    Address.objects.filter(user=request.user, is_default=True).update(is_default=False)
    address.is_default = True
    address.save(update_fields=["is_default"])
    return redirect("shop:address_list")


@login_required(login_url="account_login")
def favorite_list(request):
    favorites = Favorite.objects.select_related("product").filter(user=request.user).order_by("-created_at")
    return render(request, "favorite_list.html", page_context(request, favorites=list(favorites)))


@require_POST
@login_required(login_url="account_login")
def favorite_toggle_view(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    favorite, created = Favorite.objects.get_or_create(user=request.user, product=product)
    if not created:
        favorite.delete()
    return redirect(request.META.get("HTTP_REFERER", "shop:home"))


def _first_error(exc):
    if hasattr(exc, "messages") and exc.messages:
        return exc.messages[0]
    return str(exc)
