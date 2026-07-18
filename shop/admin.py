from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Address,
    BrowsingHistory,
    CartItem,
    Category,
    Coupon,
    Favorite,
    Notification,
    Order,
    OrderItem,
    PaymentTransaction,
    PhoneVerificationCode,
    Product,
    ProductImage,
    Review,
    RefundRequest,
    UserCoupon,
    UserProfile,
)
from .services import cancel_pending_order, complete_order, complete_refund, mark_order_paid, mark_order_shipped


admin.site.site_header = "ShopLite 管理后台"   # site_header`：后台顶部标题
admin.site.site_title = "ShopLite 后台"    # site_title`：浏览器标题
admin.site.index_title = "业务管理中心"    # index_title`：后台首页标题


class ProductImageInline(admin.TabularInline):  # TabularInline 是 Django Admin 提供的一种内联展示方式，以表格形式展示关联的多个子记录。还有一个 StackedInline 是堆叠式（每个字段竖着排），TabularInline 更紧凑。
    model = ProductImage
    # 编辑页面底部默认显示 1 行空白输入框，方便你直接添加新图片。如果设为 0，就不显示空白行，只能编辑已有的
    extra = 1
    '''
    字段	            说明
    image	        文件上传框，选图片文件
    image_preview	图片预览（只读，自动生成）
    is_main	        是否设为主图
    sort_order	    排序
    '''
    fields = ("image", "image_preview", "is_main", "sort_order")   # 参数是models.py文件中ProductImage的属性
    readonly_fields = ("image_preview",)   # 在编辑页面里，image_preview 这个字段只显示内容，不让用户修改

    @admin.display(description="预览")  # 自定义表格这一列的标题文字
    def image_preview(self, obj):
        if obj and obj.image:   # obj当前这一行对应的数据库记录对象
            '''
            obj.image.url：获取图片的访问地址
            format_html(...)：安全地拼接HTML字符串，相当于如下标签：
            <img src="/media/products/phone.jpg" style="width:56px;height:56px;object-fit:cover;border-radius:6px;">
            '''
            return format_html('<img src="{}" style="width:56px;height:56px;object-fit:cover;border-radius:6px;">', obj.image.url)
        return "-"


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0   # 不显示空白行
    can_delete = False   # 不允许后台随便删订单明细
    # fields 决定哪些字段显示在 Admin 页面上以及顺序如何，readonly_fields 决定哪些字段只能看不能改。
    readonly_fields = ("product", "product_name", "product_image", "price", "quantity", "subtotal")
    fields = ("product", "product_name", "price", "quantity", "subtotal")

    '''
    部分	                说明
    has_add_permission	Django Admin 的内置钩子方法，用来控制是否允许新增记录
    返回 False	        彻底禁用"新增"按钮，不能在订单下凭空添加明细
    '''
    def has_add_permission(self, request, obj=None):
        return False


# 自定义过滤器：用于在后台商品列表页按库存状态快速筛选
class StockLevelFilter(admin.SimpleListFilter):
    title = "库存状态"
    parameter_name = "stock_level"

    def lookups(self, request, model_admin):
        return (
            ("empty", "无库存"),
            ("low", "低库存（1-10）"),
            ("normal", "库存充足"),
        )

    def queryset(self, request, queryset):
        if self.value() == "empty":
            return queryset.filter(stock__lte=0)
        if self.value() == "low":
            return queryset.filter(stock__gt=0, stock__lte=10)
        if self.value() == "normal":
            return queryset.filter(stock__gt=10)
        return queryset


