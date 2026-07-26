"""支付流水模型。

上游：payments.services 在成功或失败回调后写入。
下游：后台、订单详情和审计文档读取；支付网关原始数据只保存最小必要摘要。
"""

from django.db import models

from core.constants import PAYMENT_METHOD_CHOICES


class PaymentTransaction(models.Model):
    """一笔订单渠道交易的不可变审计记录。

    成功流水与取消订单的迟到成功流水都以渠道交易号全局唯一。后者保留在
    ``reconciliation`` 状态，供人工核对退款或资金去向，不能驱动订单发货。
    """

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_RECONCILIATION = "reconciliation"
    STATUS_CHOICES = (
        (STATUS_PENDING, "处理中"),
        (STATUS_SUCCESS, "成功"),
        (STATUS_FAILED, "失败"),
        (STATUS_RECONCILIATION, "待人工对账"),
    )

    # 依赖流向：支付审计流水 -> 订单；存在资金流水时禁止物理删除订单。
    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    transaction_no = models.CharField(max_length=64, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """声明支付流水的后台名称和默认展示顺序。"""

        verbose_name = "支付流水"
        verbose_name_plural = "支付流水"
        ordering = ("-created_at",)
