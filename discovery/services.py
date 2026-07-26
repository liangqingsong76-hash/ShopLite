"""快速找商品的统一意图解析与匹配服务。

依赖流向：调用方 -> ``discover_products`` -> 本地文本解析或图片 provider ->
``discovery.selectors`` -> ``catalog`` 只读 ORM。语音入口只接收上游已经转写的
文本，本包不绑定任何云语音 SDK。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .contracts import (
    DiscoveryRequest,
    DiscoveryResponse,
    DiscoverySource,
    SearchIntent,
)
from .exceptions import (
    DiscoveryError,
    InvalidDiscoveryInput,
    ProviderFailure,
)
from .providers import get_image_intent_provider, get_text_intent_provider
from .selectors import catalog_facets, find_product_matches


DEFAULT_RESULT_LIMIT = 12
MAX_RESULT_LIMIT = 24
MAX_QUERY_CHARACTERS = 500
MAX_PROVIDER_KEYWORDS = 12
MAX_PRICE = Decimal("99999999.99")

_SPACE_PATTERN = re.compile(r"\s+")
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{1,31}|[\u4e00-\u9fff]{2,40}")
_RANGE_PATTERNS = (
    re.compile(
        r"(?P<min>\d+(?:\.\d{1,2})?)\s*(?:元)?\s*(?:到|至|~|～|-|—)\s*"
        r"(?P<max>\d+(?:\.\d{1,2})?)\s*(?:元)?"
    ),
)
_MAX_PRICE_PATTERNS = (
    re.compile(r"(?:预算|价格|价位)?\s*(?P<value>\d+(?:\.\d{1,2})?)\s*元?\s*(?:以内|以下|之内|内|封顶)"),
    re.compile(r"(?:不超过|低于|少于|最多)\s*(?P<value>\d+(?:\.\d{1,2})?)\s*元?"),
    re.compile(r"预算\s*(?P<value>\d+(?:\.\d{1,2})?)\s*元?"),
)
_MIN_PRICE_PATTERNS = (
    re.compile(r"(?:价格|价位)?\s*(?P<value>\d+(?:\.\d{1,2})?)\s*元?\s*(?:以上|起)"),
    re.compile(r"(?:不低于|高于|超过|至少)\s*(?P<value>\d+(?:\.\d{1,2})?)\s*元?"),
)

_ACCESSIBLE_TERMS = (
    "大字体",
    "大字",
    "大屏",
    "声音大",
    "音量大",
    "语音播报",
    "语音控制",
    "操作简单",
    "简单易用",
    "一键操作",
    "防滑",
    "轻便",
    "护眼",
    "高对比度",
    "无糖",
    "低糖",
    "低盐",
)
_PRODUCT_TERMS = (
    "老人机",
    "智能手机",
    "手机",
    "助听器",
    "血压计",
    "血糖仪",
    "保温杯",
    "收音机",
    "音箱",
    "拐杖",
    "轮椅",
    "放大镜",
    "眼镜",
    "药盒",
    "手环",
    "按摩器",
    "双肩包",
    "鼠标",
)
_NOISE_PHRASES = (
    "帮我找一下",
    "帮我找",
    "请帮我找",
    "我想购买",
    "我想买",
    "想购买",
    "想买",
    "我要买",
    "需要一个",
    "需要一款",
    "有没有",
    "适合老年人用的",
    "适合老人用的",
    "给老年人用的",
    "给老人用的",
    "适合老年人",
    "适合老人",
    "老年人用",
    "老人用",
    "请推荐",
    "推荐",
    "查找",
    "寻找",
    "商品",
    "产品",
    "左右",
    "大约",
    "最好",
    "比较",
)
_IGNORED_TOKENS = {
    "一个",
    "一款",
    "一些",
    "这个",
    "那个",
    "可以",
    "能够",
    "需要",
    "想要",
    "老人",
    "老年人",
    "使用",
    "购买",
    "买",
    "找",
    "的",
}


def discover_products(request, *, text_provider=None, image_provider=None):
    """执行统一商品发现流程并返回没有写业务副作用的结果。

    ``text_provider`` 与 ``image_provider`` 仅用于依赖注入和测试；正常运行
    分别从对应设置加载实现。文字 provider 未配置时稳定回退到本地规则解析器。
    """

    if not isinstance(request, DiscoveryRequest):
        raise InvalidDiscoveryInput("商品发现请求格式无效")
    source = _normalized_source(request.source)
    limit = _validated_limit(request.limit)

    if source in {DiscoverySource.TEXT, DiscoverySource.VOICE_TRANSCRIPT}:
        if request.image is not None:
            raise InvalidDiscoveryInput("文字或语音转写请求不能同时包含图片")
        normalized_query = _validated_query(request.query)
        provider = get_text_intent_provider(text_provider)
        if provider is None:
            intent = parse_text_intent(normalized_query)
        else:
            try:
                intent = provider.extract_intent(normalized_query)
            except DiscoveryError:
                raise
            except Exception as exc:
                raise ProviderFailure(
                    "文字意图服务暂时异常，请稍后重试。",
                    provider=str(getattr(provider, "name", "unknown")),
                ) from exc
            intent = _validated_provider_intent(
                intent,
                provider,
                input_query=normalized_query,
                source_label="文字意图",
            )
    elif source is DiscoverySource.IMAGE:
        if request.image is None or not request.image.data:
            raise InvalidDiscoveryInput("请选择需要识别的商品图片")
        if request.query:
            raise InvalidDiscoveryInput("图片请求不能同时携带查询文本")
        provider = get_image_intent_provider(image_provider)
        try:
            intent = provider.extract_intent(request.image)
        except DiscoveryError:
            raise
        except Exception as exc:
            raise ProviderFailure(
                "图片识别服务暂时异常，请稍后重试。",
                provider=str(getattr(provider, "name", "unknown")),
            ) from exc
        intent = _validated_provider_intent(
            intent,
            provider,
            input_query="",
            source_label="图片识别",
        )
    else:  # pragma: no cover - 枚举校验已覆盖，仅防未来新增来源忘记实现。
        raise InvalidDiscoveryInput("暂不支持该商品发现来源")

    results = find_product_matches(intent, limit=limit)
    return DiscoveryResponse(source=source, intent=intent, results=results)


def discover_from_text(query, *, limit=DEFAULT_RESULT_LIMIT):
    """便捷入口：从普通文字描述匹配商品。"""

    return discover_products(
        DiscoveryRequest(source=DiscoverySource.TEXT, query=query, limit=limit)
    )


def discover_from_voice_transcript(transcript, *, limit=DEFAULT_RESULT_LIMIT):
    """便捷入口：从外部语音服务已经转写的文本匹配商品。"""

    return discover_products(
        DiscoveryRequest(
            source=DiscoverySource.VOICE_TRANSCRIPT,
            query=transcript,
            limit=limit,
        )
    )


def parse_text_intent(query):
    """用本地确定性规则提取关键词、品牌、分类、预算和适老属性。"""

    normalized_query = _validated_query(query)
    facets = catalog_facets()
    brand = _longest_mentioned_value(normalized_query, facets["brands"])
    category = _longest_mentioned_value(normalized_query, facets["categories"])
    price_min, price_max, price_spans = _extract_price_range(normalized_query)
    attributes = tuple(term for term in _ACCESSIBLE_TERMS if term in normalized_query)
    keywords = _extract_keywords(
        normalized_query,
        brand=brand,
        category=category,
        attributes=attributes,
        price_spans=price_spans,
    )

    if not any((keywords, brand, category, price_min is not None, price_max is not None, attributes)):
        raise InvalidDiscoveryInput("请说得更具体一些，例如商品名称、用途、品牌或预算。")
    return SearchIntent(
        query=normalized_query,
        keywords=keywords,
        brand=brand,
        category=category,
        price_min=price_min,
        price_max=price_max,
        attributes=attributes,
    )


def _validated_query(query):
    """清理单行查询并拒绝空白、非文本和过长输入。"""

    if not isinstance(query, str):
        raise InvalidDiscoveryInput("查询内容必须是文字")
    normalized = _SPACE_PATTERN.sub(" ", query).strip()
    if not normalized:
        raise InvalidDiscoveryInput("请输入想找的商品")
    if len(normalized) > MAX_QUERY_CHARACTERS:
        raise InvalidDiscoveryInput(f"查询内容不能超过 {MAX_QUERY_CHARACTERS} 个字符")
    return normalized


def _validated_limit(limit):
    """把调用方结果数量限制在稳定的小范围内。"""

    if isinstance(limit, bool):
        raise InvalidDiscoveryInput("结果数量格式无效")
    try:
        parsed = int(limit)
    except (TypeError, ValueError) as exc:
        raise InvalidDiscoveryInput("结果数量格式无效") from exc
    if parsed < 1 or parsed > MAX_RESULT_LIMIT:
        raise InvalidDiscoveryInput(f"结果数量必须在 1 到 {MAX_RESULT_LIMIT} 之间")
    return parsed


def _normalized_source(source):
    """接受枚举或其字符串值，并拒绝未知来源。"""

    try:
        return source if isinstance(source, DiscoverySource) else DiscoverySource(source)
    except (TypeError, ValueError) as exc:
        raise InvalidDiscoveryInput("商品发现来源无效") from exc


def _longest_mentioned_value(query, values):
    """从数据库已知值中选择查询里最长的完整品牌或分类名称。"""

    normalized_query = query.casefold()
    matches = [
        str(value)
        for value in values
        if value and str(value).casefold() in normalized_query
    ]
    return max(matches, key=len) if matches else None


def _extract_price_range(query):
    """提取价格上下限，并返回应从关键词文本移除的字符区间。"""

    for pattern in _RANGE_PATTERNS:
        match = pattern.search(query)
        if not match:
            continue
        price_min = _decimal_price(match.group("min"))
        price_max = _decimal_price(match.group("max"))
        if price_min > price_max:
            price_min, price_max = price_max, price_min
        return price_min, price_max, (match.span(),)

    price_min = None
    price_max = None
    spans = []
    for pattern in _MIN_PRICE_PATTERNS:
        match = pattern.search(query)
        if match:
            price_min = _decimal_price(match.group("value"))
            spans.append(match.span())
            break
    for pattern in _MAX_PRICE_PATTERNS:
        match = pattern.search(query)
        if match:
            price_max = _decimal_price(match.group("value"))
            spans.append(match.span())
            break
    if price_min is not None and price_max is not None and price_min > price_max:
        raise InvalidDiscoveryInput("最低价格不能高于最高价格")
    return price_min, price_max, tuple(spans)


def _decimal_price(raw_value):
    """把外部价格解析为有限、非负且不超出商品字段范围的金额。"""

    try:
        value = Decimal(raw_value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidDiscoveryInput("价格格式无效") from exc
    if not value.is_finite() or value < 0 or value > MAX_PRICE:
        raise InvalidDiscoveryInput("价格超出允许范围")
    return value.quantize(Decimal("0.01"))


def _extract_keywords(query, *, brand, category, attributes, price_spans):
    """从查询中提取有限关键词，兼顾中文自然表达和数据库查询成本。"""

    working = query
    for start, end in sorted(price_spans, reverse=True):
        working = f"{working[:start]} {working[end:]}"
    for value in (brand, category, *attributes):
        if value:
            working = re.sub(re.escape(value), " ", working, flags=re.IGNORECASE)
    for phrase in _NOISE_PHRASES:
        working = working.replace(phrase, " ")

    keywords = []
    for term in _PRODUCT_TERMS:
        if term in query and term not in attributes:
            keywords.append(term)
            working = working.replace(term, " ")
    for token in _TOKEN_PATTERN.findall(working):
        cleaned = token.strip("，。！？、,.!?;；:：的了呀啊呢")
        if (
            len(cleaned) >= 2
            and cleaned.casefold() not in _IGNORED_TOKENS
            and not cleaned.isdigit()
        ):
            keywords.append(cleaned)
    return _unique_limited(keywords, MAX_PROVIDER_KEYWORDS)


def _validated_provider_intent(intent, provider, *, input_query, source_label):
    """验证外部 provider 输出，阻止异常对象进入 ORM 查询构造。"""

    provider_name = str(getattr(provider, "name", "unknown"))
    if not isinstance(intent, SearchIntent):
        raise ProviderFailure(
            f"{source_label}服务返回了无效结果。",
            provider=provider_name,
        )
    query = _SPACE_PATTERN.sub(" ", str(intent.query or input_query or "")).strip()
    if len(query) > MAX_QUERY_CHARACTERS:
        raise ProviderFailure(f"{source_label}结果过长。", provider=provider_name)
    try:
        keywords = _unique_limited(intent.keywords, MAX_PROVIDER_KEYWORDS)
        attributes = _unique_limited(intent.attributes, MAX_PROVIDER_KEYWORDS)
    except InvalidDiscoveryInput as exc:
        raise ProviderFailure(
            f"{source_label}服务返回了无效关键词。",
            provider=provider_name,
        ) from exc
    brand = _optional_short_text(intent.brand, "品牌", provider_name)
    category = _optional_short_text(intent.category, "分类", provider_name)
    price_min = _optional_provider_price(intent.price_min, provider_name)
    price_max = _optional_provider_price(intent.price_max, provider_name)
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ProviderFailure("图片识别返回了无效价格范围。", provider=provider_name)
    if not any((keywords, attributes, brand, category, price_min is not None, price_max is not None)):
        raise ProviderFailure(
            f"{source_label}结果中没有可用于找商品的信息。",
            provider=provider_name,
        )
    return SearchIntent(
        query=query,
        keywords=keywords,
        brand=brand,
        category=category,
        price_min=price_min,
        price_max=price_max,
        attributes=attributes,
    )


def _optional_short_text(value, label, provider_name):
    """校验 provider 可选短文本字段。"""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise ProviderFailure(
            f"图片识别返回了无效{label}。",
            provider=provider_name,
        )
    return value.strip()


def _optional_provider_price(value, provider_name):
    """校验 provider 可选价格字段并转换为金额。"""

    if value is None:
        return None
    try:
        return _decimal_price(value)
    except InvalidDiscoveryInput as exc:
        raise ProviderFailure(
            "图片识别返回了无效价格。",
            provider=provider_name,
        ) from exc


def _unique_limited(values, limit):
    """校验、清理并稳定去重 provider 或本地解析器的关键词。"""

    if isinstance(values, str):
        values = (values,)
    try:
        iterator = iter(values or ())
    except TypeError as exc:
        raise InvalidDiscoveryInput("关键词格式无效") from exc
    result = []
    seen = set()
    for raw_value in iterator:
        if not isinstance(raw_value, str):
            raise InvalidDiscoveryInput("关键词格式无效")
        value = raw_value.strip()
        key = value.casefold()
        if not value or len(value) > 40 or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return tuple(result)
