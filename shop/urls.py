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
    path("checkout/<int:order_id>/", views.create_checkout_session, name="checkout"),
]
