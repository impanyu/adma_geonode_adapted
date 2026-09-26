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
import shutil
import urllib.error
import urllib.parse
import urllib.request

from .dbf_names import dbf_safe_columns

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
    'elevation.nationalmap.gov',
    'gis.blm.gov',
    'droughtmonitor.unl.edu',
    'naipeuwest.blob.core.windows.net',
    'sentinel2l2a01.blob.core.windows.net',
    'hydro.nationalmap.gov',
    'overpass.kumi.systems',
    'overpass-api.de',
    'sdmdataaccess.sc.egov.usda.gov',
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

PC_SEARCH = 'https://planetarycomputer.microsoft.com/api/stac/v1/search'
PC_TOKEN = 'https://planetarycomputer.microsoft.com/api/sas/v1/token/'


def pc_search(collection, bbox, **extra):
    """Search a Planetary Computer collection over an area."""
    body = json.dumps({
        'collections': [collection], 'bbox': list(bbox), 'limit': 50, **extra,
    }).encode()
    with _open(PC_SEARCH, data=body,
               headers={'Content-Type': 'application/json'}) as response:
        return json.load(response).get('features', [])


def pc_sign(href, collection):
    """Add the read token a Planetary Computer asset needs. No account required."""
    with _open(PC_TOKEN + collection) as response:
        token = json.load(response)['token']
    return href + ('&' if '?' in href else '?') + token


