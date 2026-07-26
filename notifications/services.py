"""创建站内通知的唯一服务入口。

上游：交易、支付和未来客服领域调用。
下游：只写入 Notification 模型，外部推送能力未来单独以 TODO 适配。
"""

from .models import Notification


def create_notification(*, user, category, title, content, link=""):
    """创建并返回一条站内通知。

    输入：接收用户、分类、标题、内容及可选本站链接。
    输出：持久化后的 ``Notification``。
    """

    return Notification.objects.create(
        user=user,
        category=category,
        title=title,
        content=content,
        link=link,
    )
