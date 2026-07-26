def build_breadcrumbs(*items):
    """
    items:
    ("首页", "shop:home")
    ("家居日用", None)
    """
    breadcrumbs = []
    for name, url in items:
        breadcrumbs.append({
            "name": name,
            "url": url
        })
    return breadcrumbs