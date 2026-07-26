"""导入独立爬虫产出的、已清洗的商品 JSON 清单。

依赖流向：受控本地 ``manifest.json`` -> 本命令的结构/安全校验 ->
``catalog.services.get_or_create_category_path`` 与
``catalog.services.upsert_product_from_source`` -> ``catalog`` ORM。
本命令不依赖爬虫项目、不请求网络、不执行 JSON 中的内容，也不保存原始响应、
Cookie、令牌或其他敏感字段。

TODO（媒体）：本轮仅校验导入包内的本地图片及可选 SHA-256；待明确媒体白名单、
文件大小限制、缩略图和对象存储策略后，才复制至 ``media/products/`` 并创建
``ProductImage`` 记录。
TODO（商品规格）：当前目录模型没有 SKU/变体模型；导入包不得携带 ``sku`` 等
未约定字段，待 SKU 领域设计完成后再扩展此命令和导入契约。
"""

from __future__ import annotations

# Python 标准库依赖：解析受控 JSON、计算本地文件摘要、校验路径与生成安全报告。
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlparse

# Django 命令、字段校验和事务依赖：命令层负责交互，事务确保单条导入可回滚。
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator
from django.db import transaction

# 商品目录依赖：查询现有来源用于 dry-run，实际写入必须经过领域服务以保持幂等。
from catalog.models import Category, Product, ProductSource
from catalog.services import get_or_create_category_path, upsert_product_from_source


SUPPORTED_SCHEMA_VERSION = "1.0"
MAX_MANIFEST_BYTES = 20 * 1024 * 1024
MAX_PRODUCTS_PER_BATCH = 10_000
MAX_DESCRIPTION_LENGTH = 20_000
MAX_SPECS_BYTES = 20_000
MAX_SOURCE_METADATA_BYTES = 10_000
IMAGE_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
UNSAFE_DESCRIPTION_PATTERN = re.compile(
    r"<\s*(?:script|iframe|object|embed|style)\b|javascript\s*:|\bon\w+\s*=",
    re.IGNORECASE,
)
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:password|passwd|secret|token|cookie|authorization|api[_-]?key|private[_-]?key|"
    r"phone|mobile|email|id[_-]?card)",
    re.IGNORECASE,
)
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "signature",
    "token",
}


class RowValidationError(Exception):
    """携带一条商品记录的字段级校验错误，供命令汇总而不是立即终止。"""

    def __init__(self, errors: list[dict[str, str]]):
        """保存已脱敏的 ``field`` 与中文 ``message`` 错误项。"""

        super().__init__("商品记录校验失败")
        self.errors = errors


@dataclass(frozen=True)
class PreparedProduct:
    """表示完成预校验、可安全传给商品目录服务的一条商品数据。"""

    external_id: str
    category_names: tuple[str, ...]
    product_fields: dict[str, Any]
    source_url: str
    source_payload: dict[str, Any]
    signature: str
    warnings: tuple[str, ...]


