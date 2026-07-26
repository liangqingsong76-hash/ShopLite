"""未来独立商家平台的内部适配边界。

上游：未来商家平台调用受鉴权 REST API。
下游：该边界将调用 catalog/commerce 的公开服务；当前不暴露 URL、不开启鉴权、也不写数据库。
TODO(merchant-platform)：确定商家项目、服务令牌、幂等键、审计日志和限流策略后再实现。
"""


class MerchantPlatformGateway:
    """商家平台适配器的占位协议，防止未来跨项目直接共享数据库。"""

    def publish_catalog_change(self, payload):
        """TODO：向已鉴权的商家平台发送商品变更；当前禁止调用。"""

        raise NotImplementedError("TODO：商家平台接口尚未设计，禁止直接共享数据库。")
