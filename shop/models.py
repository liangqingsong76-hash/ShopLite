"""旧 ``shop.models`` 的兼容导出层。

依赖流向：旧视图、模板和测试 -> 本模块 -> accounts/catalog/commerce/
notifications/payments 领域模型。新代码应直接导入所属领域，避免重新形成单体模型层。
"""

from accounts.models import PhoneVerificationCode, UserProfile
from catalog.models import BrowsingHistory, Category, Product, ProductImage, ProductSource, Review
from commerce.models import Address, CartItem, Coupon, Favorite, Order, OrderItem, RefundRequest, UserCoupon
from notifications.models import Notification
from payments.models import PaymentTransaction


__all__ = (
    "Address",
    "BrowsingHistory",
    "CartItem",
    "Category",
    "Coupon",
    "Favorite",
    "Notification",
    "Order",
    "OrderItem",
    "PaymentTransaction",
    "PhoneVerificationCode",
    "Product",
    "ProductImage",
    "ProductSource",
    "RefundRequest",
    "Review",
    "UserCoupon",
    "UserProfile",
)
