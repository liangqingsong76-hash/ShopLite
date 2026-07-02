from django.urls import path
from . import api_views
from . import views

app_name = "shop"

urlpatterns = [
    path("", views.home, name="home"),
    path("category/", views.category, name="category"),
    path("new/", views.new_products, name="new_products"),
    path("hot/", views.hot_products, name="hot_products"),
    path("brand/", views.brand_page, name="brand_page"),
    path("product/<int:product_id>/", views.product_detail, name="product_detail"),
    path("cart/", views.cart, name="cart"),
    path("cart/add/<int:product_id>/", views.add_to_cart, name="add_to_cart"),
    path("cart/remove/<int:item_id>/", views.remove_cart_item, name="remove_cart_item"),
    path("profile/", views.profile, name="profile"),
    path("notifications/", views.notifications, name="notifications"),
    path("checkout/<int:order_id>/", views.create_checkout_session, name="checkout"),

    # 购物车结算
    path("checkout/", views.checkout, name="checkout_page"),
    path("place-order/", views.place_order, name="place_order"),

    # 订单管理
    path("orders/", views.order_list, name="order_list"),
    path("order/<int:order_id>/", views.order_detail, name="order_detail"),

    # 收货地址管理
    path("addresses/", views.address_list, name="address_list"),
    path("address/add/", views.address_add, name="address_add"),
    path("address/<int:address_id>/edit/", views.address_edit, name="address_edit"),
    path("address/<int:address_id>/delete/", views.address_delete, name="address_delete"),
    path("address/<int:address_id>/default/", views.address_set_default, name="address_set_default"),

    # 收藏管理
    path("favorites/", views.favorite_list, name="favorite_list"),
    path("favorite/toggle/<int:product_id>/", views.favorite_toggle_view, name="favorite_toggle"),
]