class Command(BaseCommand):
    """将契约版本为 1.0 的清洗后商品清单安全、幂等地导入商品目录。"""

    help = "导入已清洗的商品 manifest.json，支持预检、幂等更新和 JSON 报告。"

    def add_arguments(self, parser):
        """声明清单路径、预检模式、来源类型和可选报告输出位置。"""

        parser.add_argument(
            "manifest",
            help="清洗项目交付的 manifest.json 路径；不接受网页、压缩包或爬虫原始响应。",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="完整校验并预测创建/更新/跳过结果，绝不写入数据库或媒体目录。",
        )
        parser.add_argument(
            "--source-type",
            default=ProductSource.SOURCE_JD,
            choices=[choice for choice, _label in ProductSource.SOURCE_CHOICES],
            help="来源类型，必须与清单顶层 source 一致；默认 jd。",
        )
        parser.add_argument(
            "--report",
            help="可选的安全 JSON 报告路径，例如 reports/import-jd-001.json。",
        )

    def handle(self, *args, **options):
        """执行完整校验、逐条导入并在任意数据错误后以非零状态结束。"""

        mode = "dry-run" if options["dry_run"] else "import"
        report_path = _normalize_report_path(options.get("report"))
        report = _new_report(mode=mode, source_type=options["source_type"])

        try:
            manifest_path = _normalize_manifest_path(options["manifest"])
            manifest = _load_manifest(manifest_path)
            header = _validate_manifest_header(
                manifest,
                source_type=options["source_type"],
            )
        except CommandError as exc:
            report["errors"].append({"field": "manifest", "message": str(exc)})
            _write_report_if_requested(report_path, report)
            raise

        report.update(
            {
                "batch_id": header["batch_id"],
                "schema_version": header["schema_version"],
                "source": header["source"],
            }
        )
        package_root = manifest_path.parent.resolve()
        seen_keys: set[tuple[str, str]] = set()

        for index, raw_item in enumerate(manifest["products"]):
            item_report = {"index": index, "external_id": _external_id_for_report(raw_item)}
            try:
                prepared = _prepare_product(
                    raw_item,
                    package_root=package_root,
                    batch_id=header["batch_id"],
                    schema_version=header["schema_version"],
                )
                source_key = (header["source"], prepared.external_id)
                if source_key in seen_keys:
                    raise RowValidationError(
                        [
                            {
                                "field": "external_id",
                                "message": "同一导入批次中不能重复使用 source + external_id。",
                            }
                        ]
                    )
                seen_keys.add(source_key)

                status = _predict_status(header["source"], prepared)
                if not options["dry_run"] and status != "skip":
                    status = _import_product(header["source"], prepared)

                item_report.update({"external_id": prepared.external_id, "status": status})
                if prepared.warnings:
                    item_report["warnings"] = list(prepared.warnings)
            except RowValidationError as exc:
                item_report.update({"status": "failed", "errors": exc.errors})
            report["items"].append(item_report)

        _summarize_report(report)
        _write_report_if_requested(report_path, report)
        self._write_summary(report, report_path)

        failed = report["summary"]["failed"]
        if failed:
            raise CommandError(
                f"导入完成，但有 {failed} 条记录失败；已汇总全部可识别错误，请修复后重试。"
            )

    def _write_summary(self, report, report_path):
        """输出不含原始商品内容和敏感字段的人类可读导入汇总。"""

        summary = report["summary"]
        self.stdout.write(
            self.style.SUCCESS(
                "商品导入{mode}完成：总计 {total}，创建 {create}，更新 {update}，"
                "跳过 {skip}，失败 {failed}。".format(mode=report["mode"], **summary)
            )
        )
        if report_path:
            self.stdout.write(f"安全报告已写入：{report_path}")


def _normalize_manifest_path(raw_path: str) -> Path:
    """校验输入是大小受控、非符号链接的 UTF-8 JSON 文件，并返回其绝对路径。"""

    path = Path(raw_path).expanduser()
    if path.suffix.lower() != ".json":
        raise CommandError("只接受已清洗的 .json 清单文件，不接受网页、压缩包或其他格式。")
    if not path.exists() or not path.is_file():
        raise CommandError("找不到指定的 JSON 清单文件。")
    if path.is_symlink():
        raise CommandError("清单文件不能是符号链接，请将受控文件直接放入导入目录。")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CommandError("无法读取 JSON 清单文件元数据。") from exc
    if size > MAX_MANIFEST_BYTES:
        raise CommandError("JSON 清单超过 20 MiB 安全上限，请拆分清洗后的导入批次。")
    return path.resolve()


