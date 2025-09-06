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