def _clip_cog(signed_href, bbox, out_path, indexes=None, max_pixels=2048, out_shape=None):
    """
    Read just the window of a cloud-optimised GeoTIFF that covers ``bbox``.

    Reading a window rather than the scene is the point of a COG: a NAIP tile
    is 0.6 m, so a few square kilometres would otherwise be a hundred million
    pixels per band. ``max_pixels`` caps the longer side and the read is
    decimated to suit, which keeps a request for a whole township from
    returning a gigabyte.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    with rasterio.open(signed_href) as source:
        left, bottom, right, top = transform_bounds('EPSG:4326', source.crs, *bbox)
        window = from_bounds(left, bottom, right, top, source.transform)
        window = window.intersection(
            rasterio.windows.Window(0, 0, source.width, source.height)
        )
        if window.width < 1 or window.height < 1:
            raise DatasetError('That area falls outside the imagery.')

        bands = indexes or list(range(1, source.count + 1))
        if out_shape:
            out_height, out_width = out_shape
        else:
            scale = max(1.0, max(window.width, window.height) / float(max_pixels))
            out_height = max(1, int(window.height / scale))
            out_width = max(1, int(window.width / scale))

        data = source.read(
            bands, window=window, out_shape=(len(bands), out_height, out_width),
            resampling=Resampling.bilinear,
        )
        profile = source.profile.copy()
        profile.update(
            height=out_height, width=out_width, count=len(bands),
            transform=source.window_transform(window) * rasterio.Affine.scale(
                window.width / out_width, window.height / out_height
            ),
            compress='lzw', driver='GTiff',
        )
        profile.pop('photometric', None)

    with rasterio.open(out_path, 'w', **profile) as destination:
        destination.write(data)

    return out_width, out_height, len(bands)

# CDL codes 121-124 are the four developed classes, and USDA gives all four
# the same grey -- so a town reads as one flat grey block that can swamp the
# field you actually came to look at. These presets drop chosen classes out of
# the raster so the crops underneath stand out.
#
# Masked pixels become 0, which is CDL's own background value, and 0 is set as
# the raster's nodata with a fully transparent palette entry. That makes them
# transparent on a map and, because the rest of the platform honours nodata,
# leaves them out of Zonal Statistics rather than counted as a crop.
CDL_MASK_PRESETS = {
    'none': (),
    'developed': (121, 122, 123, 124),
    'non_agricultural': (
        111, 112,                 # water, ice
        121, 122, 123, 124,       # developed
        131,                      # barren
        141, 142, 143,            # forest
        152,                      # shrubland
        190, 195,                 # wetlands
    ),
}
CDL_MASK_LABELS = {
    'none': 'Show everything',
    'developed': 'Hide developed land (the grey)',
    'non_agricultural': 'Hide everything but crops and pasture',
}


def fetch_cdl(aoi, output_dir, year=2021, mask='none', **_):
    """Clip the USDA crop-type raster to the area of interest."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    if mask not in CDL_MASK_PRESETS:
        raise DatasetError(
            f'Unknown mask. Available: {", ".join(sorted(CDL_MASK_PRESETS))}.'
        )

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

    hidden = CDL_MASK_PRESETS[mask]
    hidden_share = 0.0
    if hidden:
        drop = np.isin(data, hidden)
        hidden_share = 100.0 * drop.sum() / data.size if data.size else 0.0
        data = np.where(drop, 0, data).astype(profile['dtype'])
        profile['nodata'] = 0
        if palette:
            palette = dict(palette)
            # Opaque black, not a transparent entry. GeoServer flattens a
            # palette to RGB before drawing, so alpha here is simply lost and
            # the masked ground came back painted white. Black is a colour the
            # CDL palette never uses, so the publisher can key transparency to
            # it -- and readers that honour nodata, such as QGIS, hide these
            # pixels anyway.
            palette[0] = (0, 0, 0, 255)

    with rasterio.open(out_path, 'w', **profile) as destination:
        destination.write(data, 1)
        if palette:
            destination.write_colormap(1, palette)

    classes, counts = np.unique(data, return_counts=True)
    ranked = [
        (code, n) for code, n in
        sorted(zip(classes.tolist(), counts.tolist()), key=lambda p: -p[1])
        if not (hidden and code == 0)
    ]
    named = [
        f'{CDL_CLASS_NAMES.get(code, f"class {code}")} {100.0 * n / data.size:.0f}%'
        for code, n in ranked[:4]
    ]
    message = (
        f'Cropland Data Layer {year} clipped to {data.shape[1]}x{data.shape[0]} px. '
        f'Mostly {", ".join(named) or "nothing left after masking"}.'
    )
    if hidden:
        message += (
            f' {hidden_share:.0f}% of the area was masked out and is transparent.'
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



# --- USGS 3DEP elevation ----------------------------------------------------

ELEVATION_URL = (
    'https://elevation.nationalmap.gov/arcgis/rest/services/'
    '3DEPElevation/ImageServer/exportImage'
)
# The service returns whatever grid is asked for, so this caps the request
# rather than the ground it covers.
ELEVATION_MAX_PIXELS = 2048


def fetch_elevation(aoi, output_dir, resolution=512, **_):
    """A bare-earth elevation raster for the area, from USGS 3DEP."""
    import rasterio

    bbox = aoi['bbox']
    check_bbox(bbox)

    try:
        size = int(resolution)
    except (TypeError, ValueError):
        raise DatasetError('Resolution must be a number of pixels.')
    size = max(64, min(size, ELEVATION_MAX_PIXELS))

    query = urllib.parse.urlencode({
        'bbox': ','.join(f'{v:.6f}' for v in bbox),
        'bboxSR': 4326, 'imageSR': 4326,
        'size': f'{size},{size}',
        'format': 'tiff', 'pixelType': 'F32', 'f': 'image',
    })

    with _open(f'{ELEVATION_URL}?{query}') as response:
        payload = response.read()

    if payload[:2] not in (b'II', b'MM'):
        raise DatasetError(
            'USGS returned something that is not a GeoTIFF. 3DEP covers the '
            'United States and its territories only.'
        )

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, 'usgs_3dep_elevation.tif')
    with open(path, 'wb') as handle:
        handle.write(payload)

    import numpy as np
    with rasterio.open(path, 'r+') as source:
        if source.crs is None:
            source.crs = rasterio.crs.CRS.from_epsg(4326)
        band = source.read(1, masked=True)
        width, height = source.width, source.height

    valid = band.compressed() if np.ma.isMaskedArray(band) else band.ravel()
    valid = valid[np.isfinite(valid)]
    if valid.size == 0:
        raise DatasetError('No elevation data covers that area.')

    return True, (
        f'USGS 3DEP elevation, {width}x{height} px. '
        f'Ground runs {valid.min():.1f} to {valid.max():.1f} m, '
        f'a relief of {valid.max() - valid.min():.1f} m.'
    ), {'elevation_tif': path}


