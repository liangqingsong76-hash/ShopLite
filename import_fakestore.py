import os
import json
import random
import shutil
import django
import requests
from decimal import Decimal
from datetime import timedelta
from django.core.files.base import ContentFile
from django.utils import timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shoplite.settings")
django.setup()

from shop.models import Category, Product, Review

API_URL = "https://fakestoreapi.com/products"
LOCAL_IMG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fake-store-api", "public", "img")

CATEGORIES = [
    {
        "name": "男装",
        "icon": "M",
        "subcategories": ["T恤", "衬衫", "外套", "裤装", "配饰"],
    },
    {
        "name": "女装",
        "icon": "W",
        "subcategories": ["连衣裙", "上衣", "外套", "裤装", "裙装"],
    },
    {
        "name": "数码电子",
        "icon": "E",
        "subcategories": ["手机", "电脑", "存储设备", "显示器", "配件"],
    },
    {
        "name": "珠宝首饰",
        "icon": "J",
        "subcategories": ["戒指", "手链", "项链", "耳饰"],
    },
    {
        "name": "家居生活",
        "icon": "H",
        "subcategories": ["床上用品", "厨房用品", "收纳清洁", "家具灯具", "家电"],
    },
    {
        "name": "美妆护肤",
        "icon": "B",
        "subcategories": ["护肤", "彩妆", "香水", "美发"],
    },
    {
        "name": "运动户外",
        "icon": "S",
        "subcategories": ["运动服饰", "运动鞋", "户外装备", "健身器材"],
    },
    {
        "name": "食品生鲜",
        "icon": "F",
        "subcategories": ["零食坚果", "饮料冲调", "生鲜水果", "粮油调味"],
    },
]

API_CATEGORY_MAP = {
    "men's clothing": "男装",
    "men clothing": "男装",
    "women's clothing": "女装",
    "women clothing": "女装",
    "electronics": "数码电子",
    "jewelery": "珠宝首饰",
    "jewelry": "珠宝首饰",
}

API_SUBCATEGORY_MAP = {
    "男装": lambda t: (
        "配饰" if "backpack" in t.lower() or "bag" in t.lower()
        else "外套" if "jacket" in t.lower() or "coat" in t.lower()
        else "衬衫" if "shirt" in t.lower()
        else "T恤"
    ),
    "女装": lambda t: (
        "连衣裙" if "dress" in t.lower()
        else "外套" if "jacket" in t.lower() or "coat" in t.lower() or "rain" in t.lower()
        else "上衣"
    ),
    "数码电子": lambda t: (
        "显示器" if "monitor" in t.lower() or "inch" in t.lower() or "screen" in t.lower()
        else "存储设备" if "ssd" in t.lower() or "hard drive" in t.lower() or "hdd" in t.lower() or "drive" in t.lower()
        else "配件"
    ),
    "珠宝首饰": lambda t: (
        "戒指" if "ring" in t.lower() or "princess" in t.lower() or "pave" in t.lower()
        else "手链" if "bracelet" in t.lower() or "chain" in t.lower()
        else "耳饰"
    ),
}