#  商品分类（Category）模型的后台配置
@admin.register(Category)   # admin.site.register(Category, CategoryAdmin)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "sort_order", "is_active", "children_count", "product_count")
    list_filter = ("is_active", "parent")
    search_fields = ("name", "parent__name")
    list_editable = ("sort_order", "is_active")
    ordering = ("parent__id", "sort_order", "id")
    list_per_page = 30
    fieldsets = (
        ("基础信息", {"fields": ("name", "icon", "parent")}),
        ("展示设置", {"fields": ("sort_order", "is_active")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").annotate(
            children_total=Count("children", distinct=True),
            products_total=Count("product", distinct=True),
        )

    @admin.display(description="子分类数", ordering="children_total")
    def children_count(self, obj):
        return obj.children_total

    @admin.display(description="商品数", ordering="products_total")
    def product_count(self, obj):
        return obj.products_total


#  商品管理（Product）模型的后台配置
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "image_thumb",
        "name",
        "category_path",
        "brand",
        "price",
        "stock_badge",
        "sales",
        "rating",
        "product_flags",
        "is_active",
        "updated_at",
    )
    list_display_links = ("image_thumb", "name")
    list_filter = (
        "is_active",
        "is_hot",
        "is_new",
        "is_recommended",
        StockLevelFilter,
        "category",
        "created_at",
    )
    search_fields = ("name", "brand", "description", "category__name")
    list_editable = ("price", "is_active")
    list_select_related = ("category", "category__parent")
    readonly_fields = ("created_at", "updated_at", "image_thumb_large")
    inlines = (ProductImageInline,)
    # 不使用 date_hierarchy：MySQL 未安装时区表时，Django 的 CONVERT_TZ 会返回 NULL 并导致列表页 500。
    # list_filter 中的 created_at 仍然提供按日期筛选，且不依赖服务器时区表。
    ordering = ("-created_at",)
    list_per_page = 30
    actions = ("set_active", "set_inactive", "set_hot", "set_recommended", "clear_marketing_flags")
    fieldsets = (
        ("基础信息", {"fields": ("name", "category", "brand", "description")}),
        ("价格与库存", {"fields": ("price", "original_price", "stock", "sales")}),
        ("主图与参数", {"fields": ("image", "image_thumb_large", "specs")}),
        ("运营标签", {"fields": ("is_hot", "is_new", "is_recommended", "is_active")}),
        ("评价与时间", {"fields": ("rating", "review_count", "created_at", "updated_at")}),
    )

    @admin.display(description="图片")
    def image_thumb(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width:44px;height:44px;object-fit:cover;border-radius:6px;">', obj.image.url)
        return format_html('<span style="color:#999;">无图</span>')

    @admin.display(description="主图预览")
    def image_thumb_large(self, obj):
        if obj and obj.image:
            return format_html('<img src="{}" style="width:160px;height:160px;object-fit:cover;border-radius:8px;">', obj.image.url)
        return "暂无主图"

    @admin.display(description="分类", ordering="category__name")
    def category_path(self, obj):
        if obj.category and obj.category.parent:
            return f"{obj.category.parent.name} / {obj.category.name}"
        return obj.category.name if obj.category else "-"

    @admin.display(description="库存", ordering="stock")
    def stock_badge(self, obj):
        if obj.stock <= 0:
            color, label = "#c62828", "无库存"
        elif obj.stock <= 10:
            color, label = "#ef8f00", f"低库存 {obj.stock}"
        else:
            color, label = "#2e7d32", str(obj.stock)
        return format_html('<span style="color:{};font-weight:700;">{}</span>', color, label)

    @admin.display(description="标签")
    def product_flags(self, obj):
        flags = []
        if obj.is_hot:
            flags.append("热门")
        if obj.is_new:
            flags.append("新品")
        if obj.is_recommended:
            flags.append("推荐")
        return " / ".join(flags) if flags else "-"

    @admin.action(description="上架所选商品")
    def set_active(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="下架所选商品")
    def set_inactive(self, request, queryset):
        queryset.update(is_active=False)

    @admin.action(description="设为热门")
    def set_hot(self, request, queryset):
        queryset.update(is_hot=True)

    @admin.action(description="设为推荐")
    def set_recommended(self, request, queryset):
        queryset.update(is_recommended=True)

    @admin.action(description="清除运营标签")
    def clear_marketing_flags(self, request, queryset):
        queryset.update(is_hot=False, is_new=False, is_recommended=False)


# 商品图片（ProductImage）模型的后台配置
@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("image_preview", "product", "is_main", "sort_order")
    list_filter = ("is_main",)
    search_fields = ("product__name",)
    list_select_related = ("product",)
    list_editable = ("is_main", "sort_order")

    @admin.display(description="图片")
    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width:54px;height:54px;object-fit:cover;border-radius:6px;">', obj.image.url)
        return "-"


# 订单管理（Order）模型的后台配置
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_no", "user", "status_badge", "pay_amount", "items_count", "address_summary", "created_at", "paid_at")
    list_filter = ("status", "created_at", "paid_at")
    search_fields = ("order_no", "user__username", "address_text", "items__product_name")
    readonly_fields = (
        "order_no", "user", "status", "total_amount", "discount_amount", "shipping_fee", "pay_amount",
        "address_text", "coupon", "payment_method", "payment_no", "created_at", "paid_at",
    )
    inlines = (OrderItemInline,)
    ordering = ("-created_at",)
    list_select_related = ("user",)
    list_per_page = 30
    actions = ("mark_paid", "mark_shipped", "mark_completed", "mark_cancelled")
    fieldsets = (
        ("订单信息", {"fields": ("order_no", "user", "status", "coupon")}),
        ("金额信息", {"fields": ("total_amount", "discount_amount", "shipping_fee", "pay_amount", "payment_method", "payment_no")}),
        ("收货信息", {"fields": ("address_text",)}),
        ("时间信息", {"fields": ("created_at", "paid_at")}),
    )

    @admin.display(description="状态", ordering="status")
    def status_badge(self, obj):
        colors = {
            Order.STATUS_PENDING: "#ef8f00",
            Order.STATUS_PAID: "#2563eb",
            Order.STATUS_SHIPPED: "#5b8c5a",
            Order.STATUS_COMPLETED: "#666",
            Order.STATUS_CANCELLED: "#999",
            Order.STATUS_REFUND: "#c62828",
        }
        return format_html(
            '<span style="color:{};font-weight:700;">{}</span>',
            colors.get(obj.status, "#333"),
            obj.get_status_display(),
        )

    @admin.display(description="商品件数")
    def items_count(self, obj):
        return obj.items.count()

    @admin.display(description="收货摘要")
    def address_summary(self, obj):
        return obj.address_text[:28] + "..." if len(obj.address_text) > 28 else obj.address_text or "-"

    @admin.action(description="标记为待发货")
    def mark_paid(self, request, queryset):
        from django.conf import settings

        if not (settings.DEBUG or settings.ENABLE_MOCK_PAYMENT):
            self.message_user(request, "生产环境未开启模拟支付，不能从后台伪造付款", messages.ERROR)
            return
        changed = failed = 0
        for order in queryset:
            try:
                _, did_change = mark_order_paid(order, provider=Order.PAYMENT_MOCK)
                changed += int(did_change)
            except ValidationError:
                failed += 1
        self.message_user(request, f"成功确认 {changed} 个订单，跳过/失败 {failed} 个", messages.SUCCESS if not failed else messages.WARNING)

    @admin.action(description="标记为待收货")
    def mark_shipped(self, request, queryset):
        changed = 0
        for order in queryset:
            _, did_change = mark_order_shipped(order)
            changed += int(did_change)
        self.message_user(request, f"已将 {changed} 个订单标记为待收货", messages.SUCCESS)

    @admin.action(description="标记为已完成")
    def mark_completed(self, request, queryset):
        changed = 0
        for order in queryset:
            _, did_change = complete_order(order)
            changed += int(did_change)
        self.message_user(request, f"已完成 {changed} 个待收货订单", messages.SUCCESS)

    @admin.action(description="取消待付款订单")
    def mark_cancelled(self, request, queryset):
        changed = 0
        for order in queryset:
            _, did_change = cancel_pending_order(order)
            changed += int(did_change)
        self.message_user(request, f"已取消 {changed} 个待付款订单", messages.SUCCESS)


# 订单明细（OrderItem）模型的后台配置
@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product_name", "price", "quantity", "subtotal")
    list_filter = ("order__status",)
    search_fields = ("order__order_no", "product_name")
    list_select_related = ("order", "product")
    readonly_fields = ("order", "product", "product_name", "product_image", "price", "quantity", "subtotal")

    def has_add_permission(self, request):
        return False


# 购物车项（CartItem）模型的后台配置
@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "color", "quantity", "subtotal", "is_selected", "updated_at")
    list_filter = ("is_selected", "updated_at")
    search_fields = ("user__username", "product__name", "color")
    list_select_related = ("user", "product")
    list_per_page = 30


# 用户资料（UserProfile）模型的后台配置
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "phone", "phone_verified_at", "updated_at")
    search_fields = ("user__username", "phone")
    list_select_related = ("user",)
    readonly_fields = ("created_at", "updated_at", "phone_verified_at")