# --- USGS watershed boundaries ---------------------------------------------

WATERSHED_URL = (
    'https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer'
)
# The Watershed Boundary Dataset nests from region down to subwatershed; the
# smaller units are the ones a field sits inside.
WATERSHED_LAYERS = {
    'huc8': (4, 'Subbasin (HUC8)'),
    'huc10': (5, 'Watershed (HUC10)'),
    'huc12': (6, 'Subwatershed (HUC12)'),
}


def fetch_watersheds(aoi, output_dir, level='huc12', **_):
    """The watershed polygons a field sits inside."""
    import geopandas as gpd

    if level not in WATERSHED_LAYERS:
        raise DatasetError(
            f'Unknown level. Available: {", ".join(sorted(WATERSHED_LAYERS))}.'
        )
    layer_id, label = WATERSHED_LAYERS[level]

    bbox = aoi['bbox']
    check_bbox(bbox)

    query = urllib.parse.urlencode({
        'geometry': ','.join(f'{v:.6f}' for v in bbox),
        'geometryType': 'esriGeometryEnvelope',
        'inSR': 4326, 'outSR': 4326,
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': '*', 'f': 'geojson', 'resultRecordCount': 50,
    })

    with _open(f'{WATERSHED_URL}/{layer_id}/query?{query}') as response:
        payload = json.loads(response.read().decode('utf-8'))

    features = payload.get('features') or []
    if not features:
        raise DatasetError(
            f'No {label} polygon covers that area. The Watershed Boundary '
            'Dataset covers the United States only.'
        )

    frame = gpd.GeoDataFrame.from_features(features, crs='EPSG:4326')

    os.makedirs(output_dir, exist_ok=True)
    base = f'wbd_{level}'
    shp_path = os.path.join(output_dir, f'{base}.shp')
    # WBD ships long field names, several of which share their first ten
    # characters -- truncating alone produced duplicates and geopandas
    # refused the write.
    dbf_safe_columns(frame).to_file(shp_path)

    return True, (
        f'{label}: {len(frame)} polygon(s) intersecting the area.'
    ), {
        'watersheds_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ]
    }


# --- OpenStreetMap ----------------------------------------------------------

# Overpass instances are shared and often saturated: the main one answered 504
# outright, and a mirror can still take well over a minute. Try them in turn
# and allow more time than the other services get.
OVERPASS_URLS = [
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass-api.de/api/interpreter',
]
OVERPASS_TIMEOUT = 180

OSM_FEATURES = {
    'roads': ('highway', 'Roads and tracks, including field access.'),
    'waterways': ('waterway', 'Streams, ditches and drains.'),
    'buildings': ('building', 'Buildings, including farmsteads and bins.'),
    'landuse': ('landuse', 'Land use polygons: farmland, meadow, forest.'),
}


def _osm_to_features(payload):
    """Turn an Overpass element list into GeoJSON features."""
    from shapely.geometry import LineString, Point, Polygon

    records = []
    for element in payload.get('elements', []):
        tags = element.get('tags') or {}
        kind = element.get('type')

        if kind == 'node' and element.get('lat') is not None:
            geometry = Point(element['lon'], element['lat'])
        elif kind == 'way' and element.get('geometry'):
            points = [(p['lon'], p['lat']) for p in element['geometry']]
            if len(points) < 2:
                continue
            closed = points[0] == points[-1] and len(points) >= 4
            # A closed way is an area only when its tags say so; a roundabout
            # is closed too and is still a road.
            area_like = closed and (
                'building' in tags or 'landuse' in tags or tags.get('area') == 'yes'
            )
            geometry = Polygon(points) if area_like else LineString(points)
        else:
            continue

        records.append({
            'geometry': geometry,
            'osm_id': element.get('id'),
            'osm_type': kind,
            'name': tags.get('name'),
            'category': (
                tags.get('highway') or tags.get('waterway')
                or tags.get('building') or tags.get('landuse')
            ),
        })
    return records