def _normalize_report_path(raw_path: str | None) -> Path | None:
    """校验可选报告目标为 JSON 文件；未提供时不产生额外文件。"""

    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.suffix.lower() != ".json":
        raise CommandError("--report 必须指定 .json 文件路径。")
    if path.exists() and path.is_symlink():
        raise CommandError("--report 目标不能是符号链接。")
    return path.resolve()


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """以 UTF-8 读取并解析 JSON；解析失败时不给出可能敏感的原始片段。"""

    try:
        content = manifest_path.read_text(encoding="utf-8-sig")
        manifest = json.loads(content)
    except UnicodeDecodeError as exc:
        raise CommandError("JSON 清单必须使用 UTF-8 编码。") from exc
    except json.JSONDecodeError as exc:
        raise CommandError(f"JSON 清单格式无效（第 {exc.lineno} 行第 {exc.colno} 列）。") from exc
    except OSError as exc:
        raise CommandError("无法读取 JSON 清单文件。") from exc
    if not isinstance(manifest, dict):
        raise CommandError("JSON 清单顶层必须是对象。")
    return manifest


def _validate_manifest_header(manifest: dict[str, Any], *, source_type: str) -> dict[str, Any]:
    """校验清单顶层契约字段、版本、来源与批次大小，拒绝未知结构。"""

    try:
        return _validate_manifest_header_fields(manifest, source_type=source_type)
    except RowValidationError as exc:
        first_error = exc.errors[0]["message"] if exc.errors else "顶层清单字段无效。"
        raise CommandError(first_error) from exc


def _validate_manifest_header_fields(
    manifest: dict[str, Any],
    *,
    source_type: str,
) -> dict[str, Any]:
    """执行顶层字段细节校验，并把行级校验异常交由外层转换为命令错误。"""

    allowed_keys = {"schema_version", "batch_id", "source", "generated_at", "generator", "products"}
    _reject_unknown_keys(manifest, allowed_keys, field="manifest")

    schema_version = _required_text(manifest, "schema_version", field="schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise CommandError(
            f"只支持 schema_version={SUPPORTED_SCHEMA_VERSION}；请升级导入器或使用兼容的清洗包。"
        )
    batch_id = _required_text(manifest, "batch_id", field="batch_id", max_length=128)
    source = _required_text(manifest, "source", field="source", max_length=32).lower()
    allowed_sources = {choice for choice, _label in ProductSource.SOURCE_CHOICES}
    if source not in allowed_sources:
        raise CommandError("清单 source 不在已声明的受信任来源范围内。")
    if source != source_type:
        raise CommandError("清单顶层 source 必须与 --source-type 完全一致。")

    generated_at = _required_text(manifest, "generated_at", field="generated_at", max_length=64)
    _validate_timezone_datetime(generated_at, field="generated_at")
    generator = manifest.get("generator")
    if not isinstance(generator, dict):
        raise CommandError("generator 必须是包含 project 和 version 的对象。")
    _reject_unknown_keys(generator, {"project", "version"}, field="generator")
    _required_text(generator, "project", field="generator.project", max_length=120)
    _required_text(generator, "version", field="generator.version", max_length=64)

    products = manifest.get("products")
    if not isinstance(products, list) or not products:
        raise CommandError("products 必须是非空数组。")
    if len(products) > MAX_PRODUCTS_PER_BATCH:
        raise CommandError(f"单个批次最多允许 {MAX_PRODUCTS_PER_BATCH} 条商品，请拆分后导入。")
    return {"schema_version": schema_version, "batch_id": batch_id, "source": source}


