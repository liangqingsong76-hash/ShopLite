from django.contrib.auth.models import User   # User`：Django 自带用户表
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models   # models`：Django ORM 字段和模型基类
from django.utils import timezone    # timezone`：生成带时区的时间


# 商品分类
class Category(models.Model):
    name = models.CharField("分类名称", max_length=100)
    icon = models.CharField("图标名称", max_length=50, blank=True)   # 可以为空
    # ForeignKey多对一（多个商品属于一个分类）
    parent = models.ForeignKey(
        "self",  # 指向自身（Category 表自己）
        verbose_name="父级分类",  # Admin 后台显示的字段名
        on_delete=models.SET_NULL,  # 父级被删时，子级的 parent 设为 null
        blank=True,  # 表单里可以不填
        null=True,  # 数据库里允许为空（顶级分类没有父级）
        related_name="children",  # 反查：从父级找到所有子级
    )
    sort_order = models.PositiveIntegerField("排序", default=0)   # 排序
    is_active = models.BooleanField("是否显示", default=True)    # 是否显示

    class Meta:
        verbose_name = "商品分类"    # verbose_name` 是后台显示名称，Admin 后台里这个模型的单数显示名称
        verbose_name_plural = "商品分类"   # Admin 后台里这个模型的复数显示名称
        ordering = ("sort_order", "id")  # 表示默认按排序值和 id 排序

    # __str__()` 返回分类名，方便后台和调试显示，决定了这个对象以什么字符串形式展示
    def __str__(self):
        return self.name


# 商品
class Product(models.Model):
    name = models.CharField("商品名称", max_length=200)
    category = models.ForeignKey(Category, verbose_name="分类", on_delete=models.CASCADE)   # on_delete=models.CASCADE意思是父级分类删除，子级连带删除
    brand = models.CharField("品牌", max_length=100, blank=True)
    price = models.DecimalField("现价", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    original_price = models.DecimalField("原价", max_digits=10, decimal_places=2, blank=True, null=True)    # blank后台管理系统（表单）可以不选，null为数据库可以为空，而不是空字符串''
    image = models.ImageField("主图", upload_to="products/", blank=True)   # image`：主图，上传到 `media/products/`
    stock = models.PositiveIntegerField("库存", default=100)
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
    updated_at = models.DateTimeField("更新时间", auto_now=True)   # 每次保存记录时自动更新为当前时间

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品"
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    # @property内置装饰器,让方法像属性一样调用，不用加括号,内置自动计算，如果有原价，计算原价与现价差值
    '''
    没有 @property：
    product.savings()    # 需要加括号调用
    有 @property：
    product.savings     # 像访问普通属性一样，不用加括号
    '''
    @property
    def savings(self):
        if self.original_price and self.original_price > self.price:
            return self.original_price - self.price
        return None


# 商品多图
class ProductImage(models.Model):
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE, related_name="images")    # related_name="XXX",就是给反向查询取了一个好记的名字，让你可以从一个商品（product）直接通过 product.XXX.all() 找到它关联的所有图片
    image = models.ImageField("图片", upload_to="products/gallery/")
    is_main = models.BooleanField("是否主图", default=False)
    sort_order = models.PositiveIntegerField("排序", default=0)

    class Meta:
        verbose_name = "商品图片"
        verbose_name_plural = "商品图片"
        ordering = ("sort_order", "id")

    def __str__(self):
        return f"{self.product.name} 图片"