def fetch_osm(aoi, output_dir, feature='roads', **_):
    """Map features around a field, from OpenStreetMap."""
    import geopandas as gpd

    if feature not in OSM_FEATURES:
        raise DatasetError(
            f'Unknown feature type. Available: {", ".join(sorted(OSM_FEATURES))}.'
        )
    key, _description = OSM_FEATURES[feature]

    minx, miny, maxx, maxy = aoi['bbox']
    check_bbox(aoi['bbox'])

    # Overpass takes south,west,north,east.
    query = (
        f'[out:json][timeout:60];'
        f'(way["{key}"]({miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f});'
        f' node["{key}"]({miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f}););'
        f'out geom;'
    )
    body = urllib.parse.urlencode({'data': query}).encode()

    payload, last_error = None, None
    for url in OVERPASS_URLS:
        try:
            with _open(
                url, data=body,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=OVERPASS_TIMEOUT,
            ) as response:
                payload = json.loads(response.read().decode('utf-8'))
            break
        except DatasetError as exc:
            logger.warning('Overpass instance %s unavailable: %s', url, exc)
            last_error = exc

    if payload is None:
        raise DatasetError(
            f'No OpenStreetMap server answered. They are shared and often '
            f'busy -- trying again shortly usually works. ({last_error})'
        )

    records = _osm_to_features(payload)
    if not records:
        raise DatasetError(f'OpenStreetMap has no {feature} mapped in that area.')

    frame = gpd.GeoDataFrame(records, crs='EPSG:4326')

    os.makedirs(output_dir, exist_ok=True)
    base = f'osm_{feature}'
    # GeoJSON rather than a shapefile: one query returns points and lines
    # together -- a mapped stream and the gauge on it -- and a shapefile holds
    # exactly one geometry type, so the write fails partway through. GeoJSON
    # also keeps full-length field names, and the map viewer draws it directly.
    path = os.path.join(output_dir, f'{base}.geojson')
    frame.to_file(path, driver='GeoJSON')

    kinds = frame['category'].value_counts().head(3)
    summary = ', '.join(f'{k} {v}' for k, v in kinds.items())
    shapes = ', '.join(sorted(frame.geom_type.unique()))
    return True, (
        f'{len(frame)} OpenStreetMap {feature} feature(s) ({shapes}). '
        f'Mostly {summary}. Data \u00a9 OpenStreetMap contributors, ODbL.'
    ), {'osm_geojson': path}


# --- USDA SSURGO soil survey ------------------------------------------------

SDA_URL = 'https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest'


def _sda_query(sql):
    body = json.dumps({'query': sql, 'format': 'JSON'}).encode()
    with _open(SDA_URL, data=body,
               headers={'Content-Type': 'application/json'}) as response:
        raw = response.read().decode('utf-8', errors='replace')
    if not raw.lstrip().startswith('{'):
        raise DatasetError(
            'The USDA Soil Data Access service returned an error. It goes down '
            'for maintenance nightly around 12:30 AM US Central.'
        )
    return json.loads(raw).get('Table', []) or []