def _prepare_product(
    raw_item: Any,
    *,
    package_root: Path,
    batch_id: str,
    schema_version: str,
) -> PreparedProduct:
    """将单条契约记录转为受限商品字段，并聚合其所有独立字段校验错误。"""

    if not isinstance(raw_item, dict):
        raise RowValidationError([_error("product", "商品记录必须是对象。")])

    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    _capture(
        errors,
        lambda: _reject_unknown_keys(
            raw_item,
            {
                "external_id",
                "source_url",
                "name",
                "brand",
                "category",
                "pricing",
                "stock",
                "description",
                "specs",
                "images",
                "flags",
                "source_metadata",
            },
            field="product",
        ),
    )
    external_id = _capture(
        errors,
        lambda: _required_text(
            raw_item,
            "external_id",
            field="external_id",
            max_length=ProductSource._meta.get_field("external_id").max_length,
        ),
    )
    source_url = _capture(errors, lambda: _validate_source_url(raw_item.get("source_url")))
    name = _capture(
        errors,
        lambda: _required_text(
            raw_item,
            "name",
            field="name",
            max_length=Product._meta.get_field("name").max_length,
        ),
    )
    brand = _capture(
        errors,
        lambda: _optional_text(
            raw_item.get("brand", ""),
            field="brand",
            max_length=Product._meta.get_field("brand").max_length,
        ),
    )
    category_names = _capture(errors, lambda: _validate_category(raw_item.get("category")))
    pricing = _capture(errors, lambda: _validate_pricing(raw_item.get("pricing")))
    stock = _capture(errors, lambda: _validate_stock(raw_item.get("stock")))
    description = _capture(errors, lambda: _validate_description(raw_item.get("description", "")))
    specs = _capture(errors, lambda: _validate_specs(raw_item.get("specs", {})))
    flags = _capture(errors, lambda: _validate_flags(raw_item.get("flags", {})))
    source_metadata = _capture(
        errors,
        lambda: _validate_source_metadata(raw_item.get("source_metadata", {})),
    )
    images = _capture(errors, lambda: _validate_images(raw_item.get("images", []), package_root))

    if errors:
        raise RowValidationError(errors)

    # 外部来源不能直接决定商城运营和上架状态，统一等待人工审核。
    if any(flags.values()):
        warnings.append("外部 flags 不会直接改变上架、热门、新品或推荐状态，已按待审核商品处理。")
    if images:
        warnings.append(
            "TODO：已验证导入包内本地图片，但当前版本不会复制媒体文件或创建 ProductImage 记录。"
        )

    product_fields = {
        "name": name,
        "brand": brand,
        "price": pricing["price"],
        "original_price": pricing["original_price"],
        "stock": stock,
        "description": description,
        "specs": specs,
        "is_active": False,
        "is_hot": False,
        "is_new": False,
        "is_recommended": False,
    }
    signature = _content_signature(
        {
            "category": category_names,
            "product_fields": _json_safe_product_fields(product_fields),
            "source_url": source_url,
            "source_metadata": source_metadata,
            "images": images,
        }
    )
    source_payload = {
        "batch_id": batch_id,
        "schema_version": schema_version,
        "content_hash": signature,
        "source_metadata": source_metadata,
    }
    return PreparedProduct(
        external_id=external_id,
        category_names=tuple(category_names),
        product_fields=product_fields,
        source_url=source_url,
        source_payload=source_payload,
        signature=signature,
        warnings=tuple(warnings),
    )


def _capture(errors: list[dict[str, str]], validator):
    """运行独立字段校验；失败时保留错误并返回 ``None`` 以继续检查同一记录。"""

    try:
        return validator()
    except RowValidationError as exc:
        errors.extend(exc.errors)
        return None


def _reject_unknown_keys(value: dict[str, Any], allowed_keys: set[str], *, field: str):
    """拒绝契约外字段，避免原始爬虫字段或敏感字段被意外持久化。"""

    unexpected = sorted(set(value) - allowed_keys)
    if unexpected:
        names = "、".join(unexpected)
        raise RowValidationError([_error(field, f"包含当前契约不支持的字段：{names}。")])


def _required_text(
    value: dict[str, Any],
    key: str,
    *,
    field: str,
    max_length: int | None = None,
) -> str:
    """读取必填字符串并应用空值、类型和长度限制。"""

    if key not in value:
        raise RowValidationError([_error(field, "缺少必填字段。")])
    return _optional_text(value[key], field=field, max_length=max_length, required=True)


def _optional_text(
    value: Any,
    *,
    field: str,
    max_length: int | None = None,
    required: bool = False,
) -> str:
    """规范化可选字符串；必填字段为空、非字符串或过长时返回字段错误。"""

    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise RowValidationError([_error(field, "必须是字符串。")])
    normalized = value.strip()
    if required and not normalized:
        raise RowValidationError([_error(field, "不能为空。")])
    if max_length is not None and len(normalized) > max_length:
        raise RowValidationError([_error(field, f"长度不能超过 {max_length} 个字符。")])
    return normalized


