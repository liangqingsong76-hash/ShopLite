"""旧 Celery 任务路径的兼容导出层。

依赖流向：历史任务名称 -> 本模块 -> commerce.tasks。Celery Beat 新配置应直接
引用领域任务路径。
"""

from commerce.tasks import cancel_expired_pending_orders


__all__ = ("cancel_expired_pending_orders",)
