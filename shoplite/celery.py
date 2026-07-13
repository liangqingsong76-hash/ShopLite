import os
from celery import Celery

# 使用 Django 配置
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shoplite.settings")

# 应用名叫shoplite
app = Celery("shoplite")
# 从 Django settings 中读取所有 `CELERY_` 开头的配置
app.config_from_object("django.conf:settings", namespace="CELERY")
# 自动发现各 app 中的 `tasks.py
app.autodiscover_tasks()
