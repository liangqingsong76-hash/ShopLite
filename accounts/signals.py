"""账户文件存储的生命周期信号。

依赖流向：``accounts.models.UserProfile`` 保存/删除 -> 本模块 -> Django 存储后端。
这里只清理本应用管理的头像文件；不会处理商品图或任何其他媒体文件。
"""

# Django 信号与事务依赖：先记录旧文件，再在数据库事务真正提交后删除它。
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

# 本领域模型依赖：信号只监听用户资料，不反向依赖展示层或交易领域。
from .models import UserProfile


def _is_managed_avatar(name):
    """判断存储名是否是本应用可安全删除的头像路径。

    即使数据库中出现了异常的文件名，也绝不让资料清理逻辑删除 ``avatars/``
    目录之外的媒体文件。
    """

    normalized_name = str(name or "").replace("\\", "/")
    return normalized_name.startswith("avatars/") and ".." not in normalized_name.split("/")


def _delete_avatar_when_unreferenced(storage, name):
    """在提交后删除未被其他资料引用的旧头像，并吞掉存储清理失败。

    数据库提交已经完成时，清理失败不能把已经成功的资料更新变成 500；遗留文件
    可由后续运维清理，而错误路径不会删除仍被任何 ``UserProfile`` 引用的文件。
    """

    if not _is_managed_avatar(name):
        return
    if UserProfile.objects.filter(avatar=name).exists():
        return
    try:
        storage.delete(name)
    except Exception:
        # 存储可能是暂时不可达的远程对象存储。不能因此破坏已完成的数据库事务。
        return


def _schedule_avatar_deletion(storage, name):
    """把旧头像清理注册到当前最外层数据库事务提交之后。"""

    if _is_managed_avatar(name):
        transaction.on_commit(lambda: _delete_avatar_when_unreferenced(storage, name))


@receiver(pre_save, sender=UserProfile, dispatch_uid="accounts.remember_previous_avatar")
def remember_previous_avatar(sender, instance, **kwargs):
    """在资料保存前记录将被替换或清空的头像存储名。

    不能在 ``pre_save`` 直接注册 ``on_commit``：不在显式事务中的 ``on_commit``
    会立即执行，可能早于数据库写入。故本函数只记录状态，交由 ``post_save``
    在写入成功后安排删除。
    """

    instance._avatar_name_to_delete = ""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "avatar" not in update_fields:
        # ``save(update_fields=["bio"])`` 不会写入头像列；不能因内存中可能过期
        # 的 ImageField 值误判为清空并删除数据库当前仍引用的文件。
        return
    if instance._state.adding:
        return
    try:
        previous = sender.objects.only("avatar").get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    previous_name = previous.avatar.name
    current_name = instance.avatar.name
    if previous_name and previous_name != current_name:
        instance._avatar_name_to_delete = previous_name


@receiver(post_save, sender=UserProfile, dispatch_uid="accounts.cleanup_replaced_avatar")
def cleanup_replaced_avatar(sender, instance, **kwargs):
    """在资料保存成功后安排清理被替换或清空的旧头像。"""

    previous_name = getattr(instance, "_avatar_name_to_delete", "")
    if previous_name:
        _schedule_avatar_deletion(instance.avatar.storage, previous_name)


@receiver(post_delete, sender=UserProfile, dispatch_uid="accounts.cleanup_deleted_profile_avatar")
def cleanup_deleted_profile_avatar(sender, instance, **kwargs):
    """在资料或其级联用户删除提交后清理头像文件。"""

    if instance.avatar:
        _schedule_avatar_deletion(instance.avatar.storage, instance.avatar.name)
