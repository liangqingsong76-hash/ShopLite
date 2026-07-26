"""快速找商品的稳定输入、意图和输出契约。

三种入口（文字、语音转写文本、图片）都转换为 ``SearchIntent``，再进入同一
只读匹配流程。这样未来更换语音转写或视觉模型时，只需实现 provider，不需要
修改商品筛选、评分或页面层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class DiscoverySource(str, Enum):
    """标识用户意图来自哪一种受支持的输入。"""

    TEXT = "text"
    VOICE_TRANSCRIPT = "voice_transcript"
    IMAGE = "image"


@dataclass(frozen=True)
class ImageInput:
    """只存在于当前请求内存中的图片字节及其非敏感元数据。"""

    data: bytes
    content_type: str
    filename: str = ""


@dataclass(frozen=True)
class DiscoveryRequest:
    """统一的商品发现请求；不同来源只能使用与其对应的输入字段。"""

    source: DiscoverySource
    query: str = ""
    image: ImageInput | None = None
    limit: int = 12


@dataclass(frozen=True)
class SearchIntent:
    """由本地解析器或受信 provider 生成的结构化购买意图。"""

    query: str
    keywords: tuple[str, ...] = ()
    brand: str | None = None
    category: str | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    attributes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """返回可直接进入 JSON 响应的非敏感意图摘要。"""

        return {
            "query": self.query,
            "keywords": list(self.keywords),
            "brand": self.brand,
            "category": self.category,
            "price_min": str(self.price_min) if self.price_min is not None else None,
            "price_max": str(self.price_max) if self.price_max is not None else None,
            "attributes": list(self.attributes),
        }


@dataclass(frozen=True)
class ProductMatch:
    """一个商品匹配结果及其可解释的命中原因。"""

    id: int
    name: str
    brand: str
    category: str
    price: Decimal
    original_price: Decimal | None
    image: str
    rating: Decimal
    sales: int
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """序列化商品快照，不暴露库存、来源元数据或内部对象。"""

        return {
            "id": self.id,
            "name": self.name,
            "brand": self.brand,
            "category": self.category,
            "price": str(self.price),
            "original_price": (
                str(self.original_price) if self.original_price is not None else None
            ),
            "image": self.image,
            "rating": str(self.rating),
            "sales": self.sales,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DiscoveryResponse:
    """统一的商品发现响应，供 API 与服务端页面共同消费。"""

    source: DiscoverySource
    intent: SearchIntent
    results: tuple[ProductMatch, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """返回稳定的响应对象结构。"""

        serialized_results = [item.as_dict() for item in self.results]
        return {
            "source": self.source.value,
            "intent": self.intent.as_dict(),
            "count": len(serialized_results),
            "results": serialized_results,
        }
