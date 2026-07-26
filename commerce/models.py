"""客户交易领域的数据模型。

上游：storefront 页面/API、后台管理和 Celery 任务通过 commerce 服务写入这些模型。
下游：模型通过外键引用 accounts 的 Django 用户与 catalog 商品；支付流水属于 payments 应用。
禁止：不要在模型方法中直接发通知、扣库存或调用支付网关，这些副作用必须放在服务层。
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from core.constants import (
    PAYMENT_METHOD_ALIPAY,
    PAYMENT_METHOD_CHOICES,
    PAYMENT_METHOD_MOCK,
    PAYMENT_METHOD_WECHAT,
)


class CartItem(models.Model):
    """用户购物车中的一个商品变体。

    数据流：商品详情/API → ``commerce.services.add_product_to_cart`` → 本模型。
    ``color`` 保存颜色与规格拼成的稳定变体文本，和用户、商品共同保证唯一性。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    color = models.CharField(max_length=100, blank=True)
    is_selected = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """声明购物车变体的后台名称和用户内唯一约束。"""

        verbose_name = "购物车项"
        verbose_name_plural = "购物车项"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "product", "color"),
                name="commerce_unique_cart_variant",
            )
        ]

    def __str__(self):
        """返回便于后台辨识的购物车摘要。"""

        return f"{self.user} - {self.product} x {self.quantity}"

    @property
    def subtotal(self):
        """计算当前数量对应的小计，供结算服务和模板读取。"""

        return self.product.price * self.quantity


class Address(models.Model):
    """用户收货地址。

    数据流：地址表单/API → 地址服务 → 本模型；下单时会被复制为订单地址快照。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses")
    receiver = models.CharField(max_length=50)
    phone = models.CharField(max_length=20)
    province = models.CharField(max_length=50, blank=True)
    city = models.CharField(max_length=50, blank=True)
    district = models.CharField(max_length=50, blank=True)
    detail = models.CharField(max_length=255)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """声明收货地址在后台中的展示名称。"""

        verbose_name = "收货地址"
        verbose_name_plural = "收货地址"

    def __str__(self):
        """返回后台显示所需的收件人和手机号。"""

        return f"{self.receiver} {self.phone}"


class Favorite(models.Model):
    """用户收藏的商品关系。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """声明收藏关系的后台名称和用户商品唯一约束。"""

        verbose_name = "商品收藏"
        verbose_name_plural = "商品收藏"
        constraints = [
            models.UniqueConstraint(fields=("user", "product"), name="commerce_unique_favorite")
        ]

    def __str__(self):
        """返回收藏关系的可读摘要。"""

        return f"{self.user} 收藏 {self.product}"


class Coupon(models.Model):
    """平台发放的优惠券规则。"""

    TYPE_FIXED = "fixed"
    TYPE_PERCENT = "percent"
    TYPE_CHOICES = ((TYPE_FIXED, "满减券"), (TYPE_PERCENT, "折扣券"))

    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    discount_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_FIXED)
    value = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    minimum_spend = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    total_quantity = models.PositiveIntegerField(default=0, help_text="0 表示不限量")
    claimed_count = models.PositiveIntegerField(default=0, editable=False)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """声明优惠券的后台名称和默认展示顺序。"""

        verbose_name = "优惠券"
        verbose_name_plural = "优惠券"
        ordering = ("-created_at",)

    def __str__(self):
        """返回优惠券名称和业务券码。"""

        return f"{self.name}（{self.code}）"

    @property
    def is_available(self):
        """判断优惠券是否处于可领取的有效期与库存范围。"""

        now = timezone.now()
        has_stock = self.total_quantity == 0 or self.claimed_count < self.total_quantity
        return self.is_active and self.valid_from <= now < self.valid_until and has_stock

    @property
    def remaining_quantity(self):
        """返回剩余可领取数量；不限量时返回 ``None``。"""

        if self.total_quantity == 0:
            return None
        return max(0, self.total_quantity - self.claimed_count)


