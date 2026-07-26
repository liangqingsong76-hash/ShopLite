"""快速找商品的登录态 JSON API。

依赖流向：浏览器 -> 本模块 -> ``discovery.services`` -> ``catalog`` 只读查询。
本模块只处理 HTTP 校验、限流和序列化；不会保存查询、语音文本或原始图片。
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import PurePath

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from PIL import Image, UnidentifiedImageError

from .contracts import DiscoveryRequest, DiscoverySource, ImageInput
from .exceptions import (
    FeatureUnavailable,
    InvalidDiscoveryInput,
    ProviderFailure,
)
from .services import (
    DEFAULT_RESULT_LIMIT,
    MAX_QUERY_CHARACTERS,
    MAX_RESULT_LIMIT,
    discover_products,
)


MAX_JSON_BODY_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_MULTIPART_BODY_BYTES = MAX_IMAGE_BYTES + 64 * 1024
MAX_IMAGE_PIXELS = 16_000_000
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
ALLOWED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
IMAGE_FORMAT_METADATA = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


@require_POST
def text_search(request):
    """从普通文字描述返回结构化意图和匹配商品。"""

    guard = _authentication_guard(request)
    if guard is not None:
        return guard
    payload, error = _json_object(request)
    if error is not None:
        return error
    query, limit, error = _text_payload(payload, field="query")
    if error is not None:
        return error
    guard = _rate_limit_guard(request, "text")
    if guard is not None:
        return guard
    return _execute(
        DiscoveryRequest(
            source=DiscoverySource.TEXT,
            query=query,
            limit=limit,
        )
    )


@require_POST
def voice_search(request):
    """从语音系统已经转写的文本找商品；本接口不接收音频文件。"""

    guard = _authentication_guard(request)
    if guard is not None:
        return guard
    payload, error = _json_object(request)
    if error is not None:
        return error
    if "audio" in payload:
        return _error(
            "invalid_input",
            "语音找商品接口只接收转写文本，请先在终端完成语音转写。",
            status=400,
        )
    transcript, limit, error = _text_payload(payload, field="transcript")
    if error is not None:
        return error
    guard = _rate_limit_guard(request, "voice")
    if guard is not None:
        return guard
    return _execute(
        DiscoveryRequest(
            source=DiscoverySource.VOICE_TRANSCRIPT,
            query=transcript,
            limit=limit,
        )
    )


@require_POST
def image_search(request):
    """校验内存图片并交给可插拔视觉 provider，不将原图写入项目存储。"""

    guard = _authentication_guard(request)
    if guard is not None:
        return guard
    content_length = _content_length(request)
    if content_length is not None and content_length > MAX_MULTIPART_BODY_BYTES:
        return _error("payload_too_large", "图片不能超过 2 MB。", status=413)
    if not str(request.content_type or "").lower().startswith("multipart/form-data"):
        return _error(
            "unsupported_media_type",
            "图片找商品接口需要 multipart/form-data。",
            status=415,
        )
    try:
        uploaded = request.FILES.get("image")
    except RequestDataTooBig:
        return _error("payload_too_large", "图片不能超过 2 MB。", status=413)
    if uploaded is None:
        return _error("invalid_input", "请选择需要识别的商品图片。", status=400)
    if uploaded.size > MAX_IMAGE_BYTES:
        return _error("payload_too_large", "图片不能超过 2 MB。", status=413)
    content_type = str(getattr(uploaded, "content_type", "") or "").lower()
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        return _error(
            "unsupported_media_type",
            "仅支持 JPG、PNG 或 WebP 图片。",
            status=415,
        )
    raw_image = uploaded.read(MAX_IMAGE_BYTES + 1)
    if len(raw_image) > MAX_IMAGE_BYTES:
        return _error("payload_too_large", "图片不能超过 2 MB。", status=413)
    limit, validation_error = _validated_limit_value(
        request.POST.get("limit", DEFAULT_RESULT_LIMIT)
    )
    if validation_error is not None:
        return validation_error
    guard = _rate_limit_guard(request, "image")
    if guard is not None:
        return guard
    image_metadata, validation_error = _validated_image_metadata(
        raw_image,
        declared_content_type=content_type,
    )
    if validation_error is not None:
        return validation_error

    canonical_content_type, canonical_extension = image_metadata
    return _execute(
        DiscoveryRequest(
            source=DiscoverySource.IMAGE,
            image=ImageInput(
                data=raw_image,
                content_type=canonical_content_type,
                filename=_normalized_image_filename(
                    uploaded.name,
                    canonical_extension,
                ),
            ),
            limit=limit,
        )
    )


def _authentication_guard(request):
    """先拒绝匿名请求，避免未认证调用方触发正文解析或 provider 检查。"""

    if not request.user.is_authenticated:
        return _error("authentication_required", "请先登录后再使用智能找商品。", status=401)
    return None


def _rate_limit_guard(request, scope):
    """在基本载荷通过后按登录用户限流，使格式错误不消耗有效请求额度。"""

    limit, window = _rate_limit(scope)
    if not _allow_user_request(request.user.pk, scope, limit=limit, window=window):
        response = _error("rate_limited", "操作太频繁，请稍后再试。", status=429)
        response["Retry-After"] = str(window)
        return response
    return None


def _json_object(request):
    """读取受限 JSON 对象，并区分格式、类型和正文过大错误。"""

    content_length = _content_length(request)
    if content_length is not None and content_length > MAX_JSON_BODY_BYTES:
        return None, _error("payload_too_large", "请求内容过大。", status=413)
    if not str(request.content_type or "").lower().startswith("application/json"):
        return None, _error(
            "unsupported_media_type",
            "该接口只接收 application/json。",
            status=415,
        )
    try:
        body = request.body
    except RequestDataTooBig:
        return None, _error("payload_too_large", "请求内容过大。", status=413)
    if not body or len(body) > MAX_JSON_BODY_BYTES:
        return None, _error(
            "invalid_input" if not body else "payload_too_large",
            "请求正文不能为空。" if not body else "请求内容过大。",
            status=400 if not body else 413,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _error("invalid_json", "请求 JSON 格式无效。", status=400)
    if not isinstance(payload, dict):
        return None, _error("invalid_json", "请求 JSON 必须是对象。", status=400)
    return payload, None


def _text_payload(payload, *, field):
    """校验文字入口的必填字段和结果上限，再允许请求占用限流额度。"""

    value = payload.get(field)
    if not isinstance(value, str):
        label = "转写文本" if field == "transcript" else "查询内容"
        return None, None, _error(
            "invalid_input",
            f"{label}必须是文字。",
            status=400,
        )
    normalized = " ".join(value.split())
    if not normalized:
        message = "请输入语音转写文本。" if field == "transcript" else "请输入想找的商品。"
        return None, None, _error("invalid_input", message, status=400)
    if len(normalized) > MAX_QUERY_CHARACTERS:
        return None, None, _error(
            "invalid_input",
            f"查询内容不能超过 {MAX_QUERY_CHARACTERS} 个字符。",
            status=400,
        )
    limit, error = _validated_limit_value(payload.get("limit", DEFAULT_RESULT_LIMIT))
    if error is not None:
        return None, None, error
    return normalized, limit, None


def _validated_limit_value(raw_limit):
    """校验 API 结果数量，避免无效请求占用限流次数或进入领域服务。"""

    if isinstance(raw_limit, bool):
        return None, _error("invalid_input", "结果数量格式无效。", status=400)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return None, _error("invalid_input", "结果数量格式无效。", status=400)
    if limit < 1 or limit > MAX_RESULT_LIMIT:
        return None, _error(
            "invalid_input",
            f"结果数量必须在 1 到 {MAX_RESULT_LIMIT} 之间。",
            status=400,
        )
    return limit, None


def _validated_image_metadata(raw_image, *, declared_content_type):
    """验证实际图片格式、像素和声明 MIME，并返回 provider 使用的规范元数据。"""

    if not raw_image:
        return None, _error("invalid_input", "图片内容为空。", status=400)
    try:
        with Image.open(BytesIO(raw_image)) as image:
            if image.format not in ALLOWED_IMAGE_FORMATS:
                return None, _error(
                    "unsupported_media_type",
                    "仅支持 JPG、PNG 或 WebP 图片。",
                    status=415,
                )
            canonical_content_type, canonical_extension = IMAGE_FORMAT_METADATA[
                image.format
            ]
            if declared_content_type != canonical_content_type:
                return None, _error(
                    "unsupported_media_type",
                    "图片声明类型与实际格式不一致，请重新选择原始图片。",
                    status=415,
                )
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                return None, _error(
                    "invalid_input",
                    "图片尺寸无效或像素过大。",
                    status=400,
                )
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None, _error(
            "invalid_input",
            "图片文件已损坏或格式无效。",
            status=400,
        )
    return (canonical_content_type, canonical_extension), None


def _normalized_image_filename(original_name, canonical_extension):
    """去除上传路径和伪扩展名，向 provider 传递与真实格式一致的安全文件名。"""

    basename = PurePath(str(original_name or "")).name
    stem = PurePath(basename).stem.strip()[:200] or "discovery-image"
    return f"{stem}{canonical_extension}"


def _execute(discovery_request):
    """执行领域服务，并把已知异常映射为稳定 HTTP 状态。"""

    try:
        response = discover_products(discovery_request)
    except InvalidDiscoveryInput as exc:
        return _error(exc.code, exc.message, status=400)
    except FeatureUnavailable as exc:
        payload = {
            "error": {"code": exc.code, "message": exc.message},
            "provider": exc.provider,
            "todo": exc.todo,
        }
        return JsonResponse(payload, status=503)
    except ProviderFailure as exc:
        return JsonResponse(
            {
                "error": {"code": exc.code, "message": exc.message},
                "provider": exc.provider,
            },
            status=502,
        )
    return JsonResponse(response.as_dict())


def _allow_user_request(user_id, scope, *, limit, window):
    """用缓存原子计数限制单个登录用户的请求频率。"""

    cache_key = f"discovery-rate:v1:{scope}:user:{user_id}"
    if cache.add(cache_key, 1, timeout=window):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=window)
        count = 1
    return count <= limit


def _rate_limit(scope):
    """从设置读取可运维限流值，并对异常配置使用保守默认值。"""

    defaults = {
        "text": (30, 300),
        "voice": (30, 300),
        "image": (10, 300),
    }
    default_limit, default_window = defaults[scope]
    configured_limit = getattr(
        settings,
        f"DISCOVERY_{scope.upper()}_RATE_LIMIT",
        default_limit,
    )
    configured_window = getattr(
        settings,
        "DISCOVERY_RATE_LIMIT_WINDOW_SECONDS",
        default_window,
    )
    try:
        limit = max(1, min(int(configured_limit), 1000))
        window = max(1, min(int(configured_window), 3600))
    except (TypeError, ValueError):
        return default_limit, default_window
    return limit, window


def _content_length(request):
    """安全解析 Content-Length；缺失时由实际读取长度继续兜底。"""

    raw_value = request.META.get("CONTENT_LENGTH")
    if not raw_value:
        return None
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return None


def _error(code, message, *, status):
    """构造统一、可读的错误响应。"""

    return JsonResponse(
        {"error": {"code": code, "message": message}},
        status=status,
    )