def _validate_timezone_datetime(value: str, *, field: str):
    """确认 ISO-8601 时间携带时区，保证导入批次可审计且没有本地时间歧义。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommandError(f"{field} 必须是带时区的 ISO-8601 时间。") from exc
    if parsed.tzinfo is None:
        raise CommandError(f"{field} 必须包含时区偏移量。")


def _validate_category(value: Any) -> list[str]:
    """校验至少两级的分类路径，并以层级顺序返回名称列表。"""

    if not isinstance(value, dict):
        raise RowValidationError([_error("category", "必须是包含 level_1 和 level_2 的对象。")])
    _reject_unknown_keys(value, {"level_1", "level_2", "level_3"}, field="category")
    max_length = Category._meta.get_field("name").max_length
    names = [
        _required_text(value, "level_1", field="category.level_1", max_length=max_length),
        _required_text(value, "level_2", field="category.level_2", max_length=max_length),
    ]
    if "level_3" in value and value["level_3"] not in (None, ""):
        names.append(
            _optional_text(value["level_3"], field="category.level_3", max_length=max_length, required=True)
        )
    return names


def _validate_pricing(value: Any) -> dict[str, Decimal | None]:
    """校验 CNY 价格、两位小数精度及原价不低于现价的业务约束。"""

    if not isinstance(value, dict):
        raise RowValidationError([_error("pricing", "必须是包含 price 和 currency 的对象。")])
    _reject_unknown_keys(value, {"price", "original_price", "currency"}, field="pricing")
    currency = _required_text(value, "currency", field="pricing.currency", max_length=3).upper()
    if currency != "CNY":
        raise RowValidationError([_error("pricing.currency", "当前只支持 CNY。")])
    price = _decimal_amount(value.get("price"), field="pricing.price", required=True)
    original_value = value.get("original_price")
    original_price = None
    if original_value not in (None, ""):
        original_price = _decimal_amount(
            original_value,
            field="pricing.original_price",
            required=False,
        )
        if original_price < price:
            raise RowValidationError(
                [_error("pricing.original_price", "不能低于 pricing.price。")]
            )
    return {"price": price, "original_price": original_price}


def _decimal_amount(value: Any, *, field: str, required: bool) -> Decimal:
    """将 JSON 金额安全转换为非负且符合商品字段精度的 ``Decimal``。"""

    if value in (None, ""):
        message = "缺少必填金额。" if required else "金额不能为空。"
        raise RowValidationError([_error(field, message)])
    if isinstance(value, bool):
        raise RowValidationError([_error(field, "必须是非负金额。")])
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RowValidationError([_error(field, "必须是可解析的非负金额。")]) from exc
    if not amount.is_finite() or amount < 0:
        raise RowValidationError([_error(field, "必须是非负金额。")])
    field_definition = Product._meta.get_field("price")
    decimal_places = field_definition.decimal_places
    max_digits = field_definition.max_digits
    if -amount.as_tuple().exponent > decimal_places:
        raise RowValidationError([_error(field, f"最多支持 {decimal_places} 位小数。")])
    digits = len(amount.as_tuple().digits)
    whole_digits = max(digits + amount.as_tuple().exponent, 0)
    if whole_digits > max_digits - decimal_places:
        raise RowValidationError([_error(field, "金额超出商品价格字段允许范围。")])
    return amount


def _validate_stock(value: Any) -> int:
    """校验库存是 JSON 整数且非负，不接受布尔值、浮点数或实时库存语义。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RowValidationError([_error("stock", "必须是非负整数。")])
    return value


def _validate_description(value: Any) -> str:
    """接受清洗后的文本或低风险 HTML，拒绝脚本、内联事件和危险嵌入内容。"""

    description = _optional_text(value, field="description", max_length=MAX_DESCRIPTION_LENGTH)
    if UNSAFE_DESCRIPTION_PATTERN.search(description):
        raise RowValidationError(
            [_error("description", "包含脚本、危险嵌入或内联事件，必须先在清洗项目中移除。")]
        )
    return description


