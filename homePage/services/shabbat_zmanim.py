import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from hdate import Location, Zmanim

logger = logging.getLogger(__name__)

# מיקום קבוע (ירושלים, מנהג מחמיר) לחישוב זמני כניסת/יציאת שבת וחג לכל האתר,
# ללא תלות במיקום המבקר בפועל.
_JERUSALEM_LOCATION = Location()
_CANDLE_LIGHTING_OFFSET_MINUTES = 40
_MAX_DAYS_TO_SEARCH_FOR_REOPEN = 6


def _zmanim_for(day):
    return Zmanim(
        date=day,
        location=_JERUSALEM_LOCATION,
        candle_lighting_offset=_CANDLE_LIGHTING_OFFSET_MINUTES,
    )


def _find_reopen_datetime(now):
    """
    מוצא את זמן הצאת שבת/חג הבא אחרי 'now'. מתקדם יום אחרי יום (עד תקרה)
    כדי לתמוך גם ברצפים (כניסת שבת/חג ישירות לשבת/חג נוסף), בלי לשכפל
    ידנית את לוגיקת ה"רצף" שכבר קיימת בספריית hdate.
    """
    for i in range(_MAX_DAYS_TO_SEARCH_FOR_REOPEN):
        day = now.date() + timedelta(days=i)
        havdalah = _zmanim_for(day).havdalah
        if havdalah is not None and havdalah > now:
            return havdalah
    return None


def get_closure_status():
    """
    האם האתר צריך להיות חסום כרגע בגלל שבת/חג (לפי זמני ירושלים, מחמיר),
    ואם כן - מתי הוא צפוי להיפתח שוב (לצורך header 'Retry-After').

    Fail open: כל שגיאה בחישוב מוחזרת כ"לא חסום", כדי שתקלה טכנית לא תסגור
    את האתר ביום חול.
    """
    try:
        now = timezone.now().astimezone(ZoneInfo("Asia/Jerusalem"))
        blocked = _zmanim_for(now.date()).issur_melacha_in_effect(now)
        if not blocked:
            return False, None
        return True, _find_reopen_datetime(now)
    except Exception:
        logger.exception("שגיאה בחישוב זמני שבת/חג - האתר נשאר פתוח (fail open)")
        return False, None