def fetch_ssurgo(aoi, output_dir, **_):
    """
    SSURGO soil map units as polygons, with the dominant soil named.

    SoilGrids is a 250 m global model; SSURGO is the surveyed map of the United
    States, mapped at field scale, and names the soil series the local
    agronomy is written about.
    """
    import geopandas as gpd
    from shapely import wkt as shapely_wkt

    minx, miny, maxx, maxy = aoi['bbox']
    check_bbox(aoi['bbox'])

    polygon = (
        f'polygon(({minx:.6f} {miny:.6f},{maxx:.6f} {miny:.6f},'
        f'{maxx:.6f} {maxy:.6f},{minx:.6f} {maxy:.6f},{minx:.6f} {miny:.6f}))'
    )

    sql = (
        "SELECT TOP 200 p.mupolygongeo.STAsText() AS wkt, m.mukey, m.musym, "
        "       m.muname "
        "FROM mupolygon p "
        "JOIN mapunit m ON m.mukey = p.mukey "
        f"WHERE p.mupolygonkey IN (SELECT mupolygonkey FROM "
        f"  SDA_Get_Mupolygonkey_from_intersection_with_WktWgs84('{polygon}'))"
    )

    rows = _sda_query(sql)
    if not rows:
        raise DatasetError(
            'No SSURGO soil survey covers that area. SSURGO is the United '
            'States survey; elsewhere, use SoilGrids.'
        )

    records = []
    for wkt_text, mukey, musym, muname in rows:
        try:
            geometry = shapely_wkt.loads(wkt_text)
        except Exception:
            continue
        records.append({
            'geometry': geometry, 'mukey': mukey,
            'musym': musym, 'muname': muname,
        })

    if not records:
        raise DatasetError('SSURGO returned map units without usable geometry.')

    frame = gpd.GeoDataFrame(records, crs='EPSG:4326')

    os.makedirs(output_dir, exist_ok=True)
    base = 'ssurgo_map_units'
    shp_path = os.path.join(output_dir, f'{base}.shp')
    frame.to_file(shp_path)

    names = frame['muname'].value_counts().head(2)
    summary = '; '.join(f'{k}' for k in names.index)
    return True, (
        f'{len(frame)} SSURGO map unit polygon(s). Mostly {summary}.'
    ), {
        'ssurgo_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ]
    }



# --- NAIP aerial imagery ----------------------------------------------------

NAIP_COLLECTION = 'naip'


def fetch_naip(aoi, output_dir, **_):
    """Sub-metre aerial imagery of US farmland, from USDA's NAIP programme."""
    bbox = aoi['bbox']
    check_bbox(bbox)

    features = pc_search(NAIP_COLLECTION, bbox)
    if not features:
        raise DatasetError(
            'No NAIP imagery covers that area. NAIP is flown over the '
            'continental United States only.'
        )

    # Newest flight first; NAIP is flown every two or three years per state.
    features.sort(
        key=lambda f: (f.get('properties') or {}).get('datetime') or '',
        reverse=True,
    )
    item = features[0]
    flown = ((item.get('properties') or {}).get('datetime') or '')[:10]

    href = pc_sign(item['assets']['image']['href'], NAIP_COLLECTION)

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'naip_{flown or "latest"}.tif')
    width, height, bands = _clip_cog(href, bbox, path)

    return True, (
        f'NAIP aerial imagery flown {flown}, {width}x{height} px, {bands} band(s) '
        '(red, green, blue, near-infrared).'
    ), {'naip_tif': path}


# --- Sentinel-2 -------------------------------------------------------------

SENTINEL_COLLECTION = 'sentinel-2-l2a'

# Band presets, named for what they are for rather than by band number.
SENTINEL_PRESETS = {
    'true_colour': (['B04', 'B03', 'B02'], 'Natural colour, as the eye sees it.'),
    'colour_infrared': (['B08', 'B04', 'B03'],
                        'Near-infrared as red: healthy canopy glows.'),
    'index_bands': (['B04', 'B05', 'B08'],
                    'Red, red edge and near-infrared -- the bands the '
                    'Vegetation Index tool needs for NDVI and NDRE.'),
}