SPEC_TEMPLATES = {
    "男装": {
        "材质": ["纯棉", "涤纶", "棉混纺", "羊毛", "羽绒"],
        "风格": ["休闲", "商务", "运动", "复古", "简约"],
        "适用季节": ["春季", "夏季", "秋季", "冬季", "四季"],
        "尺码": ["S", "M", "L", "XL", "XXL"],
    },
    "女装": {
        "材质": ["纯棉", "雪纺", "丝绸", "羊毛", "针织"],
        "风格": ["甜美", "通勤", "休闲", "复古", "韩版"],
        "适用季节": ["春季", "夏季", "秋季", "冬季", "四季"],
        "尺码": ["XS", "S", "M", "L", "XL"],
    },
    "数码电子": {
        "品牌": ["Samsung", "WD", "Apple", "Sony", "小米"],
        "型号": ["标准版", "Pro版", "旗舰版", "青春版"],
        "颜色": ["黑色", "白色", "银色", "金色", "蓝色"],
        "质保": ["一年质保", "两年质保", "三年质保"],
    },
    "珠宝首饰": {
        "材质": ["925银", "18K金", "铂金", "玫瑰金", "不锈钢"],
        "镶嵌": ["钻石", "锆石", "珍珠", "翡翠", "无"],
        "风格": ["简约", "复古", "时尚", "经典", "民族风"],
        "适用人群": ["女士", "男士", "通用"],
    },
    "家居生活": {
        "材质": ["棉质", "竹纤维", "不锈钢", "陶瓷", "实木"],
        "尺寸": ["小号", "中号", "大号", "特大号"],
        "风格": ["简约", "北欧", "中式", "日式", "现代"],
        "产地": ["中国", "日本", "德国", "美国"],
    },
    "美妆护肤": {
        "功效": ["保湿", "美白", "抗皱", "控油", "舒缓"],
        "肤质": ["干性", "油性", "混合性", "敏感性", "所有肤质"],
        "规格": ["30ml", "50ml", "100ml", "200ml", "套装"],
        "产地": ["日本", "韩国", "法国", "中国", "美国"],
    },
    "运动户外": {
        "材质": ["聚酯纤维", "纯棉", "皮革", "橡胶", "尼龙"],
        "适用运动": ["跑步", "篮球", "足球", "登山", "健身"],
        "尺码": ["36", "37", "38", "39", "40", "41", "42", "43"],
        "适用季节": ["春季", "夏季", "秋季", "冬季", "四季"],
    },
    "食品生鲜": {
        "净含量": ["100g", "250g", "500g", "1kg", "2kg"],
        "保质期": ["6个月", "12个月", "18个月", "24个月"],
        "产地": ["中国", "泰国", "智利", "新西兰", "澳洲"],
        "包装": ["袋装", "盒装", "罐装", "散装"],
    },
}

REVIEW_USERS = [
    "小明同学", "购物达人", "品质生活", "阳光少年", "文艺青年",
    "数码玩家", "美食家", "运动健将", "小仙女", "老王",
    "快乐小猪", "爱购物的猫", "风一样的女子", "技术宅", "吃货一枚",
    "精致女孩", "剁手党", "理性消费者", "颜值控", "性价比之王",
]

REVIEW_TEMPLATES = [
    "质量非常好，与卖家描述的完全一致，非常满意！",
    "真的很喜欢，完全超出期望值，发货速度非常快。",
    "包装非常仔细、严实，物流公司服务态度很好。",
    "很满意的一次购物，质量不错，性价比很高。",
    "宝贝收到了，和图片上一样，质量很好，值得购买！",
    "非常棒，用了几天才来评价，真的很好用。",
    "朋友推荐过来的，果然没让我失望，好评！",
    "第二次购买了，一如既往的好，会继续支持。",
    "颜色很正，款式也好看，穿起来很舒服。",
    "做工精细，材质不错，手感很好，推荐购买。",
    "性价比超高，这个价格能买到这么好的东西很划算。",
    "物流很快，客服态度也很好，满意的一次购物体验。",
    "外观漂亮，功能齐全，使用方便，非常满意。",
    "味道很好，口感不错，会回购的。",
    "大小合适，穿着舒适，非常喜欢。",
]


def generate_specs(cat_name, seed=0):
    random.seed(seed)
    specs = {}
    template = SPEC_TEMPLATES.get(cat_name, {})
    for key, values in template.items():
        specs[key] = random.choice(values)
    return specs


def generate_reviews(product, count=8, seed=0):
    random.seed(seed)
    reviews = []
    for i in range(count):
        rating = random.choice([4, 4, 5, 5, 5, 3])
        content = random.choice(REVIEW_TEMPLATES)
        username = random.choice(REVIEW_USERS)
        is_anon = random.random() < 0.3
        days_ago = random.randint(1, 180)
        created = timezone.now() - timedelta(days=days_ago, hours=random.randint(0, 23))
        reviews.append(Review(
            product=product,
            username=username,
            rating=rating,
            content=content,
            is_anonymous=is_anon,
            created_at=created,
        ))
    return reviews


