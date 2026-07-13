from django.contrib.auth.models import User   # User`：Django 自带用户表
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
    price = models.DecimalField("现价", max_digits=10, decimal_places=2)     # 数字总长度为10位，小数位数为2位
    original_price = models.DecimalField("原价", max_digits=10, decimal_places=2, blank=True, null=True)    # blank后台管理系统（表单）可以不选，null为数据库可以为空，而不是空字符串''
    image = models.ImageField("主图", upload_to="products/", blank=True)   # image`：主图，上传到 `media/products/`
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
    # 元组，自定义，用来传choices参数
    PURPOSE_CHOICES = (
        (PURPOSE_REGISTER, "手机号注册"),  # 数据库存 "register"，后台显示 "手机号注册"
        (PURPOSE_LOGIN, "手机号登录"),  # 数据库存 "login"，后台显示 "手机号登录"
        (PURPOSE_BIND, "绑定手机号"),  # 数据库存 "bind"，后台显示 "绑定手机号"
    )

    phone = models.CharField("手机号", max_length=20, db_index=True)    # db_index=True数据库里为这一列建索引，按手机号查询时速度更快
    code = models.CharField("验证码", max_length=6)
    purpose = models.CharField("用途", max_length=20, choices=PURPOSE_CHOICES)   # 限制在register、login、bind三个中选择
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    expires_at = models.DateTimeField("过期时间")
    used_at = models.DateTimeField("使用时间", blank=True, null=True)
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
