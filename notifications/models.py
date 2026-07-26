"""用户站内通知模型。

上游：commerce、payments、未来 support 服务写入通知。
下游：storefront 的通知页面读取本模型；模型自身不发邮件或短信。
"""

from django.conf import settings
from django.db import models


class Notification(models.Model):
    """用户可在消息中心查看的一条站内通知。"""

    TYPE_SYSTEM = "system"
    TYPE_ORDER = "order"
    TYPE_COUPON = "coupon"
    TYPE_CHOICES = (
        (TYPE_SYSTEM, "系统"),
        (TYPE_ORDER, "订单"),
        (TYPE_COUPON, "优惠券"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    category = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_SYSTEM)
    title = models.CharField(max_length=120)
    content = models.TextField()
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """声明站内通知的后台名称和默认展示顺序。"""

        verbose_name = "站内通知"
        verbose_name_plural = "站内通知"
        ordering = ("-created_at",)

    @property
    def safe_link(self):
        """只允许安全的本站相对路径，防止通知链接成为开放跳转。"""

        return self.link if self.link.startswith("/") and not self.link.startswith("//") else "/notifications/"
