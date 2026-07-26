"""商品目录 JSON 导入命令的端到端安全回归测试。

依赖流向：测试通过 Django ``call_command`` 调用
``catalog.management.commands.import_catalog_json``，再只读断言 ``catalog`` ORM
结果；测试文件不会绕过领域服务直接模拟导入逻辑。
"""

from __future__ import annotations

# Python 标准库依赖：在临时受控目录构造 JSON 导入包和极小本地图片样本。
import base64
import copy
import hashlib
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

# Django 测试与命令依赖：以真实管理命令覆盖预检、写入、幂等与安全拒绝路径。
from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

# 商品目录模型依赖：仅用于验证命令经领域服务写入后的持久化结果。
from catalog.models import Category, Product, ProductSource
from catalog.selectors import list_products
from catalog.services import get_or_create_category_path, upsert_product_from_source


class ImportCatalogJsonCommandTests(TestCase):
    """验证受控 JSON 商品导入的无写入预检、幂等和安全边界。"""

    # 1×1 PNG；导入器当前只校验包内文件路径和 SHA-256，不依赖图像解码库。
    _ONE_PIXEL_PNG = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
        "g7MVTAAAAABJRU5ErkJggg=="
    )

    def setUp(self):
        """为每个测试创建独立的受控导入包，避免依赖仓库媒体文件。"""

        self._temporary_directory = TemporaryDirectory()
        self.package_root = Path(self._temporary_directory.name)
        images_directory = self.package_root / "images"
        images_directory.mkdir()
        self.image_path = images_directory / "sample.png"
        self.image_path.write_bytes(base64.b64decode(self._ONE_PIXEL_PNG))
        self.image_sha256 = hashlib.sha256(self.image_path.read_bytes()).hexdigest()

    def tearDown(self):
        """删除测试生成的导入包，保证路径逃逸用例不留下本地文件。"""

        self._temporary_directory.cleanup()
        super().tearDown()

    def _valid_manifest(self):
        """返回一份满足 1.0 契约、含本地图片校验信息的最小有效清单。"""

        return {
            "schema_version": "1.0",
            "batch_id": "catalog-test-batch-001",
            "source": ProductSource.SOURCE_JD,
            "generated_at": "2026-07-18T12:00:00+08:00",
            "generator": {"project": "shoplite-crawler-cleaner", "version": "1.0.0"},
            "products": [
                {
                    "external_id": "jd-test-10001",
                    "source_url": "https://item.jd.com/10001.html",
                    "name": "导入命令测试商品",
                    "brand": "测试品牌",
                    "category": {"level_1": "测试一级分类", "level_2": "测试二级分类"},
                    "pricing": {"price": "99.00", "original_price": "129.00", "currency": "CNY"},
                    "stock": 8,
                    "description": "来自已清洗 JSON 的安全商品描述。",
                    "specs": {"颜色": "黑色", "容量": "128GB"},
                    "images": [
                        {
                            "path": "images/sample.png",
                            "role": "main",
                            "sort_order": 0,
                            "sha256": self.image_sha256,
                        }
                    ],
                    "flags": {"is_active": True, "is_hot": True},
                    "source_metadata": {
                        "captured_at": "2026-07-18T11:30:00+08:00",
                        "content_hash": "crawler-content-hash",
                        "raw_title": "清洗前标题摘要",
                    },
                }
            ],
        }

    def _write_manifest(self, manifest, *, name="manifest.json"):
        """将调用方给出的清单写入临时包根目录并返回命令可读取的路径。"""

        manifest_path = self.package_root / name
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest_path

    def _run_command(self, manifest_path, *, dry_run=False, report_name=None):
        """执行导入命令并返回其标准输出和可选 JSON 报告内容。"""

        stdout = StringIO()
        arguments = [str(manifest_path)]
        if dry_run:
            arguments.append("--dry-run")
        report_path = None
        if report_name:
            report_path = self.package_root / report_name
            arguments.extend(["--report", str(report_path)])
        call_command("import_catalog_json", *arguments, stdout=stdout)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path else None
        return stdout.getvalue(), report

    def _assert_catalog_is_empty(self):
        """断言失败或预检路径没有遗留任何商品、来源或分类写入。"""

        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductSource.objects.count(), 0)
        self.assertEqual(Category.objects.count(), 0)

    def test_dry_run_valid_manifest_does_not_write_product_or_source(self):
        """有效清单的预检应报告创建预测，但不得写入任何目录模型。"""

        manifest_path = self._write_manifest(self._valid_manifest())

        _stdout, report = self._run_command(
            manifest_path,
            dry_run=True,
            report_name="dry-run-report.json",
        )

        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["summary"], {"total": 1, "create": 1, "update": 0, "skip": 0, "failed": 0})
        self.assertEqual(report["items"][0]["status"], "create")
        self._assert_catalog_is_empty()

    def test_import_creates_inactive_product_and_traceable_source(self):
        """正式导入应创建待审核下架商品和可追溯的外部来源记录。"""

        manifest_path = self._write_manifest(self._valid_manifest())

        _stdout, report = self._run_command(manifest_path, report_name="import-report.json")

        self.assertEqual(report["summary"], {"total": 1, "create": 1, "update": 0, "skip": 0, "failed": 0})
        product = Product.objects.get()
        source = ProductSource.objects.get()
        self.assertFalse(product.is_active)
        self.assertFalse(product.is_hot)
        self.assertEqual(product.name, "导入命令测试商品")
        self.assertEqual(product.category.name, "测试二级分类")
        self.assertEqual(source.product_id, product.id)
        self.assertEqual(source.source_type, ProductSource.SOURCE_JD)
        self.assertEqual(source.external_id, "jd-test-10001")
        self.assertEqual(source.source_payload["batch_id"], "catalog-test-batch-001")
        self.assertIn("content_hash", source.source_payload)

    def test_repeated_identical_import_is_idempotent_and_reported_as_skip(self):
        """相同来源和内容重复导入时，应保持一条记录且报告跳过而非更新。"""

        manifest_path = self._write_manifest(self._valid_manifest())
        self._run_command(manifest_path)
        source = ProductSource.objects.get()
        first_imported_at = source.first_imported_at
        last_imported_at = source.last_imported_at
        product_id = source.product_id

        _stdout, report = self._run_command(manifest_path, report_name="repeat-report.json")

        self.assertEqual(report["summary"], {"total": 1, "create": 0, "update": 0, "skip": 1, "failed": 0})
        self.assertEqual(report["items"][0]["status"], "skip")
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductSource.objects.count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.product_id, product_id)
        self.assertEqual(source.first_imported_at, first_imported_at)
        self.assertEqual(source.last_imported_at, last_imported_at)

    def test_changed_source_import_preserves_manual_operations_flags(self):
        """来源内容更新时，后台已审核的上架和运营标记不能被导入默认值覆盖。"""

        manifest_path = self._write_manifest(self._valid_manifest())
        self._run_command(manifest_path)
        product = Product.objects.get()
        product.is_active = True
        product.is_hot = True
        product.is_new = True
        product.is_recommended = True
        product.save(update_fields=["is_active", "is_hot", "is_new", "is_recommended"])

        changed_manifest = self._valid_manifest()
        changed_manifest["products"][0]["pricing"]["price"] = "88.00"
        changed_manifest_path = self._write_manifest(changed_manifest, name="changed-content.json")

        _stdout, report = self._run_command(changed_manifest_path, report_name="changed-report.json")

        self.assertEqual(report["summary"], {"total": 1, "create": 0, "update": 1, "skip": 0, "failed": 0})
        product.refresh_from_db()
        self.assertEqual(str(product.price), "88.00")
        self.assertTrue(product.is_active)
        self.assertTrue(product.is_hot)
        self.assertTrue(product.is_new)
        self.assertTrue(product.is_recommended)

    def test_sensitive_invalid_and_path_escape_inputs_fail_without_writing(self):
        """敏感元数据、非法金额和图片路径穿越都必须失败且不留下半条数据。"""

        invalid_cases = {
            "sensitive-metadata": lambda manifest: manifest["products"][0].update(
                {"source_metadata": {"token": "must-not-be-persisted"}}
            ),
            "invalid-price": lambda manifest: manifest["products"][0]["pricing"].update({"price": "-0.01"}),
            "path-traversal": lambda manifest: manifest["products"][0]["images"][0].update(
                {"path": "../outside.png"}
            ),
        }
        for case_name, mutate_manifest in invalid_cases.items():
            with self.subTest(case=case_name):
                manifest = copy.deepcopy(self._valid_manifest())
                mutate_manifest(manifest)
                manifest_path = self._write_manifest(manifest, name=f"{case_name}.json")

                with self.assertRaises(CommandError):
                    self._run_command(manifest_path)

                self._assert_catalog_is_empty()


