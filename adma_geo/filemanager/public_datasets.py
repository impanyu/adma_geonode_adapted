"""
Public agricultural and climate datasets, fetched on demand for an area of
interest.

Each dataset here is free and needs no API key, and each was checked against
its live service before being added -- which is how USDA's Cropland Data Layer
ended up coming from Microsoft's Planetary Computer rather than from
CropScape. CropScape is the obvious source and its point query still answers,
but its raster services are broken at the server end: GetCDLFile hangs, the
WCS 404s, and its own WMS reports it cannot open its map file. Planetary
Computer serves the same USDA rasters as cloud-optimised GeoTIFFs, which also
lets us read just the window we need instead of a national download.

A fetcher takes an area of interest and returns the usual
``(success, message, output_files)``, so the results register through tool_io
like any other tool's output and land in the user's files.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 120
USER_AGENT = 'ADMA/1.0 (agricultural data platform; +https://adma.unl.edu)'

# A degree of latitude is ~111 km, so this caps a request at roughly 220 km a
# side. Past that these are whole-region downloads, not field work, and the
# ones backed by a raster window would pull hundreds of megabytes.
MAX_BBOX_DEGREES = 2.0

# Every host these fetchers may contact. Area of interest comes from user
# input, so the URLs are built here and never taken from a request.
ALLOWED_HOSTS = frozenset({
    'planetarycomputer.microsoft.com',
    'landcoverdata.blob.core.windows.net',
    'power.larc.nasa.gov',
    'archive-api.open-meteo.com',
    'maps.isric.org',
})


class DatasetError(Exception):
    """A fetch could not be completed, with a reason worth showing the user."""


def _open(url, data=None, headers=None, timeout=HTTP_TIMEOUT):
    host = urllib.parse.urlparse(url).hostname or ''
    if host not in ALLOWED_HOSTS:
        raise DatasetError(f'Refusing to contact an unexpected host: {host}')
    request = urllib.request.Request(url, data=data)
    request.add_header('User-Agent', USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise DatasetError(f'{host} answered {exc.code} ({exc.reason}).')
    except urllib.error.URLError as exc:
        raise DatasetError(f'Could not reach {host}: {exc.reason}.')
    except TimeoutError:
        raise DatasetError(f'{host} did not answer within {timeout} seconds.')


def check_bbox(bbox):
    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise DatasetError('The area of interest is not a valid lat/lon box.')
    if (maxx - minx) > MAX_BBOX_DEGREES or (maxy - miny) > MAX_BBOX_DEGREES:
        raise DatasetError(
            f'That area is larger than {MAX_BBOX_DEGREES} degrees a side. '
            'Pick a smaller boundary -- these datasets are meant for fields, '
            'not whole regions.'
        )


# --- USDA Cropland Data Layer ----------------------------------------------

CDL_COLLECTION = 'usda-cdl'
CDL_STAC_SEARCH = 'https://planetarycomputer.microsoft.com/api/stac/v1/search'
CDL_SAS_TOKEN = f'https://planetarycomputer.microsoft.com/api/sas/v1/token/{CDL_COLLECTION}'
CDL_FIRST_YEAR, CDL_LAST_YEAR = 2008, 2021


def fetch_cdl(aoi, output_dir, year=2021, **_):
    """Clip the USDA crop-type raster to the area of interest."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    year = int(year)
    if not CDL_FIRST_YEAR <= year <= CDL_LAST_YEAR:
        raise DatasetError(
            f'The Cropland Data Layer is available here for '
            f'{CDL_FIRST_YEAR}-{CDL_LAST_YEAR}. USDA publishes later years, '
            'but its own extract service is currently not responding.'
        )

    bbox = aoi['bbox']
    check_bbox(bbox)

    body = json.dumps({
        'collections': [CDL_COLLECTION], 'bbox': list(bbox), 'limit': 100,
    }).encode()
    with _open(CDL_STAC_SEARCH, data=body,
               headers={'Content-Type': 'application/json'}) as response:
        features = json.load(response).get('features', [])

    wanted = f'cropland_{year}_'
    items = [f for f in features if f['id'].startswith(wanted)]
    if not items:
        raise DatasetError(
            f'No Cropland Data Layer tile covers that area for {year}. '
            'The layer covers the continental United States only.'
        )

    href = items[0]['assets']['cropland']['href']
    with _open(CDL_SAS_TOKEN) as response:
        token = json.load(response)['token']
    signed = href + ('&' if '?' in href else '?') + token

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f'cdl_{year}.tif')

    with rasterio.open(signed) as source:
        left, bottom, right, top = transform_bounds('EPSG:4326', source.crs, *bbox)
        window = from_bounds(left, bottom, right, top, source.transform)
        data = source.read(1, window=window)
        if data.size == 0:
            raise DatasetError('That area falls outside the layer.')
        profile = source.profile.copy()
        profile.update(
            height=data.shape[0], width=data.shape[1],
            transform=source.window_transform(window),
            count=1, compress='lzw', driver='GTiff',
        )
        # The pixel values are class codes, not brightness, so the palette is
        # what makes the raster mean anything: corn yellow, soybeans green,
        # water blue. USDA ships it inside the GeoTIFF. Carrying it across
        # keeps the clip readable in GeoServer, QGIS and a plain download
        # alike -- without it every class renders as a shade of grey.
        try:
            palette = source.colormap(1)
        except ValueError:
            palette = None

    with rasterio.open(out_path, 'w', **profile) as destination:
        destination.write(data, 1)
        if palette:
            destination.write_colormap(1, palette)

    classes, counts = np.unique(data, return_counts=True)
    ranked = sorted(zip(classes.tolist(), counts.tolist()), key=lambda p: -p[1])
    named = [
        f'{CDL_CLASS_NAMES.get(code, f"class {code}")} {100.0 * n / data.size:.0f}%'
        for code, n in ranked[:4]
    ]
    message = (
        f'Cropland Data Layer {year} clipped to {data.shape[1]}x{data.shape[0]} px. '
        f'Mostly {", ".join(named)}.'
    )
    return True, message, {'cdl_tif': out_path}


