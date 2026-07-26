"""账户资料与密码修改表单。

依赖流向：storefront 的设置页面调用本模块；表单写入 Django 用户和
``accounts.UserProfile``，不直接操作订单、支付或通知模型。
"""

# Django 表单与认证依赖：使用内置密码修改表单和用户模型查询。
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.db import transaction

# 本领域服务与模型依赖：表单通过服务发起邮箱验证，资料头像等字段仍由模型保存。
from .models import UserProfile
from .services import (
    email_verification_delivery_configured,
    get_user_by_verified_email,
    request_email_change,
)


class ProfileSettingsForm(forms.ModelForm):
    """校验并保存用户昵称、邮箱、头像和通知偏好的设置表单。"""

    nickname = forms.CharField(label="昵称", max_length=150)
    email = forms.EmailField(label="邮箱", required=False)

    class Meta:
        """声明由表单直接映射到 UserProfile 的字段。"""

        model = UserProfile
        fields = ("avatar", "bio", "marketing_notifications", "order_notifications")
        widgets = {"bio": forms.Textarea(attrs={"rows": 3, "maxlength": 160})}

    def __init__(self, *args, user, request=None, **kwargs):
        """注入当前用户和请求，并以其资料预填昵称和邮箱。

        ``request`` 只用于 allauth 生成验证链接和投递邮件；未提供请求的其他调用方
        不能借表单直接修改邮箱。
        """

        self.user = user
        self.request = request
        self.email_change_requested = False
        super().__init__(*args, **kwargs)
        self.fields["nickname"].initial = user.first_name or user.username
        self.fields["email"].initial = user.email

    def clean_nickname(self):
        """拒绝空白昵称，返回去除首尾空格后的昵称。"""

        nickname = self.cleaned_data["nickname"].strip()
        if not nickname:
            raise ValidationError("昵称不能为空")
        return nickname

    def clean_email(self):
        """校验邮箱变更只能走已配置的 allauth 验证流程。"""

        email = self.cleaned_data.get("email", "").strip().lower()
        current_email = str(self.user.email or "").strip().lower()
        user_model = get_user_model()
        verified_current_user = get_user_by_verified_email(email) if email else None
        requires_verification = bool(email) and (
            email != current_email or not verified_current_user or verified_current_user.pk != self.user.pk
        )
        self.email_change_requested = requires_verification

        if email and user_model.objects.exclude(id=self.user.id).filter(email__iexact=email).exists():
            raise ValidationError("该邮箱已绑定其他账号")
        # 未验证地址同样不可被不同账号抢占，避免确认时才发现身份冲突。
        from allauth.account.models import EmailAddress

        if email and EmailAddress.objects.exclude(user_id=self.user.id).filter(email__iexact=email).exists():
            raise ValidationError("该邮箱已绑定其他账号")
        if email != current_email and not email:
            raise ValidationError("不能在此清空邮箱；如需移除邮箱请联系平台客服")
        if requires_verification:
            if self.request is None:
                raise ValidationError("请通过账户设置页面发起邮箱验证")
            if not email_verification_delivery_configured():
                raise ValidationError("邮箱验证服务暂未配置，暂不能更换邮箱")
        return email

    def clean_avatar(self):
        """限制上传头像的类型和大小，减少非法文件与存储滥用。"""

        avatar = self.cleaned_data.get("avatar")
        if not avatar or not hasattr(avatar, "size"):
            return avatar
        if avatar.size > 5 * 1024 * 1024:
            raise ValidationError("头像不能超过 5MB")
        content_type = getattr(avatar, "content_type", "")
        if content_type and content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValidationError("头像仅支持 JPG、PNG 或 WebP")
        return avatar

    def save(self, commit=True):
        """保存资料并在需要时发起验证；验证前绝不直接写入 ``User.email``。"""

        profile = super().save(commit=False)
        self.user.first_name = self.cleaned_data["nickname"]
        if commit:
            previous_avatar_name = (
                UserProfile.objects.filter(pk=profile.pk).values_list("avatar", flat=True).first() or ""
            )
            written_avatar_name = ""
            try:
                with transaction.atomic():
                    self.user.save(update_fields=["first_name"])
                    profile.save()
                    written_avatar_name = profile.avatar.name if profile.avatar else ""
                    if self.email_change_requested:
                        request_email_change(
                            self.user,
                            self.cleaned_data["email"],
                            request=self.request,
                        )
            except Exception:
                # 数据库回滚不会回滚文件系统；邮件投递等后续步骤失败时显式清理本次
                # 新写且未被任何资料引用的头像，保留事务前的旧头像。
                if written_avatar_name and written_avatar_name != previous_avatar_name:
                    try:
                        if not UserProfile.objects.filter(avatar=written_avatar_name).exists():
                            profile.avatar.storage.delete(written_avatar_name)
                    except Exception:
                        # 存储清理失败不能覆盖原始业务异常；后续孤儿文件巡检负责兜底。
                        pass
                raise
        return profile


class SecurePasswordChangeForm(PasswordChangeForm):
    """为设置页提供明确中文标签的 Django 密码修改表单。"""

    old_password = forms.CharField(label="当前密码", strip=False, widget=forms.PasswordInput)
    new_password1 = forms.CharField(label="新密码", strip=False, widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="确认新密码", strip=False, widget=forms.PasswordInput)
