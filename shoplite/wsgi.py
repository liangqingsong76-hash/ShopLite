"""
WSGI config for shoplite project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
"""

# 导入环境变量工具和 WSGI 应用生成函数
import os
from django.core.wsgi import get_wsgi_application

# 设置默认配置模块
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shoplite.settings")

# 生成 WSGI 应用对象。Gunicorn 会加载它
application = get_wsgi_application()