# 用户资料
class UserProfile(models.Model):
    user = models.OneToOneField(User, verbose_name="用户", on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField("手机号", max_length=20, unique=True, blank=True, null=True)    # unique=True,必须唯一，不能有重复手机号
    phone_verified_at = models.DateTimeField("手机号验证时间", blank=True, null=True)
    avatar = models.ImageField("头像", upload_to="avatars/%Y/%m/", blank=True)
    bio = models.CharField("个人简介", max_length=160, blank=True)
    marketing_notifications = models.BooleanField("接收优惠通知", default=True)
    order_notifications = models.BooleanField("接收订单通知", default=True)
    '''
    区别	            auto_now_add=True	default=timezone.now
    创建时自动设时间	✅	                ✅
    创建时手动传值	    ❌ 不能，强制用当前时间	✅ 可以，比如数据迁移时保留原时间
    Admin 表单里显示	❌ 隐藏该字段，不可编辑	✅ 显示该字段，可编辑
    '''
    created_at = models.DateTimeField("创建时间", auto_now_add=True)    # 只在第一次创建记录时自动设为当前时间，之后不再更新(在数据迁移时不会保留旧数据的时间戳，它一律使用迁移脚本执行时的当前时间)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self):
        return f"{self.user.username} {self.phone or ''}".strip()   # 如果 self.phone 是空或 None，就换成空字符串 ''，.strip() 去掉字符串首尾的空白字符


# 手机验证码
class PhoneVerificationCode(models.Model):
    # 自定义常量，方便随时修改
    PURPOSE_REGISTER = "register"  # 用户注册
    PURPOSE_LOGIN = "login"  # 用户登录
    PURPOSE_BIND = "bind"  # 绑定手机号
    PURPOSE_RESET = "reset"  # 重置密码
    # 元组，自定义，用来传choices参数
    PURPOSE_CHOICES = (
        (PURPOSE_REGISTER, "手机号注册"),  # 数据库存 "register"，后台显示 "手机号注册"
        (PURPOSE_LOGIN, "手机号登录"),  # 数据库存 "login"，后台显示 "手机号登录"
        (PURPOSE_BIND, "绑定手机号"),  # 数据库存 "bind"，后台显示 "绑定手机号"
        (PURPOSE_RESET, "重置密码"),
    )

    phone = models.CharField("手机号", max_length=20, db_index=True)    # db_index=True数据库里为这一列建索引，按手机号查询时速度更快
    # 只保存验证码摘要，避免数据库泄露后暴露仍有效的短信验证码。
    code = models.CharField("验证码摘要", max_length=64)
    purpose = models.CharField("用途", max_length=20, choices=PURPOSE_CHOICES)   # 限制在register、login、bind三个中选择
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    expires_at = models.DateTimeField("过期时间")
    used_at = models.DateTimeField("使用时间", blank=True, null=True)
    attempt_count = models.PositiveSmallIntegerField("校验失败次数", default=0)
    last_attempt_at = models.DateTimeField("最后校验时间", blank=True, null=True)
    sent_to_backend = models.BooleanField("已提交短信通道", default=False)

    class Meta:
        verbose_name = "手机验证码"
        verbose_name_plural = "手机验证码"
        ordering = ("-created_at",)
        # 给 phone + purpose + created_at 三个字段建联合索引 — 因为最常见的查询是"查某个手机号某用途的最新验证码"，联合索引让这个查询极快
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"]),
        ]

    def __str__(self):
        return f"{self.phone} {self.purpose}"

    # 判断验证码是否过期
    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    # 判断验证码是否使用
    @property
    def is_used(self):
        return self.used_at is not None


# 购物车项
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
        unique_together = ("user", "product", "color")   # 同一个用户、同一个商品、同一种颜色，在购物车里只能有一条记录

    def __str__(self):
        return f"{self.user} - {self.product} x {self.quantity}"

    @property
    def subtotal(self):
        return self.product.price * self.quantity


# 收货地址
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


# 订单
class Order(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_SHIPPED = "shipped"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUND = "refund"

    PAYMENT_MOCK = "mock"
    PAYMENT_ALIPAY = "alipay"
    PAYMENT_WECHAT = "wechat"
    PAYMENT_CHOICES = (
        (PAYMENT_MOCK, "模拟支付（开发测试）"),
        (PAYMENT_ALIPAY, "支付宝（暂未开放）"),
        (PAYMENT_WECHAT, "微信支付（暂未开放）"),
    )

    STATUS_CHOICES = (
        (STATUS_PENDING, "待付款"),
        (STATUS_PAID, "待发货"),
        (STATUS_SHIPPED, "待收货"),
        (STATUS_COMPLETED, "已完成"),
        (STATUS_CANCELLED, "已取消"),
        (STATUS_REFUND, "退款/售后"),
    )

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE)
    order_no = models.CharField("订单号", max_length=32, unique=True, blank=True, null=True)
    total_amount = models.DecimalField("商品总额", max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField("优惠金额", max_digits=10, decimal_places=2, default=0)
    shipping_fee = models.DecimalField("运费", max_digits=10, decimal_places=2, default=0)
    pay_amount = models.DecimalField("实付金额", max_digits=10, decimal_places=2, default=0)
    address_text = models.CharField("收货地址快照", max_length=255, blank=True)
    coupon = models.ForeignKey(
        "UserCoupon",
        verbose_name="使用优惠券",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="orders",
    )
    payment_method = models.CharField("支付方式", max_length=20, choices=PAYMENT_CHOICES, default=PAYMENT_MOCK)
    payment_no = models.CharField("支付流水号", max_length=64, blank=True, db_index=True)
    status = models.CharField("订单状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    paid_at = models.DateTimeField("支付时间", blank=True, null=True)

    class Meta:
        verbose_name = "订单"
        verbose_name_plural = "订单"
        ordering = ("-created_at",)

    def __str__(self):
        return self.order_no or f"订单 {self.id}"


# 订单明细
class OrderItem(models.Model):
    order = models.ForeignKey(Order, verbose_name="订单", on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.SET_NULL, blank=True, null=True)    # 商品被删，商品ID置空（不删订单）
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


# 收藏
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


# 商品评价
class Review(models.Model):
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.SET_NULL, blank=True, null=True)
    username = models.CharField("用户名", max_length=50)
    rating = models.PositiveSmallIntegerField("评分", default=5)
    content = models.TextField("评价内容")
    is_anonymous = models.BooleanField("匿名", default=False)
    created_at = models.DateTimeField("创建时间", default=timezone.now)

    class Meta:
        verbose_name = "商品评价"
        verbose_name_plural = "商品评价"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.username} - {self.product.name}"


