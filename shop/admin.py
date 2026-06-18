from django.contrib import admin

from .models import Address, CartItem, Category, Favorite, Order, OrderItem, Product, ProductImage


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "product_name", "product_image", "price", "quantity", "subtotal")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "icon", "sort_order", "is_active")
    list_filter = ("is_active", "parent")
    search_fields = ("name",)
    list_editable = ("sort_order", "is_active")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "brand",
        "price",
        "original_price",
        "stock",
        "sales",
        "is_hot",
        "is_new",
        "is_active",
    )
    list_filter = ("category", "is_hot", "is_new", "is_recommended", "is_active", "created_at")
    search_fields = ("name", "brand", "description")
    list_editable = ("price", "stock", "is_hot", "is_new", "is_active")
    inlines = (ProductImageInline,)


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "color", "quantity", "is_selected", "updated_at")
    list_filter = ("user", "is_selected")
    search_fields = ("user__username", "product__name")


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("user", "receiver", "phone", "city", "district", "is_default")
    list_filter = ("is_default", "province", "city")
    search_fields = ("user__username", "receiver", "phone", "detail")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_no", "user", "total_amount", "discount_amount", "pay_amount", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order_no", "user__username")
    inlines = (OrderItemInline,)


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    search_fields = ("user__username", "product__name")