# 短信验证码（PhoneVerificationCode）模型的后台配置
@admin.register(PhoneVerificationCode)
class PhoneVerificationCodeAdmin(admin.ModelAdmin):
    list_display = ("phone", "purpose", "created_at", "expires_at", "used_at", "sent_to_backend")
    list_filter = ("purpose", "sent_to_backend", "created_at", "used_at")
    search_fields = ("phone",)
    readonly_fields = ("phone", "code", "purpose", "created_at", "expires_at", "used_at", "attempt_count", "last_attempt_at", "sent_to_backend")

    def has_add_permission(self, request):
        return False


# 收货地址（Address）模型的后台配置
@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("user", "receiver", "phone", "province", "city", "district", "is_default", "created_at")
    list_filter = ("is_default", "province", "city", "created_at")
    search_fields = ("user__username", "receiver", "phone", "detail")
    list_select_related = ("user",)
    ordering = ("-is_default", "-created_at")
    list_per_page = 30
    fieldsets = (
        ("联系人", {"fields": ("user", "receiver", "phone")}),
        ("地址", {"fields": ("province", "city", "district", "detail")}),
        ("设置", {"fields": ("is_default",)}),
    )


# 商品收藏（Favorite）模型的后台配置
@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "product__name")
    list_select_related = ("user", "product")


