"""账户后台管理保护。

依赖流向：Django admin -> 本模块 -> 内置 ``auth.User``。用户关联订单、支付和
资料等不可逆审计数据，因此后台只能停用账户，不能物理删除。
"""

# Django 后台与权限依赖：在保留内置 UserAdmin 功能的基础上收紧删除入口。
from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied


class ProtectedUserAdmin(UserAdmin):
    """保留内置用户后台的编辑能力，但禁止单个和批量物理删除。"""

    def has_delete_permission(self, request, obj=None):
        """始终禁止删除，避免级联删除订单、支付流水和预占库存状态。"""

        return False

    def get_actions(self, request):
        """移除默认批量删除动作，避免管理员界面出现无效或危险入口。"""

        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_model(self, request, obj):
        """即使第三方后台错误调用删除钩子，也拒绝物理删除。"""

        raise PermissionDenied("用户账户不能物理删除，请改为停用账户")

    def delete_queryset(self, request, queryset):
        """阻断可能绕过单条删除权限的批量删除调用。"""

        raise PermissionDenied("用户账户不能批量物理删除，请改为停用账户")


user_model = get_user_model()
try:
    admin.site.unregister(user_model)
except NotRegistered:
    # 自定义用户模型或测试中的精简后台可能尚未注册，直接注册即可。
    pass
admin.site.register(user_model, ProtectedUserAdmin)
