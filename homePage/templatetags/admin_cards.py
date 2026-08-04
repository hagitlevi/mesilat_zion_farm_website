from django import template
from django.conf import settings

register = template.Library()

@register.filter(name="order_models")
def order_models(models, app_label):
    """
    ממיין את רשימת המודלים של אפליקציה לפי סדר שמוגדר ב-settings.ADMIN_MODEL_ORDER.
    תומך הן ב-object_name (שם המחלקה באנגלית) והן ב-name (שם תצוגה).
    """
    cfg = getattr(settings, "ADMIN_MODEL_ORDER", {})
    conf = cfg.get(app_label) or {}

    order_obj = conf.get("object_name", [])
    order_name = conf.get("name", [])

    pos_obj = {n: i for i, n in enumerate(order_obj)}
    pos_name = {n: i for i, n in enumerate(order_name)}

    def key(m):
        # m הוא dict עם מפתחות "object_name" ו-"name" בדף הראשי של האדמין
        return (
            pos_obj.get(m.get("object_name"), 10**6),
            pos_name.get(m.get("name"), 10**6),
            m.get("name"),
        )

    try:
        return sorted(models, key=key)
    except Exception:
        return models


@register.simple_tag
def group_models(models, app_label):
    """
    מחלק רשימת מודלים (כבר ממוינת) לתיקיות לפי settings.ADMIN_MODEL_GROUPS.
    מחזיר רשימת (שם_קבוצה, [מודלים]) בסדר הקבוצות המוגדר. מודל שלא שובץ
    לאף קבוצה נופל ל"אחר" בסוף.
    """
    groups = getattr(settings, "ADMIN_MODEL_GROUPS", {}).get(app_label)
    if not groups:
        return [("", list(models))]

    group_of = {
        object_name: group_name
        for group_name, object_names in groups
        for object_name in object_names
    }
    order_within = {
        object_name: i
        for _, object_names in groups
        for i, object_name in enumerate(object_names)
    }
    buckets = {name: [] for name, _ in groups}
    buckets["אחר"] = []
    for m in models:
        buckets[group_of.get(m.get("object_name"), "אחר")].append(m)

    result = []
    for name, _ in groups:
        bucket = sorted(buckets[name], key=lambda m: order_within.get(m.get("object_name"), 999))
        if bucket:
            result.append((name, bucket))
    if buckets["אחר"]:
        result.append(("אחר", buckets["אחר"]))
    return result
