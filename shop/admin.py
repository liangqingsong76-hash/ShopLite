from django.contrib import admin

from .models import CartItem, Category, Order, Product


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "icon")
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "original_price", "stock", "created_at")
    list_filter = ("category", "created_at")
    search_fields = ("name", "description")


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "quantity")
    list_filter = ("user",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("user", "total_amount", "status", "created_at")
    list_filter = ("status", "created_at")