def _validate_specs(value: Any) -> str:
    """校验有限深度的规格对象并序列化为兼容现有商品模型的 JSON 文本。"""

    if not isinstance(value, dict):
        raise RowValidationError([_error("specs", "必须是 JSON 对象。")])
    _validate_safe_json_value(value, field="specs", depth=0, max_depth=3)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_SPECS_BYTES:
        raise RowValidationError([_error("specs", "序列化后不能超过 20 KiB。")])
    return serialized


def _validate_flags(value: Any) -> dict[str, bool]:
    """校验可选运营标记的布尔结构，但不允许外部来源直接改变商城运营状态。"""

    if not isinstance(value, dict):
        raise RowValidationError([_error("flags", "必须是 JSON 对象。")])
    allowed = {"is_active", "is_new", "is_hot", "is_recommended"}
    _reject_unknown_keys(value, allowed, field="flags")
    flags: dict[str, bool] = {}
    for key in sorted(allowed):
        current = value.get(key, False)
        if not isinstance(current, bool):
            raise RowValidationError([_error(f"flags.{key}", "必须是布尔值。")])
        flags[key] = current
    return flags


def _validate_source_metadata(value: Any) -> dict[str, str]:
    """仅保留契约声明的可审计元数据，拒绝未知或疑似敏感键。"""

    if not isinstance(value, dict):
        raise RowValidationError([_error("source_metadata", "必须是 JSON 对象。")])
    allowed = {"captured_at", "content_hash", "raw_title"}
    _reject_unknown_keys(value, allowed, field="source_metadata")
    _reject_sensitive_keys(value, field="source_metadata")

    metadata: dict[str, str] = {}
    if "captured_at" in value:
        captured_at = _optional_text(value["captured_at"], field="source_metadata.captured_at", max_length=64, required=True)
        _validate_row_timezone_datetime(captured_at, field="source_metadata.captured_at")
        metadata["captured_at"] = captured_at
    if "content_hash" in value:
        content_hash = _optional_text(value["content_hash"], field="source_metadata.content_hash", max_length=128, required=True)
        metadata["content_hash"] = content_hash
    if "raw_title" in value:
        metadata["raw_title"] = _optional_text(
            value["raw_title"],
            field="source_metadata.raw_title",
            max_length=500,
        )
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) > MAX_SOURCE_METADATA_BYTES:
        raise RowValidationError([_error("source_metadata", "序列化后不能超过 10 KiB。")])
    return metadata


