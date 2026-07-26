"""快速找商品领域的可预期异常。

依赖流向：provider/服务层抛出本模块异常 -> API 层转换为稳定、可读的 HTTP
错误；异常本身不依赖 Django HTTP，因此服务也可被 SSR 页面或后台任务复用。
"""


class DiscoveryError(Exception):
    """所有可安全展示给调用方的商品发现异常基类。"""

    code = "discovery_error"

    def __init__(self, message):
        """保存面向用户的简短错误信息。"""

        super().__init__(message)
        self.message = str(message)


class InvalidDiscoveryInput(DiscoveryError):
    """表示查询文本、图片或结果数量不符合服务契约。"""

    code = "invalid_input"


class FeatureUnavailable(DiscoveryError):
    """表示可插拔能力尚未配置，调用方可以安全地显示降级提示。"""

    code = "feature_unavailable"

    def __init__(self, message, *, provider="unconfigured", todo=""):
        """附带当前 provider 名称和明确的部署 TODO。"""

        super().__init__(message)
        self.provider = provider
        self.todo = todo


class ProviderFailure(DiscoveryError):
    """表示已经配置的外部智能能力执行失败或返回了无效契约。"""

    code = "provider_failure"

    def __init__(self, message, *, provider="unknown"):
        """记录失败的 provider 名称，但不泄露密钥或原始响应。"""

        super().__init__(message)
        self.provider = provider
