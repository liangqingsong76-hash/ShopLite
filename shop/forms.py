from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from .models import Address, RefundRequest, UserProfile
from .services import normalize_phone


class ProfileSettingsForm(forms.ModelForm):
    nickname = forms.CharField(label="昵称", max_length=150)
    email = forms.EmailField(label="邮箱", required=False)

    class Meta:
        model = UserProfile
        fields = ("avatar", "bio", "marketing_notifications", "order_notifications")
        widgets = {"bio": forms.Textarea(attrs={"rows": 3, "maxlength": 160})}

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["nickname"].initial = user.first_name or user.username
        self.fields["email"].initial = user.email

    def clean_nickname(self):
        nickname = self.cleaned_data["nickname"].strip()
        if not nickname:
            raise ValidationError("昵称不能为空")
        return nickname

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if email and User.objects.exclude(id=self.user.id).filter(email__iexact=email).exists():
            raise ValidationError("该邮箱已绑定其他账号")
        return email

    def clean_avatar(self):
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
        profile = super().save(commit=False)
        self.user.first_name = self.cleaned_data["nickname"]
        self.user.email = self.cleaned_data["email"]
        if commit:
            self.user.save(update_fields=["first_name", "email"])
            profile.save()
        return profile


class SecurePasswordChangeForm(PasswordChangeForm):
    old_password = forms.CharField(label="当前密码", strip=False, widget=forms.PasswordInput)
    new_password1 = forms.CharField(label="新密码", strip=False, widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="确认新密码", strip=False, widget=forms.PasswordInput)


class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = ("receiver", "phone", "province", "city", "district", "detail", "is_default")

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])

    def clean(self):
        cleaned = super().clean()
        if not all(cleaned.get(key) for key in ("province", "city", "detail")):
            raise ValidationError("请完整填写省份、城市和详细地址")
        return cleaned


class RefundRequestForm(forms.Form):
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