# The codes that actually turn up on agricultural ground. Anything else is
# reported by number rather than guessed at.
CDL_CLASS_NAMES = {
    1: 'Corn', 2: 'Cotton', 3: 'Rice', 4: 'Sorghum', 5: 'Soybeans',
    6: 'Sunflower', 21: 'Barley', 22: 'Durum Wheat', 23: 'Spring Wheat',
    24: 'Winter Wheat', 27: 'Rye', 28: 'Oats', 29: 'Millet', 36: 'Alfalfa',
    37: 'Other Hay/Non Alfalfa', 41: 'Sugarbeets', 42: 'Dry Beans',
    43: 'Potatoes', 61: 'Fallow/Idle Cropland', 111: 'Open Water',
    121: 'Developed/Open Space', 122: 'Developed/Low Intensity',
    123: 'Developed/Medium Intensity', 124: 'Developed/High Intensity',
    131: 'Barren', 141: 'Deciduous Forest', 142: 'Evergreen Forest',
    143: 'Mixed Forest', 152: 'Shrubland', 176: 'Grass/Pasture',
    190: 'Woody Wetlands', 195: 'Herbaceous Wetlands',
}


# --- NASA POWER -------------------------------------------------------------

POWER_URL = 'https://power.larc.nasa.gov/api/temporal/daily/point'
POWER_PARAMETERS = 'T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,ALLSKY_SFC_SW_DWN,RH2M,WS2M'


def fetch_nasa_power(aoi, output_dir, start=None, end=None, **_):
    """Daily agroclimatology for the point at the centre of the area."""
    if not start or not end:
        raise DatasetError('Choose a start and end date.')

    query = urllib.parse.urlencode({
        'parameters': POWER_PARAMETERS, 'community': 'AG',
        'latitude': f"{aoi['lat']:.4f}", 'longitude': f"{aoi['lon']:.4f}",
        'start': start.replace('-', ''), 'end': end.replace('-', ''),
        'format': 'CSV',
    })
    with _open(f'{POWER_URL}?{query}') as response:
        text = response.read().decode('utf-8', errors='replace')

    # POWER wraps the table in a header block; keep it as a sidecar rather
    # than discard it, since it records the grid cell and elevation the
    # numbers actually describe.
    header, _, table = text.partition('-END HEADER-')
    if not table:
        header, table = '', text

    os.makedirs(output_dir, exist_ok=True)
    base = f"nasa_power_{start}_{end}"
    csv_path = os.path.join(output_dir, f'{base}.csv')
    with open(csv_path, 'w', encoding='utf-8') as handle:
        handle.write(table.lstrip('\r\n'))

    outputs = {'weather_csv': csv_path}
    if header.strip():
        meta_path = os.path.join(output_dir, f'{base}_about.txt')
        with open(meta_path, 'w', encoding='utf-8') as handle:
            handle.write(header.replace('-BEGIN HEADER-', '').strip())
        outputs['metadata_txt'] = meta_path

    rows = max(0, len(table.strip().splitlines()) - 1)
    return True, (
        f'NASA POWER daily agroclimatology for {aoi["lat"]:.3f}, '
        f'{aoi["lon"]:.3f}: {rows} day(s) from {start} to {end}.'
    ), outputs


