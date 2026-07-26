"""交易页面的 Django 表单。

上游：storefront 的地址和售后页面。
下游：调用 accounts 的手机号规范化服务并生成 commerce 模型数据。
"""

from django import forms
from django.core.exceptions import ValidationError

from accounts.services import normalize_phone

from .models import Address


class AddressForm(forms.ModelForm):
    """校验用户收货地址的表单。"""

    class Meta:
        """将地址表单字段限定为用户可维护的收货信息。"""

        model = Address
        fields = ("receiver", "phone", "province", "city", "district", "detail", "is_default")

    def clean_phone(self):
        """使用账户领域统一的中国大陆手机号规范化规则。"""

        return normalize_phone(self.cleaned_data["phone"])

    def clean(self):
        """确保省、市和详细地址均可用于订单快照。"""

        cleaned = super().clean()
        if not all(cleaned.get(key) for key in ("province", "city", "detail")):
            raise ValidationError("请完整填写省份、城市和详细地址")
        return cleaned


class RefundRequestForm(forms.Form):
    """用户提交售后原因与说明的表单。"""

    REASONS = (
        ("不想要了", "不想要了"),
        ("商品质量问题", "商品质量问题"),
        ("商品与描述不符", "商品与描述不符"),
        ("少件/漏发", "少件/漏发"),
        ("其他", "其他"),
    )
    reason = forms.ChoiceField(label="退款原因", choices=REASONS)
    description = forms.CharField(
        label="问题描述",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "请补充说明，最多 1000 字"}),
    )
