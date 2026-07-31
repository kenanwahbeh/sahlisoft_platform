"""Forms for tenant self-service.

The subdomain a shop asks for becomes a DNS label and a public hostname before
it is ever a slug, so it is validated here rather than left to ``SlugField``:
underscores, uppercase letters and leading or trailing hyphens all satisfy
Django's slug validator and none of them belong in a host name.

Collision handling deliberately does *not* live in the form. A form that
rejected a taken name would push the user into guessing; ``signals.unique_slug``
already knows how to walk to the next free label, and reusing it is the only way
the self-service path and the signup path can be guaranteed to agree.
"""

import re

from django import forms
from django.conf import settings

from .models import Membership
from .signals import SLUG_MAX_LENGTH

# One DNS label: lowercase alphanumerics, inner hyphens only. This is the same
# shape the middleware has to match when the label comes back as a Host header.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

SUBDOMAIN_MIN_LENGTH = 2


class ShopCreateForm(forms.Form):
    """Name a shop, optionally name its subdomain.

    Takes the acting user because "is this a duplicate?" is a question about
    that user's own shops -- two different owners may both call their shop
    "مؤسسة النور" without anyone being confused.
    """

    name = forms.CharField(
        label="اسم المتجر",
        max_length=200,
        error_messages={
            "required": "أدخل اسم المتجر.",
            "max_length": "اسم المتجر طويل جداً.",
        },
        widget=forms.TextInput(attrs={"placeholder": "مثال: مؤسسة النور للتجارة"}),
    )
    subdomain = forms.CharField(
        label="النطاق الفرعي (اختياري)",
        required=False,
        max_length=SLUG_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": "alnoor", "dir": "ltr"}),
        help_text=(
            "أحرف إنجليزية صغيرة وأرقام وشرطات فقط. "
            "اتركه فارغاً وسنختار لك نطاقاً مناسباً."
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("أدخل اسم المتجر.")
        if self.user is not None and Membership.objects.filter(
            user=self.user,
            role=Membership.Role.OWNER,
            tenant__name__iexact=name,
        ).exists():
            raise forms.ValidationError("لديك متجر بهذا الاسم بالفعل.")
        return name

    def clean_subdomain(self):
        value = (self.cleaned_data.get("subdomain") or "").strip().lower()
        if not value:
            return ""
        if len(value) < SUBDOMAIN_MIN_LENGTH:
            raise forms.ValidationError(
                f"النطاق الفرعي قصير جداً: {SUBDOMAIN_MIN_LENGTH} أحرف على الأقل."
            )
        if len(value) > SLUG_MAX_LENGTH:
            raise forms.ValidationError(
                f"النطاق الفرعي طويل جداً: {SLUG_MAX_LENGTH} حرفاً كحد أقصى."
            )
        if not _LABEL_RE.match(value):
            raise forms.ValidationError(
                "النطاق الفرعي يقبل الأحرف الإنجليزية الصغيرة والأرقام والشرطة (-) فقط، "
                "ولا يبدأ أو ينتهي بشرطة."
            )
        if value in settings.RESERVED_SUBDOMAINS:
            raise forms.ValidationError(
                "هذا النطاق الفرعي محجوز للمنصة. اختر اسماً آخر."
            )
        return value
