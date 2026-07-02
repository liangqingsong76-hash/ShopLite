from django.urls import path

from . import api_views

app_name = "api"

urlpatterns = [
    path("login/", api_views.login_view, name="login"),
    path("logout/", api_views.logout_view, name="logout"),
    path("products/", api_views.product_list, name="product_list"),
    path("products/<int:product_id>/", api_views.product_detail_api, name="product_detail"),
    path("cart/add/", api_views.cart_add, name="cart_add"),
    path("cart/update/", api_views.cart_update, name="cart_update"),
    path("cart/delete/", api_views.cart_delete, name="cart_delete"),
    path("orders/create/", api_views.order_create, name="order_create"),
    path("favorites/toggle/", api_views.favorite_toggle, name="favorite_toggle"),
]
