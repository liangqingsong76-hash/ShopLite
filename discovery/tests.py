"""快速找商品领域与 API 的安全回归测试。

依赖流向：测试调用公开服务或独立 URL -> ``discovery`` -> ``catalog`` 测试数据；
测试 provider 只返回受控 ``SearchIntent``，不连接任何外部 AI、语音或视觉服务。
"""

from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from PIL import Image

from catalog.models import Category, Product

from .contracts import (
    DiscoveryRequest,
    DiscoverySource,
    ImageInput,
    SearchIntent,
)
from .exceptions import FeatureUnavailable, InvalidDiscoveryInput, ProviderFailure
from .providers import (
    ImageIntentProvider,
    TextIntentProvider,
    image_provider_availability,
)
from .selectors import MAX_CANDIDATES, find_product_matches
from .services import discover_from_text, discover_products, parse_text_intent


# API 测试使用本模块作为最小根路由，避免依赖 storefront 的兼容路径。
urlpatterns = [
    path("api/discovery/", include("discovery.urls")),
]


class StubTextIntentProvider(TextIntentProvider):
    """测试用文字 provider，证明未来 AI 只需输出统一结构契约。"""

    name = "stub-text-ai"

    def extract_intent(self, query):
        """返回一个不访问网络的固定受控意图。"""

        return SearchIntent(query=query, keywords=("保温杯",), price_max=Decimal("100"))


class StubImageIntentProvider(ImageIntentProvider):
    """测试用图片 provider，不解析或保存原始图片。"""

    name = "stub-vision"

    def extract_intent(self, image):
        """验证内存字节存在后返回固定意图。"""

        if not image.data:
            raise AssertionError("测试图片应以内存字节传入")
        return SearchIntent(query="图片识别：保温杯", keywords=("保温杯",))


class BrokenImageIntentProvider(ImageIntentProvider):
    """模拟违反输出契约的外部视觉 provider。"""

    name = "broken-vision"

    def extract_intent(self, image):
        """故意返回错误类型，验证服务不会把它用于 ORM 查询。"""

        return {"keyword": "保温杯"}


class CapturingImageIntentProvider(ImageIntentProvider):
    """记录 API 传入的图片元数据，验证格式规范化但不保留真实生产图片。"""

    name = "capturing-vision"
    received_image = None

    def extract_intent(self, image):
        """仅在测试进程内保存引用并返回固定意图。"""

        type(self).received_image = image
        return SearchIntent(query="图片识别：保温杯", keywords=("保温杯",))


class UnreadyImageIntentProvider(ImageIntentProvider):
    """模拟类可导入但凭据等本地运行条件未准备好的 provider。"""

    name = "unready-vision"

    def is_available(self):
        """明确报告部署条件尚未满足。"""

        return False

    def extract_intent(self, image):  # pragma: no cover - 就绪检查必须先阻止调用。
        """若被调用说明就绪保护失效。"""

        raise AssertionError("未就绪 provider 不应收到图片")


class BrokenAvailabilityImageIntentProvider(ImageIntentProvider):
    """模拟 provider 就绪检查本身发生异常。"""

    name = "broken-availability-vision"

    def is_available(self):
        """模拟本地凭据读取或初始化失败。"""

        raise RuntimeError("sensitive provider detail")

    def extract_intent(self, image):  # pragma: no cover - 就绪检查必须先阻止调用。
        """若被调用说明就绪保护失效。"""

        raise AssertionError("就绪检查失败的 provider 不应收到图片")


