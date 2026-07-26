"""旧 ``shop.forms`` 的兼容导出层。

依赖流向：旧店铺视图 -> 本模块 -> accounts/commerce 表单。表单规则只在所属
领域维护，兼容层不再复制验证逻辑。
"""

from accounts.forms import ProfileSettingsForm, SecurePasswordChangeForm
from commerce.forms import AddressForm, RefundRequestForm


__all__ = (
    "AddressForm",
    "ProfileSettingsForm",
    "RefundRequestForm",
    "SecurePasswordChangeForm",
)