def fetch_sentinel2(aoi, output_dir, bands='index_bands', start=None, end=None,
                    max_cloud=20, **_):
    """A cloud-free Sentinel-2 scene clipped to the area."""
    import rasterio

    if bands not in SENTINEL_PRESETS:
        raise DatasetError(
            f'Unknown band set. Available: {", ".join(sorted(SENTINEL_PRESETS))}.'
        )
    if not start or not end:
        raise DatasetError('Choose a start and end date.')

    bbox = aoi['bbox']
    check_bbox(bbox)

    wanted, _description = SENTINEL_PRESETS[bands]

    features = pc_search(
        SENTINEL_COLLECTION, bbox,
        datetime=f'{start}/{end}',
        query={'eo:cloud_cover': {'lt': float(max_cloud)}},
    )
    if not features:
        raise DatasetError(
            f'No Sentinel-2 scene under {max_cloud}% cloud between {start} and '
            f'{end} covers that area. Widen the dates or allow more cloud.'
        )

    features.sort(key=lambda f: (f.get('properties') or {}).get('eo:cloud_cover', 100))
    item = features[0]
    properties = item.get('properties') or {}
    taken = (properties.get('datetime') or '')[:10]
    cloud = properties.get('eo:cloud_cover')

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f'sentinel2_{taken}_{bands}.tif')

    # Each band is its own asset, so they are read separately and stacked.
    # Sentinel-2 mixes resolutions -- B04 and B08 are 10 m, B05 is 20 m -- so
    # the first band sets the grid and the rest are resampled onto it.
    # Otherwise index_bands, the preset the Vegetation Index tool wants, could
    # never be stacked at all.
    layers, profile, target = [], None, None
    for band in wanted:
        asset = item['assets'].get(band)
        if asset is None:
            raise DatasetError(f'Scene has no band {band}.')
        signed = pc_sign(asset['href'], SENTINEL_COLLECTION)
        single = os.path.join(output_dir, f'.{band}.tif')
        _clip_cog(signed, bbox, single, indexes=[1], out_shape=target)
        with rasterio.open(single) as source:
            layers.append(source.read(1))
            if profile is None:
                profile = source.profile.copy()
                target = (source.height, source.width)
        os.remove(single)

    profile.update(count=len(layers), compress='lzw', driver='GTiff')
    with rasterio.open(path, 'w', **profile) as destination:
        for index, layer in enumerate(layers, start=1):
            destination.write(layer, index)
            destination.set_band_description(index, wanted[index - 1])

    cloud_text = f'{cloud:.1f}% cloud' if cloud is not None else 'cloud unknown'
    return True, (
        f'Sentinel-2 scene from {taken} ({cloud_text}), bands '
        f'{", ".join(wanted)} at {profile["width"]}x{profile["height"]} px.'
    ), {'sentinel2_tif': path}


# --- PLSS land grid ---------------------------------------------------------

PLSS_URL = (
    'https://gis.blm.gov/arcgis/rest/services/Cadastral/'
    'BLM_Natl_PLSS_CadNSDI/MapServer'
)
PLSS_LAYERS = {
    'sections': (2, 'Sections (one square mile)'),
    'townships': (1, 'Townships (six miles square)'),
}


def fetch_plss(aoi, output_dir, level='sections', **_):
    """
    The Public Land Survey System grid.

    US farmland is described in it -- "the northwest quarter of section 14" --
    so it is the grid leases, field names and most paperwork are written
    against.
    """
    import geopandas as gpd

    if level not in PLSS_LAYERS:
        raise DatasetError(
            f'Unknown level. Available: {", ".join(sorted(PLSS_LAYERS))}.'
        )
    layer_id, label = PLSS_LAYERS[level]

    bbox = aoi['bbox']
    check_bbox(bbox)

    query = urllib.parse.urlencode({
        'geometry': ','.join(f'{v:.6f}' for v in bbox),
        'geometryType': 'esriGeometryEnvelope',
        'inSR': 4326, 'outSR': 4326,
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': '*', 'f': 'geojson', 'resultRecordCount': 200,
    })

    with _open(f'{PLSS_URL}/{layer_id}/query?{query}') as response:
        payload = json.loads(response.read().decode('utf-8'))

    features = payload.get('features') or []
    if not features:
        raise DatasetError(
            f'No PLSS {level} cover that area. The survey grid covers the '
            'public-land states, which excludes most of the eastern seaboard '
            'and Texas.'
        )

    frame = gpd.GeoDataFrame.from_features(features, crs='EPSG:4326')

    os.makedirs(output_dir, exist_ok=True)
    base = f'plss_{level}'
    shp_path = os.path.join(output_dir, f'{base}.shp')
    dbf_safe_columns(frame).to_file(shp_path)

    return True, f'{label}: {len(frame)} polygon(s) over the area.', {
        'plss_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ]
    }