def _validate_row_timezone_datetime(value: str, *, field: str):
    """把行级时间格式错误转换为可汇总的商品字段错误。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RowValidationError([_error(field, "必须是带时区的 ISO-8601 时间。")]) from exc
    if parsed.tzinfo is None:
        raise RowValidationError([_error(field, "必须包含时区偏移量。")])


def _validate_images(value: Any, package_root: Path) -> list[dict[str, Any]]:
    """校验图片仅引用导入包内的本地普通文件，不下载或解析远程 URL。"""

    if not isinstance(value, list):
        raise RowValidationError([_error("images", "必须是数组。")])
    normalized: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    main_count = 0
    for index, image in enumerate(value):
        prefix = f"images[{index}]"
        try:
            if not isinstance(image, dict):
                raise RowValidationError([_error(prefix, "必须是对象。")])
            _reject_unknown_keys(image, {"path", "role", "sort_order", "sha256"}, field=prefix)
            image_path = _validate_package_image_path(image.get("path"), package_root, field=f"{prefix}.path")
            role = _required_text(image, "role", field=f"{prefix}.role", max_length=16).lower()
            if role not in {"main", "gallery"}:
                raise RowValidationError([_error(f"{prefix}.role", "只支持 main 或 gallery。")])
            sort_order = image.get("sort_order")
            if isinstance(sort_order, bool) or not isinstance(sort_order, int) or sort_order < 0:
                raise RowValidationError([_error(f"{prefix}.sort_order", "必须是非负整数。")])
            checksum = image.get("sha256")
            if checksum is not None:
                checksum = _optional_text(checksum, field=f"{prefix}.sha256", max_length=64, required=True)
                if not IMAGE_SHA256_PATTERN.fullmatch(checksum):
                    raise RowValidationError([_error(f"{prefix}.sha256", "必须是 64 位十六进制 SHA-256。")])
                if _file_sha256(image_path).lower() != checksum.lower():
                    raise RowValidationError([_error(f"{prefix}.sha256", "与导入包内本地图片不匹配。")])
            if role == "main":
                main_count += 1
            normalized.append(
                {
                    "path": image_path.relative_to(package_root).as_posix(),
                    "role": role,
                    "sort_order": sort_order,
                    **({"sha256": checksum.lower()} if checksum else {}),
                }
            )
        except RowValidationError as exc:
            errors.extend(exc.errors)
    if value and main_count != 1:
        errors.append(_error("images", "提供图片时必须且只能有一张 role=main 的主图。"))
    if errors:
        raise RowValidationError(errors)
    return sorted(normalized, key=lambda item: (item["sort_order"], item["path"]))


def _validate_package_image_path(value: Any, package_root: Path, *, field: str) -> Path:
    """解析相对 POSIX 图片路径，并阻断 URL、绝对路径、路径穿越和符号链接逃逸。"""

    relative_path = _optional_text(value, field=field, required=True, max_length=500)
    if "\\" in relative_path:
        raise RowValidationError([_error(field, "必须使用相对于导入包的 POSIX 路径，不能包含反斜杠。")])
    parsed = urlparse(relative_path)
    if parsed.scheme or parsed.netloc:
        raise RowValidationError([_error(field, "不能使用远程 URL；图片必须随导入包提供。")])
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts or not pure_path.parts or pure_path.parts[0] != "images":
        raise RowValidationError([_error(field, "必须位于导入包 images/ 目录下，且不能包含路径穿越。")])
    candidate = package_root.joinpath(*pure_path.parts)
    if not candidate.exists() or not candidate.is_file():
        raise RowValidationError([_error(field, "引用的本地图片文件不存在。")])
    if candidate.is_symlink():
        raise RowValidationError([_error(field, "图片文件不能是符号链接。")])
    resolved = candidate.resolve()
    try:
        resolved.relative_to(package_root)
    except ValueError as exc:
        raise RowValidationError([_error(field, "图片解析后超出导入包目录。")]) from exc
    return resolved


def _file_sha256(path: Path) -> str:
    """流式计算本地图片 SHA-256，避免将大文件一次性读入内存。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RowValidationError([_error("images", "无法读取导入包内本地图片。")]) from exc
    return digest.hexdigest()


def _validate_source_url(value: Any) -> str:
    """校验来源链接是无凭据、无敏感查询参数的 HTTPS URL，且不会被命令访问。"""

    source_url = _optional_text(
        value,
        field="source_url",
        required=True,
        max_length=ProductSource._meta.get_field("source_url").max_length,
    )
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RowValidationError([_error("source_url", "必须是合法的 https URL。")])
    if parsed.username or parsed.password:
        raise RowValidationError([_error("source_url", "不能包含用户名或密码。")])
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & SENSITIVE_QUERY_KEYS:
        raise RowValidationError([_error("source_url", "不能包含敏感查询参数。")])
    try:
        URLValidator(schemes=["https"])(source_url)
    except Exception as exc:
        raise RowValidationError([_error("source_url", "必须是合法的 https URL。")]) from exc
    return source_url


def _validate_safe_json_value(value: Any, *, field: str, depth: int, max_depth: int):
    """限制规格 JSON 的嵌套层级、键类型和敏感键，确保可安全持久化。"""

    if depth > max_depth:
        raise RowValidationError([_error(field, f"嵌套层级不能超过 {max_depth} 层。")])
    if isinstance(value, dict):
        _reject_sensitive_keys(value, field=field)
        for key, child in value.items():
            if not isinstance(key, str):
                raise RowValidationError([_error(field, "对象键必须是字符串。")])
            _validate_safe_json_value(child, field=f"{field}.{key}", depth=depth + 1, max_depth=max_depth)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_safe_json_value(child, field=f"{field}[{index}]", depth=depth + 1, max_depth=max_depth)
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise RowValidationError([_error(field, "只能包含 JSON 基本类型、数组或对象。")])


