from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(
            request,
            username=username,
            password=password
        )
        if user:
            login(request, user)
            return redirect("shop:home")
        else:
            messages.error(
                request,
                "用户名或密码错误"
            )
    return render(
        request,
        "account/login.html"
    )


def logout_view(request):
    logout(request)
    return redirect("shop:login")


def todo_api(name):
    return JsonResponse(
        {
            "detail": f"{name} API 尚未实现",
            "next": "在 shop/api_views.py 中补充具体逻辑。",
        },
        status=501,
    )


def product_list(request):
    return todo_api("商品列表")


def product_detail(request, product_id):
    return todo_api("商品详情")


def cart_add(request):
    return todo_api("加入购物车")


def cart_update(request):
    return todo_api("修改购物车")


def cart_delete(request):
    return todo_api("删除购物车商品")


def order_create(request):
    return todo_api("创建订单")


def favorite_toggle(request):
    return todo_api("收藏/取消收藏")
