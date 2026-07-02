from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class Category(models.Model):
    name = models.CharField("分类名称", max_length=100)
    icon = models.CharField("图标名称", max_length=50, blank=True)
    parent = models.ForeignKey(
        "self",
        verbose_name="父级分类",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="children",
    )
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("是否显示", default=True)

    class Meta:
        verbose_name = "商品分类"
        verbose_name_plural = "商品分类"
        ordering = ("sort_order", "id")

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField("商品名称", max_length=200)
    category = models.ForeignKey(Category, verbose_name="分类", on_delete=models.CASCADE)
    brand = models.CharField("品牌", max_length=100, blank=True)
    price = models.DecimalField("现价", max_digits=10, decimal_places=2)
    original_price = models.DecimalField("原价", max_digits=10, decimal_places=2, blank=True, null=True)
    image = models.ImageField("主图", upload_to="products/", blank=True)
    stock = models.IntegerField("库存", default=100)
    sales = models.PositiveIntegerField("销量", default=0)
    rating = models.DecimalField("评分", max_digits=3, decimal_places=1, default=4.8)
    review_count = models.PositiveIntegerField("评价数", default=0)
    description = models.TextField("商品描述", blank=True)
    specs = models.TextField("参数信息", blank=True)
    is_hot = models.BooleanField("热门商品", default=False)
    is_new = models.BooleanField("新品", default=False)
    is_recommended = models.BooleanField("推荐商品", default=False)
    is_active = models.BooleanField("是否上架", default=True)
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品"
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    @property
    def savings(self):
        if self.original_price and self.original_price > self.price:
            return self.original_price - self.price
        return None


class ProductImage(models.Model):
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE, related_name="images")
    image = models.ImageField("图片", upload_to="products/gallery/")
    is_main = models.BooleanField("是否主图", default=False)
    sort_order = models.PositiveIntegerField("排序", default=0)

    class Meta:
        verbose_name = "商品图片"
        verbose_name_plural = "商品图片"
        ordering = ("sort_order", "id")

    def __str__(self):
        return f"{self.product.name} 图片"


class CartItem(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField("数量", default=1)
    color = models.CharField("颜色", max_length=50, blank=True)
    is_selected = models.BooleanField("是否选中", default=True)
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "购物车项"
        verbose_name_plural = "购物车项"
        unique_together = ("user", "product", "color")

    def __str__(self):
        return f"{self.user} - {self.product} x {self.quantity}"

    @property
    def subtotal(self):
        return self.product.price * self.quantity


class Address(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="addresses")
    receiver = models.CharField("收货人", max_length=50)
    phone = models.CharField("手机号", max_length=20)
    province = models.CharField("省份", max_length=50, blank=True)
    city = models.CharField("城市", max_length=50, blank=True)
    district = models.CharField("区县", max_length=50, blank=True)
    detail = models.CharField("详细地址", max_length=255)
    is_default = models.BooleanField("默认地址", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "收货地址"
        verbose_name_plural = "收货地址"

    def __str__(self):
        return f"{self.receiver} {self.phone}"


class Order(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_SHIPPED = "shipped"
    STATUS_COMPLETED = "completed"
    STATUS_REFUND = "refund"

    STATUS_CHOICES = (
        (STATUS_PENDING, "待付款"),
        (STATUS_PAID, "待发货"),
        (STATUS_SHIPPED, "待收货"),
        (STATUS_COMPLETED, "已完成"),
        (STATUS_REFUND, "退款/售后"),
    )

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE)
    order_no = models.CharField("订单号", max_length=32, unique=True, blank=True, null=True)
    total_amount = models.DecimalField("商品总额", max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField("优惠金额", max_digits=10, decimal_places=2, default=0)
    shipping_fee = models.DecimalField("运费", max_digits=10, decimal_places=2, default=0)
    pay_amount = models.DecimalField("实付金额", max_digits=10, decimal_places=2, default=0)
    address_text = models.CharField("收货地址快照", max_length=255, blank=True)
    status = models.CharField("订单状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    paid_at = models.DateTimeField("支付时间", blank=True, null=True)

    class Meta:
        verbose_name = "订单"
        verbose_name_plural = "订单"
        ordering = ("-created_at",)

    def __str__(self):
        return self.order_no or f"订单 {self.id}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, verbose_name="订单", on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.SET_NULL, blank=True, null=True)
    product_name = models.CharField("商品名称快照", max_length=200)
    product_image = models.CharField("商品图片快照", max_length=255, blank=True)
    price = models.DecimalField("下单单价", max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField("数量", default=1)
    subtotal = models.DecimalField("小计", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "订单明细"
        verbose_name_plural = "订单明细"

    def __str__(self):
        return self.product_name


class Favorite(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "收藏"
        verbose_name_plural = "收藏"
        unique_together = ("user", "product")

    def __str__(self):
        return f"{self.user} 收藏 {self.product}"
