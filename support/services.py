"""AI 客服业务边界占位。

上游：未来 storefront 聊天 UI。
下游：未来仅通过 accounts/commerce 的公开服务读取获授权用户与订单上下文。
TODO(ai-support)：确定模型供应商、会话留存期限、敏感信息脱敏、人工转接和内容安全策略后实现。
"""


def reply_to_customer(*, user, message):
    """TODO：生成客服回复；当前显式拒绝调用，避免误发送外部模型请求。"""

    raise NotImplementedError("TODO：AI 客服尚未接入，当前不处理用户消息。")