# --- US Drought Monitor -----------------------------------------------------

USDM_URL = 'https://droughtmonitor.unl.edu/data/shapefiles_m/'

# D0 to D4. The numbers are what the shapefile carries; the names are what
# anyone reading a map expects to see.
USDM_CLASSES = {
    0: 'D0 Abnormally Dry',
    1: 'D1 Moderate Drought',
    2: 'D2 Severe Drought',
    3: 'D3 Extreme Drought',
    4: 'D4 Exceptional Drought',
}


def fetch_drought(aoi, output_dir, week='current', **_):
    """
    US Drought Monitor classes over the area.

    Published weekly by the National Drought Mitigation Center at UNL. The
    whole country is five polygons, one per class, so it is fetched once and
    clipped rather than queried.
    """
    import io
    import zipfile

    import geopandas as gpd
    from shapely.geometry import box

    bbox = aoi['bbox']
    check_bbox(bbox)

    week = (week or 'current').strip()
    if week == 'current':
        name = 'USDM_current_M.zip'
    else:
        digits = week.replace('-', '')
        if not (len(digits) == 8 and digits.isdigit()):
            raise DatasetError(
                'Give the week as a date, YYYY-MM-DD, or leave it as current.'
            )
        # The Monitor is published for Tuesdays; any other date has no file.
        name = f'USDM_{digits}_M.zip'

    with _open(USDM_URL + name) as response:
        payload = response.read()

    if not payload[:2] == b'PK':
        raise DatasetError(
            f'No Drought Monitor release for {week}. It is published weekly '
            'for Tuesdays.'
        )

    os.makedirs(output_dir, exist_ok=True)
    extracted = os.path.join(output_dir, '.usdm')
    os.makedirs(extracted, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(extracted)
        inner = next(n for n in archive.namelist() if n.endswith('.shp'))

    released = os.path.splitext(os.path.basename(inner))[0].replace('USDM_', '')
    national = gpd.read_file(os.path.join(extracted, inner))

    area = gpd.GeoDataFrame(geometry=[box(*bbox)], crs='EPSG:4326')
    clipped = gpd.clip(national, area)
    clipped = clipped[~clipped.geometry.is_empty & ~clipped.geometry.isna()]

    shutil.rmtree(extracted, ignore_errors=True)

    if clipped.empty:
        # Not an error: no drought class covers the area, which is the answer.
        return True, (
            f'No drought is mapped over that area in the {released} release -- '
            'the US Drought Monitor shows nothing there, not even D0 '
            '(abnormally dry).'
        ), {}

    clipped['class'] = clipped['DM'].map(USDM_CLASSES)

    base = f'usdm_{released}'
    shp_path = os.path.join(output_dir, f'{base}.shp')
    dbf_safe_columns(clipped).to_file(shp_path)

    worst = USDM_CLASSES.get(int(clipped['DM'].max()), 'unknown')
    present = ', '.join(
        USDM_CLASSES[c] for c in sorted(clipped['DM'].unique().tolist())
    )
    return True, (
        f'US Drought Monitor, released {released}: {present} over the area. '
        f'Worst class present is {worst}.'
    ), {
        'drought_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ]
    }


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
        options=[
            {'name': 'year', 'label': 'Year', 'type': 'select',
             'choices': [str(y) for y in range(CDL_LAST_YEAR, CDL_FIRST_YEAR - 1, -1)],
             'default': str(CDL_LAST_YEAR)},
            {'name': 'mask', 'label': 'Hide', 'type': 'select',
             'choices': ['none', 'developed', 'non_agricultural'],
             'labels': CDL_MASK_LABELS, 'default': 'none'},
        ],
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
        'naip', 'USDA NAIP aerial imagery', 'naip', 'raster', 'bbox',
        'Sub-metre aerial photography of US farmland, flown by USDA every two '
        'or three years, with a near-infrared band. Close enough to see rows.',
        fetch_naip,
    ),
    PublicDataset(
        'sentinel2', 'Sentinel-2 imagery', 'sentinel2', 'raster', 'bbox',
        'Free 10 m satellite imagery, revisited every few days. The '
        'index_bands preset returns exactly the red, red-edge and '
        'near-infrared bands the Vegetation Index tool needs.',
        fetch_sentinel2,
        options=[
            {'name': 'bands', 'label': 'Bands', 'type': 'select',
             'choices': sorted(SENTINEL_PRESETS),
             'labels': {k: k.replace('_', ' ') for k in SENTINEL_PRESETS},
             'default': 'index_bands'},
            {'name': 'start', 'label': 'From', 'type': 'date'},
            {'name': 'end', 'label': 'To', 'type': 'date'},
            {'name': 'max_cloud', 'label': 'Max cloud %', 'type': 'select',
             'choices': ['5', '10', '20', '40', '80'], 'default': '20'},
        ],
    ),
    PublicDataset(
        'drought', 'US Drought Monitor', 'usdm', 'vector', 'bbox',
        'Weekly drought classes D0 to D4 from the National Drought Mitigation '
        'Center at UNL -- the map irrigation decisions and disaster '
        'designations are argued over.',
        fetch_drought,
        options=[{'name': 'week', 'label': 'Week', 'type': 'date',
                  'optional': True,
                  'hint': 'Leave blank for the latest release. Published for Tuesdays.'}],
    ),
    PublicDataset(
        'plss', 'PLSS land grid', 'plss', 'vector', 'bbox',
        'The Public Land Survey System: the sections and townships US '
        'farmland is described in, and that leases and field names are '
        'written against.',
        fetch_plss,
        options=[{'name': 'level', 'label': 'Level', 'type': 'select',
                  'choices': sorted(PLSS_LAYERS),
                  'labels': {k: v[1] for k, v in PLSS_LAYERS.items()},
                  'default': 'sections'}],
    ),
    PublicDataset(
        'usgs_elevation', 'USGS 3DEP elevation', 'usgs_3dep', 'raster', 'bbox',
        'Bare-earth elevation for the United States. Feeds the Terrain tool '
        'for slope, aspect and where water collects.',
        fetch_elevation,
        options=[{'name': 'resolution', 'label': 'Grid size (px)', 'type': 'select',
                  'choices': ['256', '512', '1024', '2048'], 'default': '512'}],
    ),
    PublicDataset(
        'usgs_watersheds', 'USGS watershed boundaries', 'usgs_wbd', 'vector', 'bbox',
        'The nested watersheds a field drains into, from the USGS Watershed '
        'Boundary Dataset -- the unit most nutrient-loss rules are written in.',
        fetch_watersheds,
        options=[{'name': 'level', 'label': 'Level', 'type': 'select',
                  'choices': sorted(WATERSHED_LAYERS),
                  'labels': {k: v[1] for k, v in WATERSHED_LAYERS.items()},
                  'default': 'huc12'}],
    ),
    PublicDataset(
        'openstreetmap', 'OpenStreetMap features', 'openstreetmap', 'vector', 'bbox',
        'Roads, waterways, buildings and land use around a field, for context '
        'on a map or for measuring distance to a road or stream.',
        fetch_osm,
        options=[{'name': 'feature', 'label': 'Features', 'type': 'select',
                  'choices': sorted(OSM_FEATURES),
                  'default': 'roads'}],
    ),
    PublicDataset(
        'ssurgo', 'USDA SSURGO soil survey', 'ssurgo', 'vector', 'bbox',
        'The surveyed soil map of the United States, at field scale, naming '
        'the soil series local agronomy is written about. Where it covers, it '
        'beats the 250 m global SoilGrids model.',
        fetch_ssurgo,
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