# --- Open-Meteo -------------------------------------------------------------

OPEN_METEO_URL = 'https://archive-api.open-meteo.com/v1/archive'
OPEN_METEO_DAILY = (
    'temperature_2m_max,temperature_2m_min,temperature_2m_mean,'
    'precipitation_sum,et0_fao_evapotranspiration,'
    'shortwave_radiation_sum,windspeed_10m_max'
)


def fetch_open_meteo(aoi, output_dir, start=None, end=None, **_):
    """Daily reanalysis weather, including reference evapotranspiration."""
    if not start or not end:
        raise DatasetError('Choose a start and end date.')

    query = urllib.parse.urlencode({
        'latitude': f"{aoi['lat']:.4f}", 'longitude': f"{aoi['lon']:.4f}",
        'start_date': start, 'end_date': end,
        'daily': OPEN_METEO_DAILY, 'timezone': 'auto', 'format': 'csv',
    })
    with _open(f'{OPEN_METEO_URL}?{query}') as response:
        text = response.read().decode('utf-8', errors='replace')

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'open_meteo_{start}_{end}.csv')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)

    rows = max(0, len(text.strip().splitlines()) - 4)
    return True, (
        f'Open-Meteo daily archive for {aoi["lat"]:.3f}, {aoi["lon"]:.3f}: '
        f'about {rows} day(s) from {start} to {end}.'
    ), {'weather_csv': path}


# --- SoilGrids --------------------------------------------------------------

SOILGRIDS_URL = 'https://maps.isric.org/mapserv'

SOILGRIDS_PROPERTIES = {
    'soc': 'Soil organic carbon',
    'clay': 'Clay content',
    'sand': 'Sand content',
    'phh2o': 'pH in water',
    'bdod': 'Bulk density',
    'cec': 'Cation exchange capacity',
    'nitrogen': 'Total nitrogen',
}
SOILGRIDS_DEPTHS = ['0-5cm', '5-15cm', '15-30cm', '30-60cm', '60-100cm']


def fetch_soilgrids(aoi, output_dir, soil_property='soc', depth='0-5cm', **_):
    """
    A SoilGrids property raster clipped to the area of interest.

    Asked for in WGS84 rather than in SoilGrids' native Homolosine. Homolosine
    has no EPSG code, and GeoServer will not publish a coverage whose SRS it
    cannot name -- the layer processed cleanly and then failed to publish, so
    it could not be opened on a map. Letting the WCS do the reprojection also
    beats resampling it here afterwards.
    """
    if soil_property not in SOILGRIDS_PROPERTIES:
        raise DatasetError(
            f'Unknown soil property. Available: {", ".join(sorted(SOILGRIDS_PROPERTIES))}.'
        )
    if depth not in SOILGRIDS_DEPTHS:
        raise DatasetError(f'Unknown depth. Available: {", ".join(SOILGRIDS_DEPTHS)}.')

    minx, miny, maxx, maxy = aoi['bbox']
    check_bbox(aoi['bbox'])

    crs_uri = 'http://www.opengis.net/def/crs/EPSG/0/4326'
    query = (
        f'map=/map/{soil_property}.map&SERVICE=WCS&VERSION=2.0.1'
        f'&REQUEST=GetCoverage&COVERAGEID={soil_property}_{depth}_mean'
        f'&FORMAT=GEOTIFF_INT16'
        f'&SUBSET=X({minx:.6f},{maxx:.6f})&SUBSET=Y({miny:.6f},{maxy:.6f})'
        f'&SUBSETTINGCRS={crs_uri}&OUTPUTCRS={crs_uri}'
    )

    with _open(f'{SOILGRIDS_URL}?{query}') as response:
        payload = response.read()

    if payload[:2] not in (b'II', b'MM'):
        raise DatasetError(
            'SoilGrids returned something that is not a GeoTIFF for that area.'
        )

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'soilgrids_{soil_property}_{depth}.tif')
    with open(path, 'wb') as handle:
        handle.write(payload)

    import rasterio

    with rasterio.open(path, 'r+') as source:
        # The service does not always stamp the CRS even when it honours
        # OUTPUTCRS, and a raster without one cannot be placed on a map.
        if source.crs is None:
            source.crs = rasterio.crs.CRS.from_epsg(4326)
        width, height = source.width, source.height

    return True, (
        f'{SOILGRIDS_PROPERTIES[soil_property]} at {depth}, '
        f'{width}x{height} px in WGS84. Values are in SoilGrids mapped units -- '
        'see the ISRIC documentation for the conversion factor.'
    ), {'soil_tif': path}


