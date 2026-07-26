"""商品目录的写入服务与外部来源幂等导入接口。

依赖流向：未来 ``integrations`` JSON 导入命令调用本模块；本模块负责写入
``catalog`` 模型，不依赖爬虫项目、订单、支付或 HTTP 路由。
"""

from __future__ import annotations

# Python 标准库依赖：验证来源元数据可 JSON 序列化，并提供不可变结果对象。
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Django 写入依赖：以事务和数据库完整性约束实现并发场景下的幂等性。
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

# 本领域模型依赖：导入服务只操作分类、商品、来源和浏览记录。
from .models import Category, Product, ProductSource


_SOURCE_IGNORED_PRODUCT_FIELDS = frozenset({"sales"})


@dataclass(frozen=True)
class ProductSourceUpsertResult:
    """描述一次来源商品写入得到的本地商品、来源记录和创建状态。"""

    product: Product
    source: ProductSource
    created: bool


@transaction.atomic
def get_or_create_category_path(category_names: Iterable[str], *, icon=""):
    """按名称路径获取或创建分类，并返回路径最后一级分类。

    JSON 导入器应先把清洗后的一级/二级分类名称传入本函数，再将返回值交给
    :func:`upsert_product_from_source`。空名称会被忽略；全为空时抛出校验错误。
    """

    names = [str(name).strip() for name in category_names if str(name).strip()]
    if not names:
        raise ValidationError("商品导入必须提供至少一个分类名称")

    parent = None
    for position, name in enumerate(names):
        parent_scope = parent.id if parent else 0
        category = (
            Category.objects.select_for_update()
            .filter(name=name, parent_scope=parent_scope)
            .order_by("id")
            .first()
        )
        if not category:
            try:
                # 内嵌事务使并发创建触发的唯一键冲突只回滚这一条 INSERT；外层导入事务
                # 随后可以锁定并复用另一个事务已创建的同一路径节点。
                with transaction.atomic():
                    category = Category.objects.create(
                        name=name,
                        parent=parent,
                        icon=icon if position == 0 else "",
                    )
            except IntegrityError:
                category = (
                    Category.objects.select_for_update()
                    .get(name=name, parent_scope=parent_scope)
                )
        if category.parent_id != (parent.id if parent else None):
            # ``parent_scope`` 只能由 Category.save 从 parent 派生；保留这一防御性检查，
            # 以便在有人绕过模型 save 使用 QuerySet.update 时尽早阻止错误分类路径。
            raise ValidationError("分类唯一性键与父级分类不一致，请先修复分类数据")
        parent = category
    return parent


@transaction.atomic
def upsert_product_from_source(
    *,
    source_type: str,
    external_id: str,
    category: Category,
    product_fields: Mapping,
    source_url="",
    source_payload: Mapping | None = None,
):
    """按 ``(source_type, external_id)`` 创建或更新商品，保证重复导入不重复建商品。

    参数 ``product_fields`` 仅接受 ``name``、价格、库存、商品描述、图片和运营标记等
    ``Product`` 可写字段。为兼容既有调用，传入的 ``sales`` 会被接受但永远不会写入
    本地商品；销量只能由订单支付流程累计。首次创建可带入待审核运营默认值；后续更新会刻意保留后台
    人工维护的上架、热门、新品和推荐状态。调用方应先完成 JSON 文件结构、图片文件
    和分类路径校验；该函数返回 :class:`ProductSourceUpsertResult`，其中 ``created``
    表示是否首次见到该外部来源商品。
    """

    normalized_source_type = _validate_source_type(source_type)
    normalized_external_id = _validate_external_id(external_id)
    if not isinstance(category, Category) or not category.pk:
        raise ValidationError("导入商品必须关联已保存的商品分类")
    normalized_fields = _validate_product_fields(product_fields)
    normalized_url = str(source_url or "").strip()
    normalized_payload = _validate_source_payload(source_payload)

    source = (
        ProductSource.objects.select_for_update()
        .select_related("product")
        .filter(source_type=normalized_source_type, external_id=normalized_external_id)
        .first()
    )
    if source:
        product = _save_imported_product(source.product, category, normalized_fields)
        _refresh_source(source, normalized_url, normalized_payload)
        return ProductSourceUpsertResult(product=product, source=source, created=False)

    try:
        # 嵌套事务使并发唯一键冲突只回滚本次临时商品创建，不影响外层导入批次。
        with transaction.atomic():
            product = _create_imported_product(category, normalized_fields)
            source = ProductSource(
                product=product,
                source_type=normalized_source_type,
                external_id=normalized_external_id,
                source_url=normalized_url,
                source_payload=normalized_payload,
            )
            source.full_clean()
            source.save()
    except IntegrityError:
        source = (
            ProductSource.objects.select_for_update()
            .select_related("product")
            .get(source_type=normalized_source_type, external_id=normalized_external_id)
        )
        product = _save_imported_product(source.product, category, normalized_fields)
        _refresh_source(source, normalized_url, normalized_payload)
        return ProductSourceUpsertResult(product=product, source=source, created=False)

    return ProductSourceUpsertResult(product=product, source=source, created=True)


