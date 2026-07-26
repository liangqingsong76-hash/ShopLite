#  让 PyMySQL 伪装成 MySQLdb
import pymysql
pymysql.install_as_MySQLdb()

#  尝试导入 Celery 应用
try:
    from .celery import app as celery_app
except ImportError:
    celery_app = None

#  声明这个包对外导出的变量是celery_app
__all__ = ("celery_app",)