def _reject_sensitive_keys(value: dict[str, Any], *, field: str):
    """拒绝疑似凭据、个人信息或认证字段，防止将它们写入商品来源元数据。"""

    sensitive_keys = sorted(str(key) for key in value if SENSITIVE_KEY_PATTERN.search(str(key)))
    if sensitive_keys:
        raise RowValidationError([_error(field, "不能包含敏感字段。")])


def _content_signature(value: dict[str, Any]) -> str:
    """为已允许的商品内容生成稳定摘要，用于重复导入时的无写入跳过判断。"""

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_safe_product_fields(value: dict[str, Any]) -> dict[str, Any]:
    """将 Decimal 字段转为字符串，以便参与跨平台一致的内容摘要计算。"""

    normalized = dict(value)
    normalized["price"] = str(value["price"])
    normalized["original_price"] = str(value["original_price"]) if value["original_price"] is not None else None
    return normalized


def _predict_status(source_type: str, prepared: PreparedProduct) -> str:
    """只读查询现有来源记录，预测创建、更新或内容未变时的跳过结果。"""

    existing = ProductSource.objects.filter(
        source_type=source_type,
        external_id=prepared.external_id,
    ).only("source_payload").first()
    if existing is None:
        return "create"
    if existing.source_payload.get("content_hash") == prepared.signature:
        return "skip"
    return "update"


def _import_product(source_type: str, prepared: PreparedProduct) -> str:
    """在单条事务中创建分类并调用领域服务写入来源映射和商品，避免半条数据。"""

    try:
        with transaction.atomic():
            category = get_or_create_category_path(prepared.category_names)
            result = upsert_product_from_source(
                source_type=source_type,
                external_id=prepared.external_id,
                category=category,
                product_fields=prepared.product_fields,
                source_url=prepared.source_url,
                source_payload=prepared.source_payload,
            )
    except Exception as exc:
        # 不回显异常字符串，防止数据库/文件系统错误中意外包含敏感上下文。
        raise RowValidationError([_error("product", "写入失败；请检查清洗字段、分类约束和服务器日志。")]) from exc
    return "create" if result.created else "update"


def _external_id_for_report(value: Any) -> str | None:
    """从原始行安全提取可显示的外部 ID；非法行不回显其他原始内容。"""

    if not isinstance(value, dict):
        return None
    external_id = value.get("external_id")
    return external_id.strip() if isinstance(external_id, str) else None


def _new_report(*, mode: str, source_type: str) -> dict[str, Any]:
    """创建只包含导入结果、错误和统计信息的脱敏报告结构。"""

    return {
        "batch_id": None,
        "schema_version": None,
        "source": source_type,
        "mode": mode,
        "summary": {"total": 0, "create": 0, "update": 0, "skip": 0, "failed": 0},
        "items": [],
        "errors": [],
    }


def _summarize_report(report: dict[str, Any]):
    """根据逐项结果计算稳定的导入统计，供命令输出和 JSON 报告共同使用。"""

    summary = {"total": len(report["items"]), "create": 0, "update": 0, "skip": 0, "failed": 0}
    for item in report["items"]:
        status = item.get("status")
        if status in {"create", "update", "skip", "failed"}:
            summary[status] += 1
    report["summary"] = summary


def _write_report_if_requested(report_path: Path | None, report: dict[str, Any]):
    """在用户明确指定时写出 UTF-8 JSON 报告，不写入任何原始清洗数据。"""

    if report_path is None:
        return
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise CommandError("无法写入指定的 JSON 导入报告。") from exc


def _error(field: str, message: str) -> dict[str, str]:
    """构造统一的脱敏字段错误项，避免报告中泄漏整条原始输入记录。"""

    return {"field": field, "message": message}