class DiscoveryServiceTests(TestCase):
    """验证意图解析、只读匹配、有界候选和 provider 降级。"""

    @classmethod
    def setUpTestData(cls):
        """创建包含上架、下架和不同价格商品的最小目录。"""

        digital = Category.objects.create(name="数码产品")
        phones = Category.objects.create(name="智能手机", parent=digital)
        daily = Category.objects.create(name="日用百货")
        cls.phone = Product.objects.create(
            category=phones,
            name="华为大字大音量智能手机",
            brand="华为",
            price=Decimal("1999.00"),
            description="大字体、高对比度、声音大，操作简单。",
            specs='{"屏幕": "大屏", "功能": "语音播报"}',
            is_active=True,
            is_recommended=True,
        )
        cls.cup = Product.objects.create(
            category=daily,
            name="便携保温杯",
            brand="ShopLite",
            price=Decimal("89.00"),
            description="轻便防滑，容易握持。",
            is_active=True,
        )
        cls.inactive = Product.objects.create(
            category=phones,
            name="华为已下架手机",
            brand="华为",
            price=Decimal("999.00"),
            is_active=False,
        )

    def test_local_intent_extracts_catalog_facets_budget_and_accessible_needs(self):
        """自然表达应提取品牌、商品词、预算与适老属性。"""

        intent = parse_text_intent("帮我找适合老人的华为手机，预算 2000 元以内，声音大")

        self.assertEqual(intent.brand, "华为")
        self.assertEqual(intent.price_max, Decimal("2000.00"))
        self.assertIn("手机", intent.keywords)
        self.assertIn("声音大", intent.attributes)

    def test_text_discovery_matches_only_active_product_and_explains_score(self):
        """匹配结果不包含下架商品，并说明品牌、名称或预算命中原因。"""

        response = discover_from_text(
            "华为手机，2000 元以内，声音大",
            limit=5,
        )

        self.assertEqual([item.id for item in response.results], [self.phone.id])
        self.assertTrue(
            any("品牌符合" in reason for reason in response.results[0].reasons)
        )
        self.assertTrue(
            any("价格不超过" in reason for reason in response.results[0].reasons)
        )

    def test_text_discovery_has_no_catalog_write_side_effect(self):
        """服务查询前后目录记录数量和更新时间不发生变化。"""

        before_count = Product.objects.count()
        before_updated_at = self.cup.updated_at

        discover_from_text("100 元以内的保温杯")

        self.cup.refresh_from_db()
        self.assertEqual(Product.objects.count(), before_count)
        self.assertEqual(self.cup.updated_at, before_updated_at)

    def test_default_text_rules_work_without_ai_provider(self):
        """没有配置 AI provider 时，本地规则仍提供基础文字找商品。"""

        response = discover_products(
            DiscoveryRequest(
                source=DiscoverySource.TEXT,
                query="保温杯 100 元以下",
            )
        )

        self.assertEqual(response.results[0].id, self.cup.id)

    def test_injected_text_provider_uses_same_search_intent_contract(self):
        """可插拔文字 provider 输出应复用同一匹配和结果契约。"""

        response = discover_products(
            DiscoveryRequest(source=DiscoverySource.TEXT, query="给我推荐喝水的"),
            text_provider=StubTextIntentProvider(),
        )

        self.assertEqual(response.intent.keywords, ("保温杯",))
        self.assertEqual(response.results[0].id, self.cup.id)

    def test_unconfigured_image_provider_fails_explicitly(self):
        """默认图片 provider 必须报未配置，而不是根据文件名猜测商品。"""

        with self.assertRaises(FeatureUnavailable) as context:
            discover_products(
                DiscoveryRequest(
                    source=DiscoverySource.IMAGE,
                    image=ImageInput(b"image-bytes", "image/png", "保温杯.png"),
                )
            )

        self.assertEqual(context.exception.provider, "unconfigured")
        self.assertIn("TODO", context.exception.todo)

    def test_image_provider_can_match_through_unified_contract(self):
        """真实视觉 provider 将来只需返回 SearchIntent 即可复用本地匹配。"""

        response = discover_products(
            DiscoveryRequest(
                source=DiscoverySource.IMAGE,
                image=ImageInput(b"image-bytes", "image/png", "unknown.png"),
            ),
            image_provider=StubImageIntentProvider(),
        )

        self.assertEqual(response.results[0].id, self.cup.id)

    def test_broken_provider_output_is_rejected_before_query(self):
        """违反契约的 provider 输出应作为上游故障处理。"""

        with self.assertRaises(ProviderFailure):
            discover_products(
                DiscoveryRequest(
                    source=DiscoverySource.IMAGE,
                    image=ImageInput(b"image-bytes", "image/png"),
                ),
                image_provider=BrokenImageIntentProvider(),
            )

    def test_result_limit_and_vague_query_are_validated(self):
        """结果数量越界或没有可提取意图的空泛描述应给出可读错误。"""

        with self.assertRaises(InvalidDiscoveryInput):
            discover_from_text("帮我找商品")
        with self.assertRaises(InvalidDiscoveryInput):
            discover_from_text("保温杯", limit=25)

    def test_candidate_constant_is_safely_bounded(self):
        """候选上限必须保持有限，避免未来大目录查询失控。"""

        self.assertLessEqual(MAX_CANDIDATES, 200)

    def test_sql_relevance_keeps_strong_match_beyond_two_hundred_weak_matches(self):
        """强标题匹配即使创建较晚，也必须在候选截断前越过 200 个描述弱匹配。"""

        Product.objects.bulk_create(
            [
                Product(
                    category=self.cup.category,
                    name=f"普通水具 {index:03d}",
                    brand="普通品牌",
                    price=Decimal("80.00"),
                    description="精准保温杯",
                    stock=10,
                    is_active=True,
                )
                for index in range(MAX_CANDIDATES + 1)
            ]
        )
        exact = Product.objects.create(
            category=self.cup.category,
            name="精准保温杯",
            brand="精准品牌",
            price=Decimal("90.00"),
            stock=10,
            is_active=True,
        )

        matches = find_product_matches(
            SearchIntent(query="精准保温杯", keywords=("精准保温杯",)),
            limit=1,
        )

        self.assertEqual(matches[0].id, exact.id)

    def test_out_of_stock_products_are_never_returned(self):
        """已上架但库存为零的商品不能出现在智能匹配结果。"""

        sold_out = Product.objects.create(
            category=self.cup.category,
            name="库存测试专用水杯",
            price=Decimal("30.00"),
            stock=0,
            is_active=True,
        )

        matches = find_product_matches(
            SearchIntent(
                query="库存测试专用水杯",
                keywords=("库存测试专用水杯",),
            ),
            limit=5,
        )

        self.assertNotIn(sold_out.id, [item.id for item in matches])

    def test_three_level_category_path_is_preloaded_in_one_query(self):
        """三级分类匹配和结果路径组装应只执行一条查询，避免 parent N+1。"""

        root = Category.objects.create(name="健康养老")
        middle = Category.objects.create(name="居家照护", parent=root)
        leaf = Category.objects.create(name="起居辅助", parent=middle)
        Product.objects.bulk_create(
            [
                Product(
                    category=leaf,
                    name=f"起居辅助用品 {index}",
                    price=Decimal("50.00"),
                    stock=5,
                    is_active=True,
                )
                for index in range(2)
            ]
        )

        with self.assertNumQueries(1):
            matches = find_product_matches(
                SearchIntent(query="健康养老", category="健康养老"),
                limit=5,
            )

        self.assertEqual(len(matches), 2)
        self.assertTrue(
            all(item.category == "健康养老 / 居家照护 / 起居辅助" for item in matches)
        )

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER="discovery.tests.UnreadyImageIntentProvider"
    )
    def test_importable_but_unready_image_provider_is_not_advertised(self):
        """provider 可导入但本地就绪检查失败时，页面能力状态仍必须为不可用。"""

        availability = image_provider_availability()

        self.assertFalse(availability.available)
        self.assertEqual(availability.provider, "unready-vision")
        self.assertNotIn("sensitive", availability.message)

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER=(
            "discovery.tests.BrokenAvailabilityImageIntentProvider"
        )
    )
    def test_provider_availability_exception_is_safely_degraded(self):
        """就绪检查异常不能泄露底层错误，也不能误开放图片能力。"""

        availability = image_provider_availability()

        self.assertFalse(availability.available)
        self.assertEqual(availability.provider, "broken-availability-vision")
        self.assertNotIn("sensitive provider detail", availability.message)


