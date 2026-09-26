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

# Platforms ADMA syncs from, which have no dataset registry of their own.
SYNC_SOURCES = {
    'johndeere': ('John Deere', 'fa-tractor'),
    'realm5': ('Realm5', 'fa-cloud'),
}

# An icon per public dataset. The names come from the dataset registry itself
# rather than being repeated here: a second list is a list that goes stale,
# and it did -- four datasets were added and their badges read 'Ssurgo' and
# 'Usgs Wbd' until someone looked.
DATASET_ICONS = {
    'usda_cdl': 'fa-seedling',
    'nasa_power': 'fa-satellite',
    'open_meteo': 'fa-cloud-sun-rain',
    'soilgrids': 'fa-mountain',
    'usgs_3dep': 'fa-mountain-sun',
    'usgs_wbd': 'fa-water',
    'openstreetmap': 'fa-map',
    'ssurgo': 'fa-layer-group',
    'naip': 'fa-plane',
    'sentinel2': 'fa-satellite-dish',
    'plss': 'fa-border-all',
    'usdm': 'fa-sun-plant-wilt',
    'gnatsgo': 'fa-seedling',
    'sentinel1': 'fa-tower-broadcast',
    'modis_vi': 'fa-chart-line',
    'worldcover': 'fa-earth-americas',
    'copernicus_dem': 'fa-mountain',
    'surface_water': 'fa-droplet',
}

DEFAULT_ICON = 'fa-cloud'


def _sources():
    """source key -> (display name, icon), built from the dataset registry."""
    from filemanager.public_datasets import PUBLIC_DATASETS

    sources = dict(SYNC_SOURCES)
    for dataset in PUBLIC_DATASETS.values():
        sources[dataset.source] = (
            dataset.name, DATASET_ICONS.get(dataset.source, DEFAULT_ICON)
        )
    return sources


@register.filter
def source_label(value):
    """A readable name for a third-party source key."""
    if not value:
        return 'External'
    known = _sources().get(value)
    if known:
        return known[0]
    # Unknown key: 'some_source' reads better as 'Some Source' than as-is.
    return value.replace('_', ' ').replace('-', ' ').title()


@register.filter
def source_icon(value):
    """The FontAwesome icon class for a third-party source key."""
    known = _sources().get(value or '')
    return known[1] if known else DEFAULT_ICON
