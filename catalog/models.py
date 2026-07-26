"""商品目录领域的数据模型。

依赖流向：catalog.models -> Django ORM 与认证用户模型。购物车、订单等下游
领域通过外键引用 ``catalog.Product``；外部爬虫则通过 ``ProductSource`` 建立
可追溯、可幂等的商品来源关系。
"""

# Python 精确数值依赖：评分、金额等十进制字段的默认值不能使用二进制浮点数。
from decimal import Decimal

# 依赖流向：catalog.models -> Django 配置、字段验证、ORM 与时区工具。
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone


class Category(models.Model):
    """表示可嵌套的商品分类树，当前 UI 主要展示一级和二级分类。

    ``parent_scope`` 是由 ``parent`` 派生的内部唯一性键：根分类使用 ``0``，子分类
    使用父分类的主键。它避免 MySQL 对 ``NULL`` 组合唯一约束可重复的问题，使同一父级
    下的分类名称在数据库层也保持唯一；分类导入服务据此安全处理并发创建。
    """

    name = models.CharField("分类名称", max_length=100)
    icon = models.CharField("图标名称", max_length=50, blank=True)
    # 依赖流向：子分类 -> 本模型父分类；删除父级时保留子分类并清空父级引用。
    parent = models.ForeignKey(
        "self",
        verbose_name="父级分类",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="children",
    )
    # 依赖流向：由 ``parent`` 派生，仅用于 ``name + parent_scope`` 的数据库唯一约束；
    # 业务读取和展示仍只应使用 ``parent`` 外键。
    parent_scope = models.PositiveBigIntegerField("父级唯一性键", default=0, editable=False)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("是否显示", default=True)

    class Meta:
        """定义分类的后台名称和默认展示顺序。"""

        verbose_name = "商品分类"
        verbose_name_plural = "商品分类"
        ordering = ("sort_order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("name", "parent_scope"),
                name="catalog_category_parent_name_unique",
            )
        ]

    def save(self, *args, **kwargs):
        """在每次模型保存时同步内部父级唯一性键，避免后台改父级后留下旧键。"""

        self.parent_scope = self.parent_id or 0
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"parent", "parent_id"}.intersection(update_fields):
            kwargs["update_fields"] = {*update_fields, "parent_scope"}
        return super().save(*args, **kwargs)

    def __str__(self):
        """返回分类名称，供后台和日志显示。"""

        return self.name


@receiver(pre_delete, sender=Category)
def clear_deleted_category_parent_scope(sender, instance, using, **kwargs):
    """父分类删除时同步其直接子分类的内部唯一性键。

    ``on_delete=SET_NULL`` 由 Django 的收集器直接执行 SQL，不会调用 ``Category.save``。
    因此必须在删除前同时将子分类的 ``parent`` 和 ``parent_scope`` 置为根级值，避免
    留下已无父级却仍带旧父级唯一性键的数据。若子分类名称与现有根分类冲突，数据库唯一
    约束会拒绝删除并保留整棵树，管理员应先重命名或重新归类。
    """

    Category.objects.using(using).filter(parent_id=instance.id).update(parent=None, parent_scope=0)


class Product(models.Model):
    """表示商城中可浏览、可加入购物车和可下单的基础商品。

    ``stock`` 是商城当前可售库存：创建待付款订单时会先预占并扣减，取消/超时才归还。
    ``source_stock`` 仅保存最近一次爬虫/商家来源报告的参考值，不能自动覆盖 ``stock``，
    以免重导入抹掉真实订单的库存变化。
    """

    # 依赖流向：商品 -> 商品分类；已有商品时保护分类，避免删除分类连带清空商品主数据。
    # 分类不再展示时应改为 ``is_active=False``，由运营停用替代物理删除。
    category = models.ForeignKey(Category, verbose_name="分类", on_delete=models.PROTECT)
    name = models.CharField("商品名称", max_length=200)
    brand = models.CharField("品牌", max_length=100, blank=True)
    price = models.DecimalField(
        "现价",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    original_price = models.DecimalField("原价", max_digits=10, decimal_places=2, blank=True, null=True)
    image = models.ImageField("主图", upload_to="products/", blank=True)
    stock = models.PositiveIntegerField("可售库存", default=100)
    # 依赖流向：外部 JSON 导入 -> 来源参考库存；它只供人工对账，不能参与前台库存判断。
    source_stock = models.PositiveIntegerField("来源参考库存", blank=True, null=True)
    sales = models.PositiveIntegerField("销量", default=0)
    rating = models.DecimalField("评分", max_digits=3, decimal_places=1, default=Decimal("4.8"))
    review_count = models.PositiveIntegerField("评价数", default=0)
    description = models.TextField("商品描述", blank=True)
    # ``specs`` 保持 JSON 文本兼容旧模板；后续 SKU 重构前不改变其读取方式。
    specs = models.TextField("参数信息", blank=True)
    is_hot = models.BooleanField("热门商品", default=False)
    is_new = models.BooleanField("新品", default=False)
    is_recommended = models.BooleanField("推荐商品", default=False)
    is_active = models.BooleanField("是否上架", default=True)
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        """定义商品的后台名称和默认倒序展示规则。"""

        verbose_name = "商品"
        verbose_name_plural = "商品"
        ordering = ("-created_at",)

    def __str__(self):
        """返回商品名称，供后台、订单快照和调试日志使用。"""

        return self.name

    @property
    def savings(self):
        """计算原价高于现价时的节省金额；没有优惠则返回 ``None``。"""

        if self.original_price and self.original_price > self.price:
            return self.original_price - self.price
        return None


class ProductImage(models.Model):
    """保存商品详情页可排序展示的附加图片。"""

    # 依赖流向：商品图片 -> 商品；删除商品时同步删除其图片数据库记录。
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE, related_name="images")
    image = models.ImageField("图片", upload_to="products/gallery/")
    is_main = models.BooleanField("是否主图", default=False)
    sort_order = models.PositiveIntegerField("排序", default=0)

    class Meta:
        """定义商品图片的后台名称和前端主图优先展示顺序。"""

        verbose_name = "商品图片"
        verbose_name_plural = "商品图片"
        ordering = ("-is_main", "sort_order", "id")

    def __str__(self):
        """返回包含商品名称的图片说明。"""

        return f"{self.product.name} 图片"