@override_settings(ROOT_URLCONF="discovery.tests")
class DiscoveryApiTests(TestCase):
    """验证三种 API 的认证、方法、限流、载荷和降级响应。"""

    @classmethod
    def setUpTestData(cls):
        """创建一个登录用户和可匹配商品。"""

        cls.user = get_user_model().objects.create_user(
            username="discovery-user",
            password="Strong-Test-Password-2026",
        )
        category = Category.objects.create(name="生活用品")
        cls.product = Product.objects.create(
            category=category,
            name="轻便防滑保温杯",
            brand="ShopLite",
            price=Decimal("89.00"),
            description="适合日常喝水。",
            is_active=True,
        )

    def setUp(self):
        """清理限流缓存，确保测试相互独立。"""

        cache.clear()

    def _post_json(self, url_name, payload):
        """向命名路由提交 UTF-8 JSON。"""

        return self.client.post(
            reverse(url_name),
            data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

    def _png_upload(self, *, name="sample.png"):
        """在内存生成一个极小 PNG，不创建测试文件。"""

        buffer = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(buffer, format="PNG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")

    def test_api_requires_login_and_post(self):
        """未登录 POST 返回 JSON 401，已登录 GET 返回 405。"""

        response = self._post_json("discovery:text-search", {"query": "保温杯"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

        self.client.force_login(self.user)
        response = self.client.get(reverse("discovery:text-search"))
        self.assertEqual(response.status_code, 405)

    def test_text_and_voice_endpoints_use_distinct_input_fields(self):
        """文字和语音转写入口都返回统一结构，同时保留来源标识。"""

        self.client.force_login(self.user)
        text_response = self._post_json(
            "discovery:text-search",
            {"query": "100 元以内的保温杯", "limit": 5},
        )
        voice_response = self._post_json(
            "discovery:voice-search",
            {"transcript": "声音大一些的保温杯", "limit": 5},
        )

        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(text_response.json()["source"], "text")
        self.assertEqual(text_response.json()["results"][0]["id"], self.product.id)
        self.assertEqual(voice_response.status_code, 200)
        self.assertEqual(voice_response.json()["source"], "voice_transcript")

    def test_voice_endpoint_rejects_audio_payload(self):
        """语音 API 只接收转写文本，不在后端隐式接收音频。"""

        self.client.force_login(self.user)
        response = self._post_json(
            "discovery:voice-search",
            {"audio": "base64-audio"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("转写文本", response.json()["error"]["message"])

    @override_settings(DISCOVERY_IMAGE_INTENT_PROVIDER="")
    def test_image_endpoint_reports_provider_todo_when_unconfigured(self):
        """图片能力未配置时返回 provider 名称和明确 TODO。"""

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("discovery:image-search"),
            {"image": self._png_upload()},
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "feature_unavailable")
        self.assertEqual(payload["provider"], "unconfigured")
        self.assertIn("DISCOVERY_IMAGE_INTENT_PROVIDER", payload["todo"])

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER="discovery.tests.StubImageIntentProvider"
    )
    def test_configured_image_provider_returns_normal_results(self):
        """配置视觉 provider 后，图片入口复用统一商品结果结构。"""

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("discovery:image-search"),
            {"image": self._png_upload(), "limit": "5"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "image")
        self.assertEqual(response.json()["results"][0]["id"], self.product.id)

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER=(
            "discovery.tests.CapturingImageIntentProvider"
        )
    )
    def test_image_metadata_is_normalized_to_verified_format_for_provider(self):
        """provider 收到的 MIME 和扩展名必须来自实际图片格式而不是可伪造文件名。"""

        CapturingImageIntentProvider.received_image = None
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("discovery:image-search"),
            {"image": self._png_upload(name="photo.not-really-jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        received = CapturingImageIntentProvider.received_image
        self.assertIsNotNone(received)
        self.assertEqual(received.content_type, "image/png")
        self.assertEqual(received.filename, "photo.png")

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER=(
            "discovery.tests.CapturingImageIntentProvider"
        )
    )
    def test_declared_image_mime_must_match_verified_format(self):
        """声明为 JPEG 的 PNG 必须在进入 provider 前被拒绝。"""

        CapturingImageIntentProvider.received_image = None
        self.client.force_login(self.user)
        upload = self._png_upload(name="mismatch.jpg")
        upload.content_type = "image/jpeg"
        response = self.client.post(
            reverse("discovery:image-search"),
            {"image": upload},
        )

        self.assertEqual(response.status_code, 415)
        self.assertIsNone(CapturingImageIntentProvider.received_image)

    @override_settings(
        DISCOVERY_TEXT_INTENT_PROVIDER="discovery.tests.StubTextIntentProvider"
    )
    def test_configured_text_provider_is_used_by_api(self):
        """配置未来 AI provider 后，API 不需要改变请求或响应字段。"""

        self.client.force_login(self.user)
        response = self._post_json(
            "discovery:text-search",
            {"query": "帮我推荐一个喝水用的"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"]["keywords"], ["保温杯"])

    def test_invalid_or_oversized_image_is_rejected_before_provider(self):
        """损坏图片和超过 2 MB 的文件都不能进入 provider。"""

        self.client.force_login(self.user)
        invalid = SimpleUploadedFile(
            "invalid.png",
            b"not-an-image",
            content_type="image/png",
        )
        invalid_response = self.client.post(
            reverse("discovery:image-search"),
            {"image": invalid},
        )
        self.assertEqual(invalid_response.status_code, 400)

        oversized = SimpleUploadedFile(
            "large.png",
            b"x" * (2 * 1024 * 1024 + 1),
            content_type="image/png",
        )
        oversized_response = self.client.post(
            reverse("discovery:image-search"),
            {"image": oversized},
        )
        self.assertEqual(oversized_response.status_code, 413)

    @override_settings(DISCOVERY_TEXT_RATE_LIMIT=1)
    def test_rate_limit_is_scoped_to_logged_in_user(self):
        """同一登录用户超过配置次数时收到 429 和重试时间。"""

        self.client.force_login(self.user)
        first = self._post_json("discovery:text-search", {"query": "保温杯"})
        second = self._post_json("discovery:text-search", {"query": "保温杯"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)

    @override_settings(DISCOVERY_TEXT_RATE_LIMIT=1)
    def test_invalid_payload_does_not_consume_rate_limit_allowance(self):
        """格式错误的基础载荷应先返回 400，随后首个有效请求仍可成功。"""

        self.client.force_login(self.user)
        invalid = self._post_json("discovery:text-search", {"query": ""})
        first_valid = self._post_json(
            "discovery:text-search",
            {"query": "保温杯"},
        )
        second_valid = self._post_json(
            "discovery:text-search",
            {"query": "保温杯"},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(first_valid.status_code, 200)
        self.assertEqual(second_valid.status_code, 429)

    @override_settings(
        DISCOVERY_IMAGE_INTENT_PROVIDER="discovery.tests.UnreadyImageIntentProvider"
    )
    def test_unready_configured_image_provider_returns_safe_503(self):
        """配置路径有效但 provider 未就绪时，API 必须安全降级而不是尝试识别。"""

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("discovery:image-search"),
            {"image": self._png_upload()},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["provider"], "unready-vision")