# 商品评价（Review）模型的后台配置
@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "username", "rating", "is_anonymous", "created_at")
    list_filter = ("rating", "is_anonymous", "created_at")
    search_fields = ("product__name", "username", "content")
    list_select_related = ("product", "user")
    readonly_fields = ("created_at",)


# 优惠券（Coupon）模型的后台配置
@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "discount_type", "value", "minimum_spend", "stock_status", "valid_from", "valid_until", "is_active")
    list_filter = ("discount_type", "is_active", "valid_from", "valid_until")
    search_fields = ("code", "name", "description")
    readonly_fields = ("claimed_count", "created_at")
    list_editable = ("is_active",)

    @admin.display(description="领取情况")
    def stock_status(self, obj):
        return f"{obj.claimed_count} / {obj.total_quantity or '不限量'}"


# 用户领取的优惠券（UserCoupon）模型的后台配置
@admin.register(UserCoupon)
class UserCouponAdmin(admin.ModelAdmin):
    list_display = ("user", "coupon", "status_display", "claimed_at", "used_at")
    list_filter = ("coupon", "claimed_at", "used_at")
    search_fields = ("user__username", "coupon__code", "coupon__name")
    list_select_related = ("user", "coupon")
    readonly_fields = ("claimed_at", "used_at")

    @admin.display(description="状态")
    def status_display(self, obj):
        return {"available": "可使用", "used": "已使用", "expired": "已过期"}.get(obj.status, obj.status)

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


# 支付流水（PaymentTransaction）模型的后台配置
@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "order", "provider", "amount", "status", "created_at", "completed_at")
    list_filter = ("provider", "status", "created_at")
    search_fields = ("transaction_no", "order__order_no", "order__user__username")
    list_select_related = ("order", "order__user")
    readonly_fields = ("order", "provider", "transaction_no", "amount", "status", "raw_payload", "created_at", "completed_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# 退款管理（RefundRequest）模型的后台配置
@admin.register(RefundRequest)
class RefundRequestAdmin(admin.ModelAdmin):
    list_display = ("refund_no", "order", "user", "amount", "status", "created_at", "completed_at")
    list_filter = ("status", "created_at", "completed_at")
    search_fields = ("refund_no", "order__order_no", "user__username", "reason")
    list_select_related = ("order", "user")
    readonly_fields = ("refund_no", "order", "user", "amount", "status", "order_status_before", "reason", "description", "created_at", "updated_at", "completed_at")
    actions = ("approve_refunds", "complete_refunds", "reject_refunds")

    @admin.action(description="同意所选退款")
    def approve_refunds(self, request, queryset):
        count = queryset.filter(status=RefundRequest.STATUS_PENDING).update(status=RefundRequest.STATUS_APPROVED)
        self.message_user(request, f"已同意 {count} 条退款申请", messages.SUCCESS)

    @admin.action(description="完成所选退款并回补库存")
    def complete_refunds(self, request, queryset):
        changed = failed = 0
        for refund in queryset:
            try:
                _, did_change = complete_refund(refund)
                changed += int(did_change)
            except ValidationError:
                failed += 1
        self.message_user(request, f"完成 {changed} 条，跳过/失败 {failed} 条", messages.SUCCESS if not failed else messages.WARNING)

    @admin.action(description="拒绝所选退款")
    def reject_refunds(self, request, queryset):
        count = queryset.filter(status=RefundRequest.STATUS_PENDING).update(status=RefundRequest.STATUS_REJECTED)
        for refund in queryset.filter(status=RefundRequest.STATUS_REJECTED).select_related("order"):
            if refund.order.status == Order.STATUS_REFUND:
                refund.order.status = refund.order_status_before
                refund.order.save(update_fields=["status"])
        self.message_user(request, f"已拒绝 {count} 条退款申请", messages.SUCCESS)


# 站内通知（Notification）模型的后台配置
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "category", "is_read", "created_at")
    list_filter = ("category", "is_read", "created_at")
    search_fields = ("title", "content", "user__username")
    list_select_related = ("user",)
    readonly_fields = ("created_at",)


# 浏览记录（BrowsingHistory）模型的后台配置
@admin.register(BrowsingHistory)
class BrowsingHistoryAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "viewed_at")
    search_fields = ("user__username", "product__name")
    list_select_related = ("user", "product")
    readonly_fields = ("user", "product", "viewed_at")

    def has_add_permission(self, request):
        return False