class ProductSource(models.Model):
    """记录商品的外部数据源身份，支持爬虫 JSON 的幂等导入和来源追溯。"""

    SOURCE_JD = "jd"
    SOURCE_MANUAL = "manual"
    SOURCE_MERCHANT = "merchant"
    SOURCE_CHOICES = (
        (SOURCE_JD, "京东清洗数据"),
        (SOURCE_MANUAL, "后台手工录入"),
        (SOURCE_MERCHANT, "未来商家平台"),
    )

    # 依赖流向：来源记录 -> 本地商品；同一商品可有多个来源记录。
    product = models.ForeignKey(Product, verbose_name="本地商品", on_delete=models.CASCADE, related_name="sources")
    source_type = models.CharField("来源类型", max_length=32, choices=SOURCE_CHOICES)
    external_id = models.CharField("外部商品 ID", max_length=128)
    source_url = models.URLField("来源链接", max_length=500, blank=True)
    # 仅保存清洗后的来源元数据摘要，不能保存用户个人数据或敏感凭据。
    source_payload = models.JSONField("来源元数据", default=dict, blank=True)
    first_imported_at = models.DateTimeField("首次导入时间", auto_now_add=True)
    last_imported_at = models.DateTimeField("最后导入时间", auto_now=True)

    class Meta:
        """保证来源类型与外部 ID 的组合全局唯一，作为幂等导入键。"""

        verbose_name = "商品来源"
        verbose_name_plural = "商品来源"
        ordering = ("-last_imported_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("source_type", "external_id"),
                name="catalog_source_external_unique",
            )
        ]

    def __str__(self):
        """返回来源类型和外部 ID，不输出完整来源元数据。"""

        return f"{self.get_source_type_display()}：{self.external_id}"


class Review(models.Model):
    """保存用户或匿名用户对某个商品提交的评价。"""

    # 依赖流向：评价 -> 商品；商品删除时删除评价，用户删除时保留匿名快照。
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    username = models.CharField("用户名", max_length=50)
    rating = models.PositiveSmallIntegerField(
        "评分",
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    content = models.TextField("评价内容")
    is_anonymous = models.BooleanField("匿名", default=False)
    created_at = models.DateTimeField("创建时间", default=timezone.now)

    class Meta:
        """定义评价的后台名称和最新优先展示顺序。"""

        verbose_name = "商品评价"
        verbose_name_plural = "商品评价"
        ordering = ("-created_at",)

    def __str__(self):
        """返回用于后台识别的评价人和商品名称。"""

        return f"{self.username} - {self.product.name}"


class BrowsingHistory(models.Model):
    """记录用户最近浏览的商品，用于个人历史页和后续推荐能力。"""

    # 依赖流向：浏览记录 -> 用户与商品；任一主体删除时清理关联记录。
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.CASCADE,
        related_name="browsing_history",
    )
    product = models.ForeignKey(Product, verbose_name="商品", on_delete=models.CASCADE)
    viewed_at = models.DateTimeField("浏览时间", auto_now=True)

    class Meta:
        """保证每位用户对每个商品只有一条可更新时间的浏览记录。"""

        verbose_name = "浏览记录"
        verbose_name_plural = "浏览记录"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "product"),
                name="catalog_history_user_product_unique",
            )
        ]
        ordering = ("-viewed_at",)