class UserCoupon(models.Model):
    """某用户领取到的单张优惠券实例。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="coupons")
    # 依赖流向：用户领券记录 -> 优惠券规则；规则停用后仍保留领取与核销审计。
    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT, related_name="user_coupons")
    claimed_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """声明用户领券关系的唯一约束和展示顺序。"""

        verbose_name = "用户优惠券"
        verbose_name_plural = "用户优惠券"
        constraints = [
            models.UniqueConstraint(fields=("user", "coupon"), name="commerce_unique_user_coupon")
        ]
        ordering = ("-claimed_at",)

    def __str__(self):
        """返回用户和优惠券的可读关系。"""

        return f"{self.user} - {self.coupon.name}"

    @property
    def status(self):
        """计算可用、已用或过期状态，供页面和后台展示。"""

        if self.used_at:
            return "used"
        if timezone.now() >= self.coupon.valid_until:
            return "expired"
        return "available"


class Order(models.Model):
    """交易订单及其不可变金额、地址和支付状态快照。

    数据流：结算服务创建待付款订单并预占商品可售库存；payments 服务确认支付；
    后台/用户服务推进物流状态。``stock_reserved`` 记录该订单是否已经通过预占占用库存，
    用于兼容上线前创建的旧待付款订单：旧订单仍在支付确认时按原规则扣减一次库存。
    """

    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_SHIPPED = "shipped"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUND = "refund"
    STATUS_CHOICES = (
        (STATUS_PENDING, "待付款"),
        (STATUS_PAID, "待发货"),
        (STATUS_SHIPPED, "待收货"),
        (STATUS_COMPLETED, "已完成"),
        (STATUS_CANCELLED, "已取消"),
        (STATUS_REFUND, "退款/售后"),
    )

    # 兼容现有调用方；实际渠道编码定义在 core.constants。
    PAYMENT_MOCK = PAYMENT_METHOD_MOCK
    PAYMENT_ALIPAY = PAYMENT_METHOD_ALIPAY
    PAYMENT_WECHAT = PAYMENT_METHOD_WECHAT
    PAYMENT_CHOICES = PAYMENT_METHOD_CHOICES

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    order_no = models.CharField(max_length=32, unique=True, blank=True, null=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pay_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    address_text = models.CharField(max_length=255, blank=True)
    coupon = models.ForeignKey(
        "commerce.UserCoupon",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="orders",
    )
    payment_method = models.CharField(max_length=20, choices=PAYMENT_CHOICES, default=PAYMENT_MOCK)
    payment_no = models.CharField(max_length=64, blank=True, db_index=True)
    # 依赖流向：结算服务 -> 本字段 -> 取消/超时与支付服务；禁止由页面或后台直接编辑。
    stock_reserved = models.BooleanField("已预占库存", default=False, editable=False)
    # 依赖流向：支付回调 -> 本字段 -> 后台人工对账。取消后的迟到成功支付只能置此标记并
    # 写入支付审计，绝不能重新把订单推进为已付款、发货或再次扣减库存。
    payment_reconciliation_required = models.BooleanField(
        "待支付人工对账",
        default=False,
        editable=False,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """声明订单的后台名称和按创建时间倒序的展示规则。"""

        verbose_name = "订单"
        verbose_name_plural = "订单"
        ordering = ("-created_at",)

    def __str__(self):
        """返回业务订单号，尚未生成时退回数据库编号。"""

        return self.order_no or f"订单 {self.id}"


class OrderItem(models.Model):
    """订单内的商品快照，防止商品后续修改影响历史订单。"""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL, blank=True, null=True)
    product_name = models.CharField(max_length=200)
    product_image = models.CharField(max_length=255, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        """声明订单项的后台展示名称。"""

        verbose_name = "订单明细"
        verbose_name_plural = "订单明细"

    def __str__(self):
        """返回订单项的商品名称快照。"""

        return self.product_name


class RefundRequest(models.Model):
    """订单售后/退款申请。

    数据流：用户申请 → 订单进入 refund → 管理员审核 → 已验证的真实支付渠道退款回调
    才能完成退款并回补库存。当前未接入真实退款渠道，因此申请只能停留在待审核/已同意/已拒绝。
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = (
        (STATUS_PENDING, "待审核"),
        (STATUS_APPROVED, "已同意"),
        (STATUS_COMPLETED, "退款完成"),
        (STATUS_REJECTED, "已拒绝"),
    )

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="refund_request")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refund_requests")
    refund_no = models.CharField(max_length=40, unique=True)
    reason = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    order_status_before = models.CharField(
        max_length=20,
        choices=Order.STATUS_CHOICES,
        default=Order.STATUS_PAID,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """声明售后申请的后台名称和默认展示顺序。"""

        verbose_name = "退款/售后"
        verbose_name_plural = "退款/售后"
        ordering = ("-created_at",)