def get_image_content(image_url):
    filename = os.path.basename(image_url)
    local_path = os.path.join(LOCAL_IMG_DIR, filename)
    if os.path.isfile(local_path):
        with open(local_path, "rb") as f:
            return ContentFile(f.read())
    try:
        resp = requests.get(image_url, timeout=30)
        if resp.status_code == 200:
            return ContentFile(resp.content)
    except Exception:
        pass
    return None


def main():
    print(f"Local image dir: {LOCAL_IMG_DIR}")
    print(f"Local images found: {len(os.listdir(LOCAL_IMG_DIR)) if os.path.isdir(LOCAL_IMG_DIR) else 0}")
    print("\nFetching products from Fake Store API...")
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    api_products = resp.json()
    print(f"Got {len(api_products)} products from API")

    Review.objects.all().delete()
    Product.objects.all().delete()
    Category.objects.all().delete()
    print("Cleared existing data")

    category_objs = {}
    subcategory_objs = {}

    for idx, cat_info in enumerate(CATEGORIES):
        cat = Category.objects.create(
            name=cat_info["name"],
            icon=cat_info["icon"],
            parent=None,
            sort_order=idx,
            is_active=True,
        )
        category_objs[cat_info["name"]] = cat
        print(f"Created category: {cat_info['name']}")

        for sub_idx, sub_name in enumerate(cat_info["subcategories"]):
            sub = Category.objects.create(
                name=sub_name,
                parent=cat,
                sort_order=sub_idx,
                is_active=True,
            )
            subcategory_objs[(cat_info["name"], sub_name)] = sub

    print(f"\nImporting {len(api_products)} products...")
    product_idx = 0

    for item in api_products:
        api_cat = item.get("category", "").lower()
        cat_name = API_CATEGORY_MAP.get(api_cat)
        if not cat_name:
            print(f"  Skipping unknown category: {api_cat}")
            continue

        title = item.get("title", "")
        rule = API_SUBCATEGORY_MAP.get(cat_name)
        sub_name = rule(title) if rule else category_objs[cat_name].children.first().name
        sub_cat = subcategory_objs.get((cat_name, sub_name))
        if not sub_cat:
            sub_cat = category_objs[cat_name].children.first()

        price = Decimal(str(item.get("price", 0)))
        original_price = (price * Decimal("1.3")).quantize(Decimal("0.01"))

        is_hot = product_idx < 6
        is_new = product_idx % 4 == 0
        is_recommended = product_idx % 3 == 0
        sales = 100 + product_idx * 45
        rating_val = Decimal(str(round(4.0 + (product_idx % 10) * 0.1, 1)))
        review_count = 30 + product_idx * 15

        specs = generate_specs(cat_name, seed=product_idx)

        product = Product.objects.create(
            name=title,
            category=sub_cat,
            brand=cat_name,
            price=price,
            original_price=original_price,
            stock=100 + product_idx * 7,
            sales=sales,
            rating=rating_val,
            review_count=review_count,
            description=item.get("description", ""),
            specs=json.dumps(specs, ensure_ascii=False, indent=2),
            is_hot=is_hot,
            is_new=is_new,
            is_recommended=is_recommended,
            is_active=True,
        )

        image_url = item.get("image", "")
        img_content = get_image_content(image_url)
        if img_content:
            product.image.save(f"product_{product.id}.jpg", img_content, save=True)

        num_reviews = min(review_count, 15 + product_idx % 10)
        reviews = generate_reviews(product, count=num_reviews, seed=product_idx)
        Review.objects.bulk_create(reviews)
        product.review_count = len(reviews)
        product.save(update_fields=["review_count"])

        print(f"  [{product_idx+1}] {title[:50]} -> {cat_name}/{sub_name}")
        product_idx += 1

    total_products = Product.objects.filter(is_active=True).count()
    total_categories = Category.objects.filter(is_active=True, parent__isnull=True).count()
    total_sub = Category.objects.filter(is_active=True, parent__isnull=False).count()
    total_reviews = Review.objects.count()

    print(f"\n{'='*50}")
    print(f"Import complete!")
    print(f"  Categories: {total_categories} main, {total_sub} subcategories")
    print(f"  Products:   {total_products} (all from Fake Store API)")
    print(f"  Reviews:    {total_reviews}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