class CatalogInventoryAndCategoryTests(TestCase):
    """验证来源库存隔离、分类唯一约束和三级分类商品的读取语义。"""

    def test_reimport_updates_source_stock_without_overwriting_sellable_stock(self):
        """订单变化后的可售库存不得被下一次爬虫导入加回。"""

        category = get_or_create_category_path(("数码", "耳机"))
        created = upsert_product_from_source(
            source_type=ProductSource.SOURCE_JD,
            external_id="source-stock-1001",
            category=category,
            product_fields={"name": "来源库存隔离商品", "price": "99.00", "stock": 8},
        )
        product = created.product
        self.assertEqual(product.stock, 8)
        self.assertEqual(product.source_stock, 8)

        Product.objects.filter(id=product.id).update(stock=3)
        updated = upsert_product_from_source(
            source_type=ProductSource.SOURCE_JD,
            external_id="source-stock-1001",
            category=category,
            product_fields={"name": "来源库存隔离商品（更新）", "price": "88.00", "stock": 12},
        )

        self.assertFalse(updated.created)
        product.refresh_from_db()
        self.assertEqual(product.stock, 3)
        self.assertEqual(product.source_stock, 12)
        self.assertEqual(str(product.price), "88.00")

    def test_source_sales_is_ignored_on_create_and_reimport(self):
        """外部历史销量不得污染或覆盖由本地订单累计的销量。"""

        category = get_or_create_category_path(("数码", "平板"))
        created = upsert_product_from_source(
            source_type=ProductSource.SOURCE_JD,
            external_id="source-sales-1001",
            category=category,
            product_fields={
                "name": "来源销量隔离商品",
                "price": "199.00",
                "stock": 8,
                "sales": 999,
            },
        )

        product = created.product
        self.assertEqual(product.sales, 0)

        # 模拟成功订单已经累计的本地销量，来源后续数据不能将其重置或覆盖。
        Product.objects.filter(id=product.id).update(sales=3)
        updated = upsert_product_from_source(
            source_type=ProductSource.SOURCE_JD,
            external_id="source-sales-1001",
            category=category,
            product_fields={
                "name": "来源销量隔离商品（更新）",
                "price": "188.00",
                "stock": 12,
                "sales": 2000,
            },
        )

        self.assertFalse(updated.created)
        product.refresh_from_db()
        self.assertEqual(product.sales, 3)
        self.assertEqual(product.source_stock, 12)
        self.assertEqual(str(product.price), "188.00")

    def test_category_parent_scope_is_unique_and_path_service_reuses_nodes(self):
        """根分类也必须唯一，同名子分类只可存在于不同父分类下。"""

        root = Category.objects.create(name="分类唯一根")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Category.objects.create(name="分类唯一根")

        other_root = Category.objects.create(name="另一分类唯一根")
        first_child = Category.objects.create(name="同名子分类", parent=root)
        second_child = Category.objects.create(name="同名子分类", parent=other_root)
        self.assertNotEqual(first_child.parent_scope, second_child.parent_scope)

        first_path = get_or_create_category_path(("导入一级", "导入二级", "导入三级"))
        second_path = get_or_create_category_path(("导入一级", "导入二级", "导入三级"))
        self.assertEqual(first_path.id, second_path.id)
        self.assertEqual(Category.objects.filter(name="导入一级").count(), 1)
        self.assertEqual(Category.objects.filter(name="导入二级").count(), 1)
        self.assertEqual(Category.objects.filter(name="导入三级").count(), 1)

    def test_deleting_parent_promotes_children_with_root_scope(self):
        """父分类 SET_NULL 时必须同步内部唯一性键，后续路径查询才能正确复用子分类。"""

        root = Category.objects.create(name="待删除父分类")
        child = Category.objects.create(name="删除后成为根分类", parent=root)

        root.delete()

        child.refresh_from_db()
        self.assertIsNone(child.parent_id)
        self.assertEqual(child.parent_scope, 0)
        reused = get_or_create_category_path((child.name,))
        self.assertEqual(reused.id, child.id)

    def test_top_and_second_level_filters_include_third_level_products(self):
        """导航只显示两级时，一级/二级筛选仍必须找到三级导入商品。"""

        electronics = get_or_create_category_path(("电子产品",))
        phones = get_or_create_category_path(("电子产品", "手机"))
        android = get_or_create_category_path(("电子产品", "手机", "安卓手机"))
        clothing = get_or_create_category_path(("服饰", "手机"))
        android_product = Product.objects.create(
            category=android,
            name="三级安卓手机",
            price="1999.00",
            stock=3,
        )
        phone_product = Product.objects.create(
            category=phones,
            name="二级手机配件",
            price="99.00",
            stock=3,
        )
        unrelated_product = Product.objects.create(
            category=clothing,
            name="服饰分类同名手机",
            price="59.00",
            stock=3,
        )

        top_level_ids = {product.id for product in list_products(category_name=electronics.name)}
        scoped_second_level_ids = {
            product.id
            for product in list_products(category_name=phones.name, parent_category_name=electronics.name)
        }

        self.assertEqual(top_level_ids, {android_product.id, phone_product.id})
        self.assertEqual(scoped_second_level_ids, {android_product.id, phone_product.id})
        self.assertNotIn(unrelated_product.id, scoped_second_level_ids)
