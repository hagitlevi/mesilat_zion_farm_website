from django import forms
from .models import SiteReview

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