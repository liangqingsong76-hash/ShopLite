from django.urls import path

from . import api_views

app_name = "api"

urlpatterns = [
    path("login/", api_views.login_view, name="login"),
    path("logout/", api_views.logout_view, name="logout"),
    path("auth/phone/code/", api_views.phone_code_send, name="phone_code_send"),
    path("auth/phone/register/", api_views.phone_register, name="phone_register"),
    path("auth/phone/login/", api_views.phone_login, name="phone_login"),
    path("auth/password/login/", api_views.password_login, name="password_login"),
    path("auth/phone/password-reset/", api_views.phone_password_reset, name="phone_password_reset"),
    path("auth/wechat/login/", api_views.wechat_login, name="wechat_login"),
    path("auth/alipay/login/", api_views.alipay_login, name="alipay_login"),
    path("account/phone/bind/", api_views.phone_bind, name="phone_bind"),
    path("products/", api_views.product_list, name="product_list"),
    path("products/<int:product_id>/", api_views.product_detail_api, name="product_detail"),
    path("cart/add/", api_views.cart_add, name="cart_add"),
    path("cart/update/", api_views.cart_update, name="cart_update"),
    path("cart/delete/", api_views.cart_delete, name="cart_delete"),
    path("orders/create/", api_views.order_create, name="order_create"),
    path("favorites/toggle/", api_views.favorite_toggle, name="favorite_toggle"),
    path("search/suggest/", api_views.search_suggest, name="search_suggest"),
]
