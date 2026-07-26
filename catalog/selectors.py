"""商品目录的只读查询服务。

依赖流向：storefront、API 与其他领域调用本模块获取商品数据；本模块只读取
``catalog`` 模型，不写入数据库，也不依赖订单、支付或账户业务服务。
"""

# Python 标准库依赖：解析旧商品参数 JSON 文本。
import json

# Django 查询依赖：聚合品牌计数与获取不存在时返回 404 的商品详情。
from django.db.models import Count
from django.shortcuts import get_object_or_404

# 本领域模型依赖：查询服务只读商品目录模型。
from .models import Category, Product


def active_product_queryset():
    """返回带分类预加载、仅包含上架商品的基础查询集。"""

    return Product.objects.select_related("category").filter(is_active=True)


def active_categories(*, with_children=False):
    """返回启用的一级分类，可选地附加 ``subcategories`` 临时属性。"""

    categories = list(
        Category.objects.filter(is_active=True, parent__isnull=True).order_by("sort_order", "id")
    )
    if not with_children:
        return categories

    category_ids = [category.id for category in categories]
    children = Category.objects.filter(is_active=True, parent_id__in=category_ids).order_by(
        "parent_id",
        "sort_order",
        "id",
    )
    children_by_parent = {}
    for child in children:
        children_by_parent.setdefault(child.parent_id, []).append(child)
    for category in categories:
        category.subcategories = children_by_parent.get(category.id, [])
    return categories


def list_products(
    limit=None,
    *,
    hot=False,
    new=False,
    recommended=False,
    category_name=None,
    parent_category_name=None,
    keyword=None,
    brand=None,
    price_min=None,
    price_max=None,
    sort_by=None,
):
    """按商品页筛选条件返回已物化列表，兼容首页和详情页等现有调用方。"""

    return list(
        product_queryset(
            limit,
            hot=hot,
            new=new,
            recommended=recommended,
            category_name=category_name,
            parent_category_name=parent_category_name,
            keyword=keyword,
            brand=brand,
            price_min=price_min,
            price_max=price_max,
            sort_by=sort_by,
        )
    )


def product_queryset(
    limit=None,
    *,
    hot=False,
    new=False,
    recommended=False,
    category_name=None,
    parent_category_name=None,
    keyword=None,
    brand=None,
    price_min=None,
    price_max=None,
    sort_by=None,
):
    """返回可继续分页的上架商品查询集，筛选与 ``list_products`` 保持一致。"""

    products = active_product_queryset()
    if hot:
        products = products.filter(is_hot=True)
    if new:
        products = products.filter(is_new=True)
    if recommended:
        products = products.filter(is_recommended=True)
    if category_name:
        products = _filter_by_category_path(products, category_name, parent_category_name)
    if keyword:
        products = products.filter(name__icontains=keyword)
    if brand:
        products = products.filter(brand=brand)
    if price_min is not None:
        products = products.filter(price__gte=price_min)
    if price_max is not None:
        products = products.filter(price__lte=price_max)
    products = products.order_by(*_product_ordering(sort_by))
    if limit:
        products = products[:limit]
    return products


def _filter_by_category_path(products, category_name, parent_category_name=None):
    """按分类层级筛选，并包含目标分类的全部启用后代。

    导入契约允许三级分类，而 storefront 的导航当前只展示前两级；因此点击一级或二级
    分类时都必须能找到归入更深层分类的商品。``parent_category_name`` 仍用于消除不同
    父级下同名子分类的歧义。
    """

    matches = Category.objects.filter(is_active=True, name=category_name)
    if parent_category_name:
        matches = matches.filter(parent__is_active=True, parent__name=parent_category_name)
        return products.filter(category_id__in=_category_and_descendant_ids(matches))

    top_level_ids = list(matches.filter(parent__isnull=True).values_list("id", flat=True))
    if top_level_ids:
        return products.filter(category_id__in=_category_and_descendant_ids(top_level_ids))
    return products.filter(category_id__in=_category_and_descendant_ids(matches))