class Coupon(models.Model):
    TYPE_FIXED = "fixed"
    TYPE_PERCENT = "percent"
    TYPE_CHOICES = ((TYPE_FIXED, "满减券"), (TYPE_PERCENT, "折扣券"))

    code = models.CharField("券码", max_length=40, unique=True)
    name = models.CharField("优惠券名称", max_length=100)
    description = models.CharField("使用说明", max_length=255, blank=True)
    discount_type = models.CharField("优惠类型", max_length=20, choices=TYPE_CHOICES, default=TYPE_FIXED)
    value = models.DecimalField("优惠值", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    minimum_spend = models.DecimalField("最低消费", max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    total_quantity = models.PositiveIntegerField("发行数量", default=0, help_text="0 表示不限量")
    claimed_count = models.PositiveIntegerField("已领取数量", default=0, editable=False)
    valid_from = models.DateTimeField("生效时间", default=timezone.now)
    valid_until = models.DateTimeField("失效时间")
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "优惠券"
        verbose_name_plural = "优惠券"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name}（{self.code}）"

    @property
    def is_available(self):
        now = timezone.now()
        has_stock = self.total_quantity == 0 or self.claimed_count < self.total_quantity
        return self.is_active and self.valid_from <= now < self.valid_until and has_stock

    @property
    def remaining_quantity(self):
        if self.total_quantity == 0:
            return None
        return max(0, self.total_quantity - self.claimed_count)


class UserCoupon(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="coupons")
    coupon = models.ForeignKey(Coupon, verbose_name="优惠券", on_delete=models.CASCADE, related_name="user_coupons")
    claimed_at = models.DateTimeField("领取时间", auto_now_add=True)
    used_at = models.DateTimeField("使用时间", blank=True, null=True)

    class Meta:
        verbose_name = "用户优惠券"
        verbose_name_plural = "用户优惠券"
        constraints = [models.UniqueConstraint(fields=("user", "coupon"), name="unique_user_coupon")]
        ordering = ("-claimed_at",)

    def __str__(self):
        return f"{self.user} - {self.coupon.name}"

    @property
    def status(self):
        if self.used_at:
            return "used"
        if timezone.now() >= self.coupon.valid_until:
            return "expired"
        return "available"


class PaymentTransaction(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = ((STATUS_PENDING, "处理中"), (STATUS_SUCCESS, "成功"), (STATUS_FAILED, "失败"))

    order = models.ForeignKey(Order, verbose_name="订单", on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField("支付渠道", max_length=20, choices=Order.PAYMENT_CHOICES)
    transaction_no = models.CharField("渠道流水号", max_length=64, unique=True)
    amount = models.DecimalField("金额", max_digits=10, decimal_places=2)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    raw_payload = models.JSONField("回调摘要", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    completed_at = models.DateTimeField("完成时间", blank=True, null=True)

    class Meta:
        verbose_name = "支付流水"
        verbose_name_plural = "支付流水"
        ordering = ("-created_at",)


class RefundRequest(models.Model):
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

    order = models.OneToOneField(Order, verbose_name="订单", on_delete=models.PROTECT, related_name="refund_request")
    user = models.ForeignKey(User, verbose_name="申请用户", on_delete=models.PROTECT, related_name="refund_requests")
    refund_no = models.CharField("售后编号", max_length=40, unique=True)
    reason = models.CharField("退款原因", max_length=100)
    description = models.TextField("问题描述", blank=True)
    amount = models.DecimalField("退款金额", max_digits=10, decimal_places=2)
    order_status_before = models.CharField("申请前订单状态", max_length=20, choices=Order.STATUS_CHOICES, default=Order.STATUS_PAID)
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    admin_note = models.TextField("后台备注", blank=True)
    created_at = models.DateTimeField("申请时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    completed_at = models.DateTimeField("完成时间", blank=True, null=True)

    class Meta:
        verbose_name = "退款/售后"
        verbose_name_plural = "退款/售后"
        ordering = ("-created_at",)


class Notification(models.Model):
    TYPE_SYSTEM = "system"
    TYPE_ORDER = "order"
    TYPE_COUPON = "coupon"
    TYPE_CHOICES = ((TYPE_SYSTEM, "系统"), (TYPE_ORDER, "订单"), (TYPE_COUPON, "优惠券"))

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="notifications")
    category = models.CharField("类型", max_length=20, choices=TYPE_CHOICES, default=TYPE_SYSTEM)
    title = models.CharField("标题", max_length=120)
    content = models.TextField("内容")
    link = models.CharField("站内链接", max_length=255, blank=True)
    is_read = models.BooleanField("已读", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "站内通知"
        verbose_name_plural = "站内通知"
        ordering = ("-created_at",)

    @property
    def safe_link(self):
        return self.link if self.link.startswith("/") and not self.link.startswith("//") else "/notifications/"


class BrowsingHistory(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="browsing_history")
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE)
    viewed_at = models.DateTimeField("浏览时间", auto_now=True)

    class Meta:
        verbose_name = "浏览记录"
        verbose_name_plural = "浏览记录"
        constraints = [models.UniqueConstraint(fields=("user", "product"), name="unique_user_product_history")]
        ordering = ("-viewed_at",)
