from django.contrib import admin
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Address,
    CartItem,
    Category,
    Favorite,
    Order,
    OrderItem,
    Product,
    ProductImage,
    Review,
)


admin.site.site_header = "ShopLite 管理后台"
admin.site.site_title = "ShopLite 后台"
admin.site.index_title = "业务管理中心"


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ("image", "image_preview", "is_main", "sort_order")
    readonly_fields = ("image_preview",)

    @admin.display(description="预览")
    def image_preview(self, obj):
        if obj and obj.image:
            return format_html('<img src="{}" style="width:56px;height:56px;object-fit:cover;border-radius:6px;">', obj.image.url)
        return "-"


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = ("product", "product_name", "product_image", "price", "quantity", "subtotal")
    fields = ("product", "product_name", "price", "quantity", "subtotal")

    def has_add_permission(self, request, obj=None):
        return False


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


@admin.register(Category)
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
    date_hierarchy = "created_at"
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


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_no", "user", "status_badge", "pay_amount", "items_count", "address_summary", "created_at", "paid_at")
    list_filter = ("status", "created_at", "paid_at")
    search_fields = ("order_no", "user__username", "address_text", "items__product_name")
    readonly_fields = ("created_at", "paid_at")
    inlines = (OrderItemInline,)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("user",)
    list_per_page = 30
    actions = ("mark_paid", "mark_shipped", "mark_completed", "mark_cancelled")
    fieldsets = (
        ("订单信息", {"fields": ("order_no", "user", "status")}),
        ("金额信息", {"fields": ("total_amount", "discount_amount", "shipping_fee", "pay_amount")}),
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
        queryset.filter(status=Order.STATUS_PENDING).update(status=Order.STATUS_PAID, paid_at=timezone.now())

    @admin.action(description="标记为待收货")
    def mark_shipped(self, request, queryset):
        queryset.filter(status=Order.STATUS_PAID).update(status=Order.STATUS_SHIPPED)

    @admin.action(description="标记为已完成")
    def mark_completed(self, request, queryset):
        queryset.filter(status__in=(Order.STATUS_PAID, Order.STATUS_SHIPPED)).update(status=Order.STATUS_COMPLETED)

    @admin.action(description="取消待付款订单")
    def mark_cancelled(self, request, queryset):
        queryset.filter(status=Order.STATUS_PENDING).update(status=Order.STATUS_CANCELLED)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product_name", "price", "quantity", "subtotal")
    list_filter = ("order__status",)
    search_fields = ("order__order_no", "product_name")
    list_select_related = ("order", "product")
    readonly_fields = ("order", "product", "product_name", "product_image", "price", "quantity", "subtotal")

    def has_add_permission(self, request):
        return False


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "color", "quantity", "subtotal", "is_selected", "updated_at")
    list_filter = ("is_selected", "updated_at")
    search_fields = ("user__username", "product__name", "color")
    list_select_related = ("user", "product")
    list_per_page = 30


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("user", "receiver", "phone", "province", "city", "district", "is_default", "created_at")
    list_filter = ("is_default", "province", "city", "created_at")
    search_fields = ("user__username", "receiver", "phone", "detail")
    list_select_related = ("user",)
    list_editable = ("is_default",)
    ordering = ("-is_default", "-created_at")
    list_per_page = 30
    fieldsets = (
        ("联系人", {"fields": ("user", "receiver", "phone")}),
        ("地址", {"fields": ("province", "city", "district", "detail")}),
        ("设置", {"fields": ("is_default",)}),
    )


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "product__name")
    list_select_related = ("user", "product")
    date_hierarchy = "created_at"


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "username", "rating", "is_anonymous", "created_at")
    list_filter = ("rating", "is_anonymous", "created_at")
    search_fields = ("product__name", "username", "content")
    list_select_related = ("product", "user")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
