from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from shop.models import (
    Address,
    CartItem,
    Category,
    Coupon,
    Notification,
    Order,
    Product,
    UserProfile,
)
from shop.services import create_order_from_cart, mark_order_paid


DEMO_PASSWORD = "ShopLiteTest!2026"
ADMIN_PASSWORD = "ShopLiteAdmin!2026"


class Command(BaseCommand):
    help = "创建本地验收所需的管理员、个人用户、商品、优惠券与订单数据（可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--allow-production", action="store_true", help="明确允许在生产环境执行（强烈不建议）")

    def handle(self, *args, **options):
        if getattr(settings, "PRODUCTION", False) and not options["allow_production"]:
            raise CommandError("生产环境禁止创建演示账号；如确有需要请显式传 --allow-production")

        admin = self._user("shoplite_admin", "13800000000", "平台管理员", ADMIN_PASSWORD, staff=True)
        users = [
            self._user("buyer_01", "13800000001", "测试用户一", DEMO_PASSWORD),
            self._user("buyer_02", "13800000002", "测试用户二", DEMO_PASSWORD),
            self._user("buyer_03", "13800000003", "测试用户三", DEMO_PASSWORD),
        ]
        products = self._products()
        self._coupons()
        for index, user in enumerate(users, start=1):
            address, _ = Address.objects.update_or_create(
                user=user,
                receiver=f"测试用户{index}",
                defaults={
                    "phone": f"1380000000{index}",
                    "province": "广东省",
                    "city": "深圳市",
                    "district": "南山区",
                    "detail": f"科技园测试路 {index} 号",
                    "is_default": True,
                },
            )
            Notification.objects.get_or_create(
                user=user,
                title="欢迎体验 ShopLite",
                defaults={"content": "演示账号已准备好，可测试收藏、购物车、优惠券、订单与个人设置。"},
            )
            if not Order.objects.filter(user=user).exists():
                CartItem.objects.update_or_create(
                    user=user,
                    product=products[index - 1],
                    color="演示规格",
                    defaults={"quantity": 1},
                )
                order = create_order_from_cart(user, address_id=address.id)
                if index >= 2:
                    order, _ = mark_order_paid(order, payment_no=f"DEMO-PAY-{index}")
                if index == 3:
                    order.status = Order.STATUS_SHIPPED
                    order.save(update_fields=["status"])
            CartItem.objects.get_or_create(
                user=user,
                product=products[(index + 1) % len(products)],
                color="购物车演示",
                defaults={"quantity": 1},
            )

        self.stdout.write(self.style.SUCCESS("演示数据已创建/更新完成"))
        self.stdout.write(f"管理员：{admin.username} / {ADMIN_PASSWORD}")
        self.stdout.write(f"个人用户：buyer_01、buyer_02、buyer_03 / {DEMO_PASSWORD}")
        self.stdout.write("手机号：13800000001、13800000002、13800000003（均可用密码登录）")

    def _user(self, username, phone, nickname, password, *, staff=False):
        user, _ = User.objects.get_or_create(username=username)
        user.first_name = nickname
        user.email = f"{username}@example.test"
        user.is_staff = staff
        user.is_superuser = staff
        user.is_active = True
        user.set_password(password)
        user.save()
        UserProfile.objects.update_or_create(
            user=user,
            defaults={"phone": phone, "phone_verified_at": timezone.now(), "bio": "ShopLite 功能验收账号"},
        )
        return user

    def _products(self):
        category, _ = Category.objects.get_or_create(name="测试商品", defaults={"icon": "flask", "sort_order": 999})
        definitions = (
            ("轻量通勤双肩包", "199.00", 50),
            ("桌面蓝牙音箱", "329.00", 30),
            ("便携保温杯", "89.00", 80),
            ("人体工学鼠标", "259.00", 40),
        )
        products = []
        for index, (name, price, stock) in enumerate(definitions):
            product, _ = Product.objects.update_or_create(
                name=name,
                defaults={
                    "category": category,
                    "brand": "ShopLite Lab",
                    "price": Decimal(price),
                    "original_price": Decimal(price) + Decimal("40.00"),
                    "stock": stock,
                    "description": "用于本地功能验收的演示商品。",
                    "is_active": True,
                    "is_new": True,
                    "is_recommended": True,
                    "is_hot": index < 2,
                },
            )
            products.append(product)
        return products

    def _coupons(self):
        now = timezone.now()
        definitions = (
            ("WELCOME30", "新人满减券", Coupon.TYPE_FIXED, "30.00", "99.00"),
            ("SAVE10", "九折通用券", Coupon.TYPE_PERCENT, "90.00", "199.00"),
        )
        for code, name, kind, value, minimum in definitions:
            Coupon.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": "演示优惠券，可在结算页选择",
                    "discount_type": kind,
                    "value": Decimal(value),
                    "minimum_spend": Decimal(minimum),
                    "total_quantity": 1000,
                    "valid_from": now - timezone.timedelta(days=1),
                    "valid_until": now + timezone.timedelta(days=90),
                    "is_active": True,
                },
            )