def _category_and_descendant_ids(categories_or_ids):
    """返回分类自身及所有启用后代的主键，支持任意深度且不混淆同名节点。

    当前分类树规模较小，使用一次全量轻量查询构建父子映射，比数据库方言相关的递归 CTE
    更容易在本地 MySQL 和测试数据库中保持一致。未来分类量明显增长时可改为物化路径或
    数据库递归查询，但调用方的“包含全部后代”语义不变。
    """

    if hasattr(categories_or_ids, "values_list"):
        root_ids = set(categories_or_ids.values_list("id", flat=True))
    else:
        root_ids = {category.id if isinstance(category, Category) else category for category in categories_or_ids}
    root_ids.discard(None)
    if not root_ids:
        return []

    children_by_parent = {}
    for category_id, parent_id in Category.objects.filter(is_active=True).values_list("id", "parent_id"):
        children_by_parent.setdefault(parent_id, []).append(category_id)

    result_ids = set(root_ids)
    frontier = list(root_ids)
    while frontier:
        parent_id = frontier.pop()
        for child_id in children_by_parent.get(parent_id, []):
            if child_id not in result_ids:
                result_ids.add(child_id)
                frontier.append(child_id)
    return sorted(result_ids)


def _product_ordering(sort_by):
    """将页面排序参数转换为安全、固定的 ORM 排序字段。"""

    ordering_map = {
        "sales": ("-sales", "-created_at"),
        "price_asc": ("price", "-created_at"),
        "price_desc": ("-price", "-created_at"),
        "rating": ("-rating", "-created_at"),
    }
    return ordering_map.get(sort_by, ("-created_at",))


def product_detail(product_id):
    """返回一个上架商品及其图片、评价和父分类预加载数据，不存在时返回 404。"""

    return get_object_or_404(
        active_product_queryset().prefetch_related("images", "reviews", "category__parent"),
        id=product_id,
    )


def popular_brands(limit=10):
    """统计上架商品品牌数量，返回供品牌页直接渲染的字典列表。"""

    brands = (
        Product.objects.filter(is_active=True)
        .exclude(brand="")
        .values("brand")
        .annotate(count=Count("id"))
        .order_by("-count", "brand")[:limit]
    )
    return [{"name": item["brand"], "count": item["count"]} for item in brands]


def product_spec_context(product):
    """将旧 ``specs`` JSON 文本拆成详情页参数和可选规格上下文。"""

    spec_dict = {}
    spec_options = []
    if product.specs:
        try:
            raw_specs = json.loads(product.specs)
        except (json.JSONDecodeError, TypeError):
            raw_specs = {}
        if not isinstance(raw_specs, dict):
            raw_specs = {}
        selectable_keys = {"颜色", "规格", "尺寸", "版本", "容量", "款式", "型号", "尺码"}
        for key, value in raw_specs.items():
            if key in selectable_keys and isinstance(value, str) and "/" in value:
                spec_options.append(
                    {"label": key, "values": [item.strip() for item in value.split("/") if item.strip()]}
                )
            else:
                spec_dict[key] = value
    if not spec_options:
        spec_options.append({"label": "规格", "values": ["标准版"]})
    return {"spec_dict": spec_dict, "spec_options": spec_options}


def review_stats(product, *, limit=20):
    """返回限定数量的评价及按一至五星聚合的详情页统计数据。"""

    reviews = list(product.reviews.all()[:limit])
    if not reviews:
        return reviews, []
    total = len(reviews)
    star_counts = {star: 0 for star in range(1, 6)}
    for review in reviews:
        if review.rating in star_counts:
            star_counts[review.rating] += 1
    stats = []
    for star in [5, 4, 3, 2, 1]:
        count = star_counts[star]
        stats.append((star, count, int(count * 100 / total)))
    return reviews, stats
