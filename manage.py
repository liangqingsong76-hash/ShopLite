#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    #  默认文件配置路径是shoplite.settings
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shoplite.settings")
    try:
        #  导入Django的命令执行器
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        #  Django没安装或环境不对报错
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    #  执行命令
    execute_from_command_line(sys.argv)


#  只有直接运行这个文件时才调用main()
if __name__ == "__main__":
    main()
