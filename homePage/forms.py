from django import forms
from .models import SiteReview, CancellationRequest

class SiteReviewForm(forms.ModelForm):
    website = forms.CharField(required=False, widget=forms.HiddenInput)  # honeypot נסתר

    # דירוג – חובה, 1–5, עם הודעות שגיאה בעברית
    rating = forms.IntegerField(
        required=True,
        min_value=1, max_value=5,
        error_messages={
            "required": "יש לדרג  לפני השליחה",
            "min_value": "הדירוג חייב להיות לפחות 1.",
            "max_value": "הדירוג לא יכול להיות מעל 5.",
        }
    )

    class Meta:
        model  = SiteReview
        fields = ['name', 'rating', 'comment']           # שימי לב: אין email
        labels = {'name': 'שם', 'rating': 'דירוג', 'comment': 'תגובה'}
        widgets = {'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'איך היה? 🙂'})}

    def clean(self):
        data = super().clean()
        if self.cleaned_data.get('website'):             # אם honeypot מולא → כנראה בוט
            raise forms.ValidationError("Request rejected.")
        return data

    def clean_rating(self):
        # הגנת-יתר: אם משום מה לא הגיע ערך
        r = self.cleaned_data.get("rating")
        if not r:
            raise forms.ValidationError("יש לבחור דירוג כוכבים.")
        return r

class CancelRequestForm(forms.ModelForm):
    # אנטי-ספאם פשוט
    honeypot = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = CancellationRequest
        fields = [
            "full_name", "phone", "email",
            "order_id", "start_dt", "reason",
            "booking", "appointment",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "*שם מלא שנרשם בעת הרכישה"}),
            "phone": forms.TextInput(attrs={"placeholder": "*טלפון", "inputmode": "tel"}),
            "email": forms.EmailInput(attrs={"placeholder": "אימייל"}),
            "order_id": forms.TextInput(attrs={"placeholder": "*מס׳ הזמנה (כפי שמופיע באישור ההזמנה לדוגמה: MZ-12345678) "}),
            "start_dt": forms.DateTimeInput(attrs={"type": "datetime-local", "dir": "rtl"}),
            "reason": forms.Textarea(attrs={"rows": 3, "placeholder": "סיבת הביטול"}),
            "booking": forms.HiddenInput(),
            "appointment": forms.HiddenInput(),
        }

    # 1) להצמיד אטריביוטים ל-<input> שנוצר ל-phone
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # מס' הזמנה – טקסט בועה מותאם
        self.fields['order_id'].widget.attrs.update({
            "required": True,
            "oninvalid": "this.setCustomValidity('חובה להזין מס׳ הזמנה')",
            "oninput": "this.setCustomValidity('')",
        })

        self.fields['full_name'].widget.attrs.update({
            "required": True,
            "pattern": r".*\S.*",
            "oninvalid": "this.setCustomValidity('חובה למלא שם מלא')",
            "oninput": "this.setCustomValidity('')",
        })

        # אימייל – הודעה שונה אם הפורמט לא תקין
        self.fields['email'].widget.attrs.update({
            "oninvalid": "this.setCustomValidity(this.validity.typeMismatch ? 'כתובת אימייל לא תקינה' : 'חובה למלא אימייל')",
            "oninput": "this.setCustomValidity('')",
        })

        # טלפון (אם את רוצה בועה מותאמת)
        self.fields['phone'].widget.attrs.update({
            # אל תוסיפי required אם אצלך הכלל הוא 'טלפון או אימייל'
            "pattern": r"^0(5\d{8}|[2-9]\d{7})$",
            "title": "מספר טלפון ישראלי לא תקין",
            "oninvalid": "this.setCustomValidity(this.validity.patternMismatch ? 'מספר טלפון ישראלי לא תקין' : 'חובה למלא טלפון')",
            "oninput": "this.setCustomValidity('')",
        })

    # 2) ולידציה שרתית ישראלית (כולל תמיכה ב-+972)
    def clean_phone(self):
        import re
        p = (self.cleaned_data.get("phone") or "").strip()
        if not p:
            return p  # לא מכשיל כאן כי מותר להשאיר ריק אם יש אימייל
        d = re.sub(r"\D", "", p)
        if d.startswith("972"):
            d = "0" + d[3:]
        if not re.match(r"^0(5\d{8}|[2-9]\d{7})$", d):
            raise forms.ValidationError("מספר טלפון ישראלי לא תקין")
        return d

    def clean(self):
            cleaned = super().clean()

            # ===== מיפוי מה חסר =====
            def _blank(v):
                return not (v or "").strip()

            missing = {
                "full_name": _blank(cleaned.get("full_name")),
                "phone": _blank(cleaned.get("phone")),
                "email": _blank(cleaned.get("email")),
                "order_id": _blank(cleaned.get("order_id")),
                "start_dt": not cleaned.get("start_dt"),
            }
            missing_count = sum(missing.values())

            # אנטי-ספאם (נשאר כמו שהיה)
            if cleaned.get("honeypot"):
                raise forms.ValidationError("שגיאה בהגשה. נסו שוב.")

            # ===== דרישתך: שגיאה לשם מלא רק אם הוא היחיד שחסר =====
            if missing["full_name"] and missing_count == 1:
                self.add_error("full_name", "חובה למלא שם מלא")
                return cleaned  # שאר השדות תקינים, אין צורך בהמשך בדיקות

            # טלפון/אימייל: שגיאה צמודה לשני השדות רק כששניהם חסרים
            if missing["phone"] and missing["email"] and missing_count == 2:
                self.add_error("phone", "יש להזין טלפון או אימייל")
                self.add_error("email", "יש להזין טלפון או אימייל")
                return cleaned  # לא raise → לא תופיע הודעה כללית

            # הודעה כללית אם חסרים יותר משני שדות
            if missing_count >= 3:
                self.add_error(None, "חלק מהשדות לא מולאו. אנא השלימו את הפרטים המסומנים.")

            # ===== ההיגיון המקורי שלך, מותאם כדי לא לייצר כפילויות =====

            # טלפון/אימייל: הודעה רק אם אלה שני השדות היחידים שחסרים
            if missing["phone"] and missing["email"] and missing_count == 2:
                raise forms.ValidationError("יש להזין טלפון או אימייל")

            # מס' הזמנה: הודעה רק אם זה השדה היחיד שחסר
            if missing["order_id"] and missing_count == 1:
                raise forms.ValidationError("חובה להזין מס׳ הזמנה")

            # אם גם מס' הזמנה וגם מועד חסרים, ורק הם חסרים → הודעה לזוג
            if missing["order_id"] and missing["start_dt"] and missing_count == 2:
                raise forms.ValidationError("יש להזין מס׳ הזמנה או מועד הרכיבה")

            return cleaned

