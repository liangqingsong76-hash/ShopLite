import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shoplite.settings')

import django
django.setup()

from shop.models import Category

subcategories = {
    "数码家电": ["手机", "电脑", "耳机", "平板", "智能穿戴", "数码配件"],
    "家居日用": ["厨房用品", "收纳整理", "清洁用品", "家纺布艺", "装饰摆件", "生活日用"],
    "服饰鞋包": ["男装", "女装", "鞋子", "包包", "配饰", "运动服饰"],
    "美妆护肤": ["护肤", "彩妆", "香水", "身体护理", "男士护肤", "美容工具"],
    "母婴用品": ["奶粉", "纸尿裤", "婴儿用品", "童装", "玩具", "孕妇用品"],
    "运动户外": ["运动服饰", "户外装备", "健身器材", "球类运动", "骑行", "露营"],
}

for parent_name, children in subcategories.items():
    try:
        parent = Category.objects.get(name=parent_name)
        for i, child_name in enumerate(children):
            Category.objects.get_or_create(
                name=child_name,
                parent=parent,
                defaults={"sort_order": i + 1, "is_active": True}
            )
        print(f"Added subcategories for {parent_name}")
    except Category.DoesNotExist:
        print(f"Category {parent_name} not found")

print("Subcategories created successfully!")