# --- registry ---------------------------------------------------------------

class PublicDataset:
    def __init__(self, key, name, source, kind, needs, description, fetch, options=None):
        self.key = key
        self.name = name
        self.source = source          # value stored in File.third_party_source
        self.kind = kind              # 'raster' or 'table'
        self.needs = needs            # 'bbox' or 'point'
        self.description = description
        self.fetch = fetch
        self.options = options or []


PUBLIC_DATASETS = {d.key: d for d in [
    PublicDataset(
        'usda_cdl', 'USDA Cropland Data Layer', 'usda_cdl', 'raster', 'bbox',
        'Crop type for every 30 m pixel, from USDA. Feeds straight into Zonal '
        'Statistics to get the crop make-up of each field.',
        fetch_cdl,
        options=[{'name': 'year', 'label': 'Year', 'type': 'select',
                  'choices': [str(y) for y in range(CDL_LAST_YEAR, CDL_FIRST_YEAR - 1, -1)],
                  'default': str(CDL_LAST_YEAR)}],
    ),
    PublicDataset(
        'nasa_power', 'NASA POWER agroclimatology', 'nasa_power', 'table', 'point',
        'Daily temperature, rainfall, solar radiation, humidity and wind, from '
        'NASA, for the centre of the area. Built for agriculture; 1981 to now.',
        fetch_nasa_power,
        options=[{'name': 'start', 'label': 'Start date', 'type': 'date'},
                 {'name': 'end', 'label': 'End date', 'type': 'date'}],
    ),
    PublicDataset(
        'open_meteo', 'Open-Meteo daily archive', 'open_meteo', 'table', 'point',
        'Daily reanalysis weather back to 1940, including reference '
        'evapotranspiration (ET0) which NASA POWER does not carry.',
        fetch_open_meteo,
        options=[{'name': 'start', 'label': 'Start date', 'type': 'date'},
                 {'name': 'end', 'label': 'End date', 'type': 'date'}],
    ),
    PublicDataset(
        'soilgrids', 'SoilGrids soil properties', 'soilgrids', 'raster', 'bbox',
        'Global soil property rasters from ISRIC at 250 m: organic carbon, '
        'texture, pH, bulk density, CEC and nitrogen, by depth layer.',
        fetch_soilgrids,
        options=[
            {'name': 'soil_property', 'label': 'Property', 'type': 'select',
             'choices': sorted(SOILGRIDS_PROPERTIES),
             'labels': SOILGRIDS_PROPERTIES, 'default': 'soc'},
            {'name': 'depth', 'label': 'Depth', 'type': 'select',
             'choices': SOILGRIDS_DEPTHS, 'default': '0-5cm'},
        ],
    ),
]}


def fetch_dataset(dataset_key, aoi, output_dir, **params):
    dataset = PUBLIC_DATASETS.get(dataset_key)
    if dataset is None:
        raise DatasetError(
            f'Unknown dataset. Available: {", ".join(sorted(PUBLIC_DATASETS))}.'
        )
    return dataset.fetch(aoi, output_dir, **params)
