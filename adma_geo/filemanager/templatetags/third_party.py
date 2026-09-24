"""
Display names and icons for the platforms data was pulled from.

``third_party_source`` holds a machine key -- 'usda_cdl', 'nasa_power' -- and
the panels used to render it raw, which is readable enough for 'realm5' and
not at all for the rest. The two templates that show it had their own inline
{% if %} chains covering John Deere and Realm5 only, so every source added
since would have appeared as "Usda_Cdl".
"""
from django import template

register = template.Library()

SOURCES = {
    'johndeere': ('John Deere', 'fa-tractor'),
    'realm5': ('Realm5', 'fa-cloud'),
    'usda_cdl': ('USDA Cropland Data Layer', 'fa-seedling'),
    'nasa_power': ('NASA POWER', 'fa-satellite'),
    'open_meteo': ('Open-Meteo', 'fa-cloud-sun-rain'),
    'soilgrids': ('SoilGrids', 'fa-mountain'),
}

DEFAULT_ICON = 'fa-cloud'


@register.filter
def source_label(value):
    """A readable name for a third-party source key."""
    if not value:
        return 'External'
    known = SOURCES.get(value)
    if known:
        return known[0]
    # Unknown key: 'some_source' reads better as 'Some Source' than as-is.
    return value.replace('_', ' ').replace('-', ' ').title()


@register.filter
def source_icon(value):
    """The FontAwesome icon class for a third-party source key."""
    known = SOURCES.get(value or '')
    return known[1] if known else DEFAULT_ICON
