"""跨应用共享的领域常量。

上游：commerce、payments 等领域模型和服务读取这里的支付枚举。
下游：本模块不访问数据库，也不依赖任何业务应用。
"""

# 订单和支付流水都使用同一组渠道编码，避免 commerce 与 payments 相互导入。
PAYMENT_METHOD_MOCK = "mock"
PAYMENT_METHOD_ALIPAY = "alipay"
PAYMENT_METHOD_WECHAT = "wechat"
PAYMENT_METHOD_CHOICES = (
    (PAYMENT_METHOD_MOCK, "模拟支付（仅开发环境）"),
    (PAYMENT_METHOD_ALIPAY, "支付宝（TODO：待接入官方能力）"),
    (PAYMENT_METHOD_WECHAT, "微信支付（TODO：待接入官方能力）"),
)