def _validate_source_type(source_type):
    """校验来源类型属于已声明的安全来源集合。"""

    normalized_source_type = str(source_type or "").strip().lower()
    allowed_types = {choice for choice, _label in ProductSource.SOURCE_CHOICES}
    if normalized_source_type not in allowed_types:
        raise ValidationError("商品来源类型无效")
    return normalized_source_type


def _validate_external_id(external_id):
    """校验外部商品标识非空且不超过数据库字段长度。"""

    normalized_external_id = str(external_id or "").strip()
    if not normalized_external_id:
        raise ValidationError("商品导入必须提供 external_id")
    if len(normalized_external_id) > 128:
        raise ValidationError("external_id 长度不能超过 128 个字符")
    return normalized_external_id


def _validate_product_fields(product_fields):
    """过滤并校验来源商品允许写入的字段，阻止导入器覆盖系统字段。"""

    if not isinstance(product_fields, Mapping):
        raise ValidationError("product_fields 必须是对象")
    allowed_fields = {
        "name",
        "brand",
        "price",
        "original_price",
        "image",
        "stock",
        "sales",
        "rating",
        "review_count",
        "description",
        "specs",
        "is_hot",
        "is_new",
        "is_recommended",
        "is_active",
    }
    unexpected_fields = set(product_fields) - allowed_fields
    if unexpected_fields:
        raise ValidationError(f"商品导入包含不允许的字段：{', '.join(sorted(unexpected_fields))}")
    name = str(product_fields.get("name") or "").strip()
    if not name:
        raise ValidationError("商品导入必须提供非空 name")
    if "price" not in product_fields or product_fields["price"] in (None, ""):
        raise ValidationError("商品导入必须提供 price")
    normalized_fields = dict(product_fields)
    normalized_fields["name"] = name
    return normalized_fields


def _validate_source_payload(source_payload):
    """确保来源元数据为可序列化的 JSON 对象，避免保存不可恢复的 Python 对象。"""

    if source_payload is None:
        return {}
    if not isinstance(source_payload, Mapping):
        raise ValidationError("source_payload 必须是 JSON 对象")
    normalized_payload = dict(source_payload)
    try:
        json.dumps(normalized_payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("source_payload 必须可序列化为 JSON") from exc
    return normalized_payload


def _create_imported_product(category, product_fields):
    """创建并模型级校验一条来自外部来源的本地商品。

    首次导入时来源库存可作为商城可售库存的初始值；之后订单预占会独立维护
    ``Product.stock``，所以重导入只更新 ``source_stock`` 作为人工对账参考。
    """

    # 外部来源的历史销量不能成为商城销量；商城销量只反映本地成功支付订单。
    create_fields = {
        field_name: value
        for field_name, value in product_fields.items()
        if field_name not in _SOURCE_IGNORED_PRODUCT_FIELDS
    }
    product = Product(
        category=category,
        source_stock=product_fields.get("stock"),
        **create_fields,
    )
    product.full_clean()
    product.save()
    return product


def _save_imported_product(product, category, product_fields):
    """更新来源事实字段，同时保留后台人工审核的本地运营状态。

    外部 JSON 的商品内容会随爬虫批次变化，而 ``is_active``、``is_hot``、``is_new``
    和 ``is_recommended`` 是本地运营决定。``stock`` 是订单预占后不断变化的可售库存，
    也不能被导入覆盖；来源报告值只写入 ``source_stock``。若允许更新覆盖这些字段，
    重导入会将已审核商品意外下架或把已售库存加回，因此它们只在首次创建时写入默认值。
    """

    local_operations_fields = {
        "is_active",
        "is_hot",
        "is_new",
        "is_recommended",
        "stock",
        *_SOURCE_IGNORED_PRODUCT_FIELDS,
    }
    imported_fields = {
        field_name: value
        for field_name, value in product_fields.items()
        if field_name not in local_operations_fields
    }
    product.category = category
    product.source_stock = product_fields.get("stock")
    for field_name, value in imported_fields.items():
        setattr(product, field_name, value)
    product.full_clean()
    product.save(update_fields=["category", "source_stock", *imported_fields.keys(), "updated_at"])
    return product


def _refresh_source(source, source_url, source_payload):
    """更新来源链接、元数据及最后导入时间，保留首次导入时间。"""

    source.source_url = source_url
    source.source_payload = source_payload
    source.full_clean()
    source.save(update_fields=["source_url", "source_payload", "last_imported_at"])
    return source
