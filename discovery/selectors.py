"""快速找商品的 catalog 只读查询和可解释评分。

依赖流向：``discovery.services`` -> 本模块 -> ``catalog.models``。查询始终限制
候选数量，只读取已上架商品，不创建浏览历史、收藏、购物车或订单记录。
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Case, IntegerField, Q, Value, When

from catalog.models import Category, Product

from .contracts import ProductMatch, SearchIntent


MAX_FACET_VALUES = 500
MAX_CANDIDATES = 200
MAX_QUERY_TERMS = 8


def catalog_facets():
    """返回用于意图识别的有限品牌与分类名称集合。"""

    brands = tuple(
        Product.objects.filter(is_active=True, stock__gt=0)
        .exclude(brand="")
        .order_by("brand")
        .values_list("brand", flat=True)
        .distinct()[:MAX_FACET_VALUES]
    )
    categories = tuple(
        Category.objects.filter(is_active=True)
        .order_by("name")
        .values_list("name", flat=True)
        .distinct()[:MAX_FACET_VALUES]
    )
    return {"brands": brands, "categories": categories}


def find_product_matches(intent, *, limit):
    """按结构化意图筛选有限候选，并返回按解释性得分排序的商品快照。"""

    products = Product.objects.filter(is_active=True, stock__gt=0).select_related(
        "category",
        "category__parent",
        "category__parent__parent",
    )
    if intent.price_min is not None:
        products = products.filter(price__gte=intent.price_min)
    if intent.price_max is not None:
        products = products.filter(price__lte=intent.price_max)
    if intent.brand:
        products = products.filter(brand__iexact=intent.brand)
    if intent.category:
        products = products.filter(_category_query(intent.category))

    terms = _unique_terms((*intent.keywords, *intent.attributes))[:MAX_QUERY_TERMS]
    if terms:
        match_query = Q()
        for term in terms:
            match_query |= (
                Q(name__icontains=term)
                | Q(brand__icontains=term)
                | Q(category__name__icontains=term)
                | Q(category__parent__name__icontains=term)
                | Q(category__parent__parent__name__icontains=term)
                | Q(description__icontains=term)
                | Q(specs__icontains=term)
            )
        products = products.filter(match_query)

    # 必须在截断候选前于数据库中优先排列强匹配。否则当大量推荐商品只在描述中
    # 弱命中时，标题精确匹配可能排在第 200 条之后，永远进不了 Python 解释评分。
    products = products.annotate(
        _discovery_relevance=_database_relevance(intent, terms),
    )
    candidates = list(
        products.order_by(
            "-_discovery_relevance",
            "-is_recommended",
            "-is_hot",
            "-rating",
            "-sales",
            "price",
            "id",
        )[:MAX_CANDIDATES]
    )
    scored = [_score_product(product, intent, terms) for product in candidates]
    scored.sort(key=lambda item: (-item.score, -item.rating, -item.sales, item.price, item.id))
    return tuple(scored[:limit])


def _category_query(category_name):
    """让一级/二级分类名称都可匹配，同时保持 SQL 查询只读且有界。"""

    return (
        Q(category__name__iexact=category_name)
        | Q(category__parent__name__iexact=category_name)
        | Q(category__parent__parent__name__iexact=category_name)
    )


def _database_relevance(intent, terms):
    """构造截断前的数据库相关度表达式，保证强匹配优先进入有限候选。"""

    relevance = Value(0, output_field=IntegerField())
    normalized_query = str(intent.query or "").strip()
    if normalized_query:
        relevance = relevance + Case(
            When(name__iexact=normalized_query, then=Value(220)),
            default=Value(0),
            output_field=IntegerField(),
        )
    if intent.brand:
        relevance = relevance + Case(
            When(brand__iexact=intent.brand, then=Value(70)),
            default=Value(0),
            output_field=IntegerField(),
        )
    if intent.category:
        relevance = relevance + Case(
            When(_category_query(intent.category), then=Value(60)),
            default=Value(0),
            output_field=IntegerField(),
        )

    for term in terms:
        relevance = relevance + Case(
            When(name__iexact=term, then=Value(120)),
            When(name__icontains=term, then=Value(80)),
            When(brand__iexact=term, then=Value(65)),
            When(brand__icontains=term, then=Value(55)),
            When(category__name__icontains=term, then=Value(48)),
            When(category__parent__name__icontains=term, then=Value(46)),
            When(category__parent__parent__name__icontains=term, then=Value(44)),
            When(specs__icontains=term, then=Value(28)),
            When(description__icontains=term, then=Value(20)),
            default=Value(0),
            output_field=IntegerField(),
        )
    return relevance


def _score_product(product, intent, terms):
    """根据可审计字段计算相关度，并生成面向用户的命中原因。"""

    name = product.name.casefold()
    brand = (product.brand or "").casefold()
    category_names = _category_names(product)
    category_text = " ".join(category_names).casefold()
    description = (product.description or "").casefold()
    specs = (product.specs or "").casefold()
    score = 0
    reasons = []

    if intent.brand and brand == intent.brand.casefold():
        score += 28
        reasons.append(f"品牌符合：{intent.brand}")
    if intent.category and intent.category.casefold() in category_text:
        score += 24
        reasons.append(f"分类符合：{intent.category}")

    for term in terms:
        normalized_term = term.casefold()
        if normalized_term in name:
            score += 18
            reasons.append(f"商品名称符合“{term}”")
        elif normalized_term in brand:
            score += 14
            reasons.append(f"品牌信息符合“{term}”")
        elif normalized_term in category_text:
            score += 12
            reasons.append(f"商品分类符合“{term}”")
        elif normalized_term in description:
            score += 7
            reasons.append(f"商品描述符合“{term}”")
        elif normalized_term in specs:
            score += 6
            reasons.append(f"商品参数符合“{term}”")

    if intent.price_min is not None or intent.price_max is not None:
        score += 8
        reasons.append(_price_reason(intent))
    if product.is_recommended:
        score += 3
    if product.is_hot:
        score += 2

    if not reasons:
        reasons.append("符合当前筛选条件")
    return ProductMatch(
        id=product.id,
        name=product.name,
        brand=product.brand,
        category=" / ".join(category_names),
        price=product.price,
        original_price=product.original_price,
        image=_safe_image_url(product),
        rating=product.rating,
        sales=product.sales,
        score=score,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _category_names(product):
    """按父级到当前分类返回最多三层名称，避免额外数据库查询。"""

    names = []
    category = product.category
    while category is not None and len(names) < 3:
        names.append(category.name)
        category = getattr(category, "parent", None)
    return tuple(reversed(names))


def _price_reason(intent):
    """把价格筛选转换成简短、易读的解释。"""

    if intent.price_min is not None and intent.price_max is not None:
        return f"价格在 {intent.price_min} 至 {intent.price_max} 元之间"
    if intent.price_min is not None:
        return f"价格不低于 {intent.price_min} 元"
    return f"价格不超过 {intent.price_max} 元"


def _safe_image_url(product):
    """读取图片 URL；存储后端异常时返回空字符串而不影响其余结果。"""

    if not product.image:
        return ""
    try:
        return product.image.url
    except (ValueError, OSError):
        return ""


def _unique_terms(terms):
    """稳定去重并丢弃空白、过长关键词，控制 ORM 条件数量。"""

    result = []
    seen = set()
    for value in terms:
        term = str(value or "").strip()
        key = term.casefold()
        if not term or len(term) > 40 or key in seen:
            continue
        seen.add(key)
        result.append(term)
    return tuple(result)
