"""账户领域的数据模型。

依赖流向：accounts.models -> Django 认证用户模型。商品、订单等领域只能
通过 ``settings.AUTH_USER_MODEL`` 关联用户，不能反向导入账户业务服务。
"""

# 依赖流向：accounts.models -> Django 配置、ORM 与时区工具。
from django.conf import settings
from django.db import models
from django.utils import timezone


class UserProfile(models.Model):
    """保存 Django 内置用户模型之外的个人资料和通知偏好。"""

    # 依赖流向：账户资料 -> Django AUTH_USER_MODEL；用户删除时资料随之删除。
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.CASCADE,
        related_name="profile",
    )
    phone = models.CharField("手机号", max_length=20, unique=True, blank=True, null=True)
    phone_verified_at = models.DateTimeField("手机号验证时间", blank=True, null=True)
    avatar = models.ImageField("头像", upload_to="avatars/%Y/%m/", blank=True)
    bio = models.CharField("个人简介", max_length=160, blank=True)
    marketing_notifications = models.BooleanField("接收优惠通知", default=True)
    order_notifications = models.BooleanField("接收订单通知", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        """定义后台展示名称。"""

        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self):
        """返回便于后台和日志辨识的用户资料文本。"""

        return f"{self.user.username} {self.phone or ''}".strip()


class PhoneVerificationCode(models.Model):
    """保存短信验证码摘要、使用状态和防暴力尝试计数。"""

    PURPOSE_REGISTER = "register"
    PURPOSE_LOGIN = "login"
    PURPOSE_BIND = "bind"
    PURPOSE_CHANGE_OLD = "change_old"
    PURPOSE_CHANGE_NEW = "change_new"
    PURPOSE_RESET = "reset"
    PURPOSE_CHOICES = (
        (PURPOSE_REGISTER, "手机号注册"),
        (PURPOSE_LOGIN, "手机号登录"),
        (PURPOSE_BIND, "绑定手机号"),
        (PURPOSE_CHANGE_OLD, "换绑验证当前手机号"),
        (PURPOSE_CHANGE_NEW, "换绑验证新手机号"),
        (PURPOSE_RESET, "重置密码"),
    )

    phone = models.CharField("手机号", max_length=20, db_index=True)
    # 仅存储 HMAC 摘要；明文验证码只存在于短信发送过程或本地测试进程中。
    code = models.CharField("验证码摘要", max_length=64)
    purpose = models.CharField("用途", max_length=20, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    expires_at = models.DateTimeField("过期时间")
    used_at = models.DateTimeField("使用时间", blank=True, null=True)
    attempt_count = models.PositiveSmallIntegerField("校验失败次数", default=0)
    last_attempt_at = models.DateTimeField("最后校验时间", blank=True, null=True)
    sent_to_backend = models.BooleanField("已提交短信通道", default=False)

    class Meta:
        """定义验证码查询顺序及手机号用途时间联合索引。"""

        verbose_name = "手机验证码"
        verbose_name_plural = "手机验证码"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["phone", "purpose", "created_at"])]

    def __str__(self):
        """返回验证码归属手机号和用途，不暴露验证码摘要。"""

        return f"{self.phone} {self.purpose}"

    @property
    def is_expired(self):
        """判断验证码是否已到达失效时间。"""

        return timezone.now() >= self.expires_at

    @property
    def is_used(self):
        """判断验证码是否已经成功消费。"""

        return self.used_at is not None
