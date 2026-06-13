from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("", views.home, name="home"),
    path("category/", views.category, name="category"),
    path("product/<int:product_id>/", views.product_detail, name="product_detail"),
    path("cart/", views.cart, name="cart"),
    path("profile/", views.profile, name="profile"),
    path("checkout/<int:order_id>/", views.create_checkout_session, name="checkout"),
]
