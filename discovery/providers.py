"""可插拔的文字与图片意图识别 provider。

依赖流向：``discovery.services`` -> 本模块 -> 环境配置指定的 provider。
provider 只能把文字或内存图片转换为 ``SearchIntent``；商品查询仍由本地服务
完成，原始输入不得由本包写入模型、媒体目录或日志。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from inspect import isclass

from django.conf import settings
from django.utils.module_loading import import_string

from .contracts import ImageInput, SearchIntent
from .exceptions import FeatureUnavailable, ProviderFailure


IMAGE_PROVIDER_SETTING = "DISCOVERY_IMAGE_INTENT_PROVIDER"
TEXT_PROVIDER_SETTING = "DISCOVERY_TEXT_INTENT_PROVIDER"
IMAGE_PROVIDER_TODO = (
    "TODO(discovery): 配置 DISCOVERY_IMAGE_INTENT_PROVIDER 为实现 "
    "discovery.providers.ImageIntentProvider 的视觉识别类或实例导入路径。"
)


@dataclass(frozen=True)
class ProviderAvailability:
    """页面可安全消费的 provider 可用性摘要。"""

    available: bool
    provider: str
    message: str = ""
    todo: str = ""


class TextIntentProvider(ABC):
    """自然语言意图适配器接口，未来 AI 实现必须输出受控结构。"""

    name = "unnamed-text-provider"

    @abstractmethod
    def extract_intent(self, query: str) -> SearchIntent:
        """把规范化文本转换为意图，不直接查询或写入商品数据。"""


class ImageIntentProvider(ABC):
    """视觉模型适配器接口；实现方不得保存或记录原始图片。"""

    name = "unnamed-image-provider"

    @abstractmethod
    def extract_intent(self, image: ImageInput) -> SearchIntent:
        """从内存图片提取结构化意图，不直接访问商品或订单数据。"""

    def is_available(self) -> bool:
        """返回本地配置是否已具备调用条件；实现方可检查凭据但不应发起远程请求。"""

        return True


class UnavailableImageIntentProvider(ImageIntentProvider):
    """默认 provider：明确说明能力未配置，绝不伪造识别结果。"""

    def __init__(self, provider_name="unconfigured"):
        """记录配置项名称，供 API 和部署排障展示。"""

        self.name = provider_name or "unconfigured"

    def extract_intent(self, image):
        """总是抛出可降级异常，不读取、保存或猜测图片内容。"""

        raise FeatureUnavailable(
            "图片找商品尚未配置视觉识别服务，请先使用文字或语音找商品。",
            provider=self.name,
            todo=IMAGE_PROVIDER_TODO,
        )

    def is_available(self):
        """明确报告默认占位实现不可用。"""

        return False


def get_text_intent_provider(provider=None):
    """返回显式或配置的文字 provider；没有配置时返回 ``None``。

    ``None`` 表示服务层继续使用内置的可测试规则解析器，因此未来 AI 能力没有
    配置时不会影响基础文字搜索。若显式配置错误则清楚报错，不静默伪装成功。
    """

    if provider is not None:
        return _validated_provider(
            provider,
            expected_type=TextIntentProvider,
            provider_name=_provider_name(provider),
            label="文字意图",
        )

    configured_path = str(getattr(settings, TEXT_PROVIDER_SETTING, "") or "").strip()
    if not configured_path:
        return None
    candidate = _import_provider(configured_path, label="文字意图")
    return _validated_provider(
        candidate,
        expected_type=TextIntentProvider,
        provider_name=configured_path,
        label="文字意图",
    )


def get_image_intent_provider(provider=None):
    """返回显式 provider 或从 Django 设置加载的 provider 实例。

    配置值使用 Python 导入路径，目标可以是 ``ImageIntentProvider`` 子类、实例，
    或返回实例的无参工厂。无配置时返回明确不可用的安全实现。
    """

    if provider is not None:
        validated = _validated_provider(
            provider,
            expected_type=ImageIntentProvider,
            provider_name=_provider_name(provider),
            label="图片识别",
        )
        return _ensure_image_provider_available(validated)

    configured_path = str(getattr(settings, IMAGE_PROVIDER_SETTING, "") or "").strip()
    if not configured_path:
        return UnavailableImageIntentProvider()

    try:
        candidate = _import_provider(configured_path, label="图片识别")
        validated = _validated_provider(
            candidate,
            expected_type=ImageIntentProvider,
            provider_name=configured_path,
            label="图片识别",
        )
        return _ensure_image_provider_available(validated)
    except ProviderFailure as exc:
        raise FeatureUnavailable(
            "图片识别服务配置无效，请联系管理员检查 provider。",
            provider=configured_path,
            todo=IMAGE_PROVIDER_TODO,
        ) from exc


def image_provider_availability():
    """可靠检查页面是否应开放图片入口，并保留可排障状态。

    此检查会执行与 API 相同的导入和接口验证；配置字符串非空但无法导入、无法
    实例化或类型错误时仍视为不可用，避免页面给出一个必然失败的提交按钮。
    """

    try:
        provider = get_image_intent_provider()
        if isinstance(provider, UnavailableImageIntentProvider):
            return ProviderAvailability(
                available=False,
                provider=provider.name,
                message="图片找商品尚未配置视觉识别服务。",
                todo=IMAGE_PROVIDER_TODO,
            )
        return ProviderAvailability(
            available=True,
            provider=_provider_name(provider),
        )
    except FeatureUnavailable as exc:
        return ProviderAvailability(
            available=False,
            provider=exc.provider,
            message=exc.message,
            todo=exc.todo,
        )
    except ProviderFailure as exc:
        return ProviderAvailability(
            available=False,
            provider=exc.provider,
            message=exc.message,
            todo=IMAGE_PROVIDER_TODO,
        )


def _import_provider(configured_path, *, label):
    """按导入路径实例化 provider 类或无参工厂。"""

    try:
        imported = import_string(configured_path)
        candidate = imported() if isclass(imported) else imported
        if callable(candidate) and not hasattr(candidate, "extract_intent"):
            candidate = candidate()
        return candidate
    except Exception as exc:
        raise ProviderFailure(
            f"{label}服务配置无效。",
            provider=configured_path,
        ) from exc


def _validated_provider(provider, *, expected_type, provider_name, label):
    """校验 provider 的最小契约，避免配置错误延迟到处理图片时才暴露。"""

    if not isinstance(provider, expected_type):
        raise ProviderFailure(
            f"{label}服务没有实现规定的接口。",
            provider=provider_name,
        )
    return provider


def _ensure_image_provider_available(provider):
    """执行 provider 的轻量本地就绪检查，避免仅凭可导入就误开放页面入口。"""

    provider_name = _provider_name(provider)
    try:
        available = provider.is_available()
    except Exception as exc:
        raise FeatureUnavailable(
            "图片识别服务当前不可用，请稍后再试。",
            provider=provider_name,
            todo=IMAGE_PROVIDER_TODO,
        ) from exc
    if available is not True:
        raise FeatureUnavailable(
            "图片识别服务尚未完成运行配置，请先使用文字或语音找商品。",
            provider=provider_name,
            todo=IMAGE_PROVIDER_TODO,
        )
    return provider


def _provider_name(provider):
    """返回适合排障展示且不包含对象地址的 provider 名称。"""

    return str(getattr(provider, "name", provider.__class__.__name__))
