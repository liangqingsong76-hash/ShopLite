from django.shortcuts import render, get_object_or_404
from .models import Product
import stripe
from django.conf import settings
from django.shortcuts import redirect
from .models import Order

stripe.api_key = "你的Stripe_Secret_Key"


def create_checkout_session(request, order_id):
    order = Order.objects.get(id=order_id)

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[
            {
                'price_data': {
                    'currency': 'cny',
                    'product_data': {
                        'name': 'ShopLite 订单 #{}'.format(order.id),
                    },
                    'unit_amount': int(order.total_amount * 100),  # 转换成整数分
                },
                'quantity': 1,
            },
        ],
        mode='payment',
        success_url='http://localhost:8000/payment/success/' + str(order.id),
        cancel_url='http://localhost:8000/payment/cancel/' + str(order.id),
    )

    # 这一步会生成一个支付链接 checkout_session.url
    return redirect(checkout_session.url)
def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    return render(request, 'product_detail.html', {'product': product})