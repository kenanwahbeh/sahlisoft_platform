"""Arabic-correct relative time for the dashboard.

Django ships ``timesince``, and its Arabic catalogue does translate the unit
names, but it pluralises with gettext's two-form logic: one string for n == 1
and one for everything else. Arabic needs four. "منذ 2 دقائق" and "منذ 11
دقائق" are both wrong, and both are what the built-in filter produces -- on a
page whose whole job is to say how long ago a shop's machine last called home,
that reads as broken software rather than as a rounding detail.

The four forms, for a unit like "دقيقة":

===========  ===================  ==================================
count        form                 example
===========  ===================  ==================================
1            singular, no digit   منذ دقيقة
2            dual, no digit       منذ دقيقتين
3 - 10       digit + plural       منذ 3 دقائق
11 and up    digit + singular     منذ 11 دقيقة
===========  ===================  ==================================

Counts of 1 and 2 deliberately carry no numeral: Arabic marks those in the
noun itself, so "منذ 1 دقيقة" is the kind of phrasing that only ever comes out
of a machine. 11 and up take the accusative singular (تمييز) -- "منذ 11 يوماً",
not "منذ 11 يوم" -- which is why days and months carry a fourth form that
differs from their first.

Digits stay Western (0-9) rather than Arabic-Indic. Both are read fluently in
Syria, the rest of the dashboard already prints dates as 2026-08-02, and mixing
numeral systems inside one page is worse than either choice on its own.
"""

from django import template
from django.utils import timezone

register = template.Library()

#: ``(seconds_per_unit, singular, dual, plural, singular_accusative)``, largest
#: unit first so the first threshold that fits wins.
_UNITS = (
    (60 * 60 * 24 * 365, "سنة", "سنتين", "سنوات", "سنة"),
    (60 * 60 * 24 * 30, "شهر", "شهرين", "أشهر", "شهراً"),
    (60 * 60 * 24, "يوم", "يومين", "أيام", "يوماً"),
    (60 * 60, "ساعة", "ساعتين", "ساعات", "ساعة"),
    (60, "دقيقة", "دقيقتين", "دقائق", "دقيقة"),
)

#: Below this, "منذ 12 ثانية" is noise -- the answer the reader wants is "now".
_JUST_NOW_SECONDS = 45


def arabic_count(count: int, singular: str, dual: str, plural: str, accusative: str) -> str:
    """One count in the right Arabic form, numeral included only where it belongs."""
    if count == 1:
        return singular
    if count == 2:
        return dual
    if 3 <= count <= 10:
        return f"{count} {plural}"
    return f"{count} {accusative}"


@register.filter
def ar_timesince(value, now=None) -> str:
    """``datetime`` -> "منذ 3 دقائق". Empty string for None, so callers can branch.

    Only ever looks backwards. A ``last_seen_at`` in the future means a clock
    skewed on the shop's machine or on ours, and "منذ لحظات" is a better answer
    to that than a negative duration or a crash.
    """
    if value is None:
        return ""

    now = now or timezone.now()
    seconds = int((now - value).total_seconds())

    if seconds < _JUST_NOW_SECONDS:
        return "منذ لحظات"

    for unit_seconds, singular, dual, plural, accusative in _UNITS:
        if seconds >= unit_seconds:
            count = seconds // unit_seconds
            return f"منذ {arabic_count(count, singular, dual, plural, accusative)}"

    return "منذ لحظات"
