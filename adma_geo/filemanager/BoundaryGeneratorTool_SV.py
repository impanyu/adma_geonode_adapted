"""
Field boundary generator -- Sreeja Vinod's script, vendored.

Emailed 2026-09-24: "a new tool that automatically generates boundary
shapefiles and layer files from GPS point data". Kept as close to what she
sent as possible so her next revision drops in with the same two edits, which
are the only changes and are marked ADMA EDIT below:

  1. generate_boundary() takes an optional notes list. Her code reports
     what it did -- outliers dropped, a curve followed -- by printing, which
     on a worker goes only to the log. Collecting the same strings lets the
     tool show them to the person who pressed Run.
  2. load_points() checks a CSV's latitude/longitude columns really hold
     degrees. Her own test file has them swapped, which silently produced an
     empty boundary.
  3. estimate_utm_crs() measures the centroid in degrees, so an input already
     in a projected CRS picks a real UTM zone instead of a nonexistent one.
  4. process_boundary_generation() is added as the entry point ADMA calls:
     her main() minus the interactive prompts, which cannot work on a worker.
     main() itself is untouched and the script still runs from a shell.
"""
## Field Boundary Generator
# Generates a boundary polygon around yield or seeding point data, buffered by a user specified
# distance in feet. Positive buffer expands the boundary outward, negative
# buffer shrinks it inward. Handles both CSV files with latitude/longitude
# columns and existing point shapefiles##

 
import argparse
import json
import os
import sys
 
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point
 
FEET_TO_METERS = 0.3048
 
 
def parse_args():
    parser = argparse.ArgumentParser(description="Generate a buffered boundary polygon around point data.")
    parser.add_argument("-i", "--input", help="Path to input data (CSV or shapefile/GeoJSON/GPKG with points).")
    parser.add_argument("-b", "--buffer", type=float,
                         help="Buffer distance in feet. Positive = outward, negative = inward.")
    parser.add_argument("-o", "--output", help="Full output path for the boundary file. Overrides --output-dir.")
    parser.add_argument("--output-dir", help="Directory to save the boundary file in. Defaults to the input file's folder.")
    parser.add_argument("--concave", action="store_true",
                         help="Force a concave hull instead of letting the script auto-detect. "
                              "Buffer distance will not be perfectly uniform at concave (reflex) vertices.")
    parser.add_argument("--convex", action="store_true",
                         help="Force a plain convex hull and disable automatic curve detection. "
                              "Guarantees uniform buffer distance on every side, but straight-lines across "
                              "any real notch or curve (a pond, waterway, etc).")
    parser.add_argument("--concavity", type=float, default=0.3,
                         help="Concave hull ratio, 0 (very concave) to 1 (convex). Default 0.3.")
    parser.add_argument("--curve-depth-threshold", type=float, default=15.0,
                         help="Auto-detection sensitivity, in feet: the minimum physical depth a notch/curve "
                              "must have (how far a straight-line boundary would cut across it) before it's "
                              "treated as real and followed. Measured as an absolute distance, not a percentage "
                              "of field area, so it catches a real notch on a large field too, not just a small "
                              "one. Lower = more sensitive (catches smaller curves, but may react to GPS jitter "
                              "along straight rows). Default 15 ft.")
    parser.add_argument("--rectangle", action="store_true",
                         help="Fit a minimum rotated rectangle (4 corners) instead of a hull. "
                              "Best for straight, rectangular pass patterns, avoids extra corners from GPS jitter. "
                              "Not suitable for a field with a real curved or notched edge.")
    parser.add_argument("--outlier-threshold", type=float, default=6.0,
                         help="Sensitivity of automatic outlier removal (robust MAD multiplier). "
                              "Lower = more aggressive removal. Default 6.0.")
    parser.add_argument("--no-outlier-filter", action="store_true",
                         help="Disable automatic outlier removal entirely.")
    return parser.parse_args()
 
 # fn to expand or shrink field boundary
def prompt_input_path():
    while True:
        path = input("Path to input data (CSV or shapefile with points): ").strip().strip('"')
        if os.path.isfile(path):
            return path
        print(f"File not found: {path}")
 
 
def prompt_buffer_distance():
    while True:
        raw = input("Buffer distance in feet (positive = outward, negative = inward): ").strip()
        try:
            return float(raw)
        except ValueError:
            print("Enter a numeric value, e.g. 3 or -5.")
 
 
def prompt_output_dir(default_dir):
    raw = input(f"Folder to save the boundary file in [{default_dir}]: ").strip().strip('"')
    return raw if raw else default_dir
 
 
def default_output_path(input_path, output_dir=None):
    base = os.path.splitext(os.path.basename(input_path))[0]
    folder = output_dir if output_dir else os.path.dirname(os.path.abspath(input_path))
    return os.path.join(folder, f"{base}_boundary.shp")
 
 
def guess_lat_lon_columns(columns):
    lat_candidates = ["lat", "latitude", "y", "northing"]
    lon_candidates = ["lon", "long", "longitude", "x", "easting"]
 
    cols_lower = {c.lower(): c for c in columns}
 
    lat_col = next((cols_lower[c] for c in lat_candidates if c in cols_lower), None)
    lon_col = next((cols_lower[c] for c in lon_candidates if c in cols_lower), None)
    return lat_col, lon_col
 
 
def load_points(input_path):
    ext = os.path.splitext(input_path)[1].lower()
 
    if ext in (".shp", ".geojson", ".gpkg"):
        gdf = gpd.read_file(input_path)
        if gdf.crs is None:
            print("Input has no CRS defined, assuming EPSG:4326 (WGS84).")
            gdf.set_crs(epsg=4326, inplace=True)
        return gdf
 
    if ext == ".csv":
        df = pd.read_csv(input_path)
        lat_col, lon_col = guess_lat_lon_columns(df.columns)
 
        if lat_col is None or lon_col is None:
            print("Could not auto-detect latitude/longitude columns.")
            print(f"Available columns: {list(df.columns)}")
            lat_col = input("Enter the latitude column name: ").strip()
            lon_col = input("Enter the longitude column name: ").strip()
 
        df = df.dropna(subset=[lat_col, lon_col])

        # ADMA EDIT 3: check the columns really hold what they are named.
        #
        # Richters_Clean_Yield_23.csv in the test data has them the wrong way
        # round: its "Longitude" column holds 40.8 and its "Latitude" column
        # -97.37. Trusting the names builds points at latitude -97, which is
        # not a place; every one then projects to nonsense, the outlier filter
        # discards all 21,511 of them, and the boundary comes out empty.
        # Latitude cannot exceed 90, so a swap is unambiguous when one column
        # is out of range and the other is not -- correct it and say so rather
        # than return an empty field.
        lat_values = pd.to_numeric(df[lat_col], errors="coerce")
        lon_values = pd.to_numeric(df[lon_col], errors="coerce")

        lat_ok = lat_values.between(-90, 90).all()
        lon_ok = lon_values.between(-180, 180).all()

        if not lat_ok and lon_values.between(-90, 90).all() and lat_values.between(-180, 180).all():
            print(f"Columns {lat_col!r} and {lon_col!r} appear to be swapped "
                  f"(latitude cannot be outside -90..90) - reading them the other way round.")
            lat_col, lon_col = lon_col, lat_col
            lat_values, lon_values = lon_values, lat_values
        elif not (lat_ok and lon_ok):
            raise ValueError(
                f"Columns {lat_col!r}/{lon_col!r} are not latitude/longitude in degrees. "
                "If they hold projected coordinates such as UTM metres, convert them "
                "first or supply the file as a shapefile with its CRS."
            )

        geometry = [Point(xy) for xy in zip(lon_values, lat_values)]
        gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
        return gdf
 
    raise ValueError(f"Unsupported input file type: {ext}")
 
 
def estimate_utm_crs(gdf):
    # Use the data's centroid to pick an appropriate UTM zone for
    # accurate metric buffering, then reproject back to WGS84 at the end.
    #
    # ADMA EDIT 4: measure the centroid in degrees. The zone arithmetic below
    # only means anything for lat/lon, and an input already in a projected CRS
    # -- Richters_clean_yield_23.shp in the test data is UTM 14N -- gives a
    # centre of 637535, a "zone" of 106286 and EPSG:138886, which does not
    # exist. Yield exports are often delivered projected, so this is not rare.
    geographic = gdf.to_crs("EPSG:4326") if gdf.crs is not None and gdf.crs.is_projected else gdf
    minx, miny, maxx, maxy = geographic.total_bounds
    center_lon = (minx + maxx) / 2
    center_lat = (miny + maxy) / 2
 
    zone = int((center_lon + 180) / 6) + 1
    hemisphere = 326 if center_lat >= 0 else 327  # EPSG prefix: 326=N, 327=S
    epsg_code = hemisphere * 100 + zone
    return f"EPSG:{epsg_code}"
 
 
def remove_outlier_points(gdf_proj, threshold=6.0):
    """Drop points far from the main cluster (bad GPS fixes, sensor jumps).
    """
    coords = np.array([(geom.x, geom.y) for geom in gdf_proj.geometry])
    median_point = np.median(coords, axis=0)
    dists = np.sqrt(((coords - median_point) ** 2).sum(axis=1))
 
    med_dist = np.median(dists)
    mad = np.median(np.abs(dists - med_dist))
 
    if mad == 0:
         
        return gdf_proj, 0
 
    cutoff = med_dist + threshold * mad
    keep_mask = dists <= cutoff
    n_removed = int((~keep_mask).sum())
 
    return gdf_proj[keep_mask], n_removed
 
 

_AUTO_CONCAVITY_RATIO = 0.02
 
 
def detect_true_shape(all_points, curve_depth_threshold_m, concavity=_AUTO_CONCAVITY_RATIO):
    """Decide whether the field has a real curved/notched edge worth following.
    """
    convex = all_points.convex_hull
 
    try:
        concave = shapely.concave_hull(all_points, ratio=concavity)
    except AttributeError:
        # Shapely < 2.0: no concave_hull available at all, fall back silently.
        return convex, False, 0.0
 
    notch_depth = convex.hausdorff_distance(concave)
    if notch_depth > curve_depth_threshold_m:
        return concave, True, notch_depth
 
    return convex, False, notch_depth
 
 
def generate_boundary(gdf, buffer_distance_m, use_concave_hull=False, concavity=0.3, use_rectangle=False,
                       outlier_threshold=6.0, auto_detect_curves=True, curve_depth_threshold_m=4.57,
                       notes=None):  # ADMA EDIT 1: notes collects what is printed
    utm_crs = estimate_utm_crs(gdf)
    gdf_proj = gdf.to_crs(utm_crs)
 
    if outlier_threshold is not None:
        gdf_proj, n_removed = remove_outlier_points(gdf_proj, threshold=outlier_threshold)
        if n_removed:
            message = f"Removed {n_removed} outlier point(s) far from the main field cluster before generating the boundary."
            print(message)
            if notes is not None:          # ADMA EDIT 1
                notes.append(message)
 
    all_points = gdf_proj.union_all()
 
    if use_rectangle:
        boundary = shapely.oriented_envelope(all_points)
    elif use_concave_hull:
        try:
            boundary = shapely.concave_hull(all_points, ratio=concavity)
        except AttributeError:
            print("concave_hull requires shapely >= 2.0, using convex hull instead.")
            boundary = all_points.convex_hull
    elif auto_detect_curves:
        boundary, used_concave, notch_depth_m = detect_true_shape(all_points, curve_depth_threshold_m)
        if used_concave:
            notch_depth_ft = notch_depth_m / FEET_TO_METERS
            message = (f"Detected a curved or notched field edge (up to {notch_depth_ft:.1f} ft deep would be cut "
                       f"across by a straight-line boundary) - following the curve instead.")
            print(message)
            if notes is not None:          # ADMA EDIT 1
                notes.append(message)
    else:
        boundary = all_points.convex_hull
 
    join_style = "mitre" if use_rectangle else "round"
    buffered = boundary.buffer(buffer_distance_m, join_style=join_style)
 
    boundary_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[buffered], crs=utm_crs)
    boundary_gdf = boundary_gdf.to_crs(gdf.crs)
    return boundary_gdf
 
 
def write_arcgis_lyrx(shapefile_path, line_width=1.5):
    """Write an ArcGIS Pro .lyrx layer file: no fill, black outline only.

    """
    folder = os.path.dirname(os.path.abspath(shapefile_path))
    filename = os.path.basename(shapefile_path)
    dataset = os.path.splitext(filename)[0]
    lyrx_path = os.path.splitext(shapefile_path)[0] + ".lyrx"
 
    cim = {
        "type": "CIMLayerDocument",
        "version": "2.9.0",
        "build": 32739,
        "layers": [f"CIMPATH=Map/{dataset}.xml"],
        "layerDefinitions": [
            {
                "type": "CIMFeatureLayer",
                "name": dataset,
                "uRI": f"CIMPATH=Map/{dataset}.xml",
                "useSourceMetadata": True,
                "layerType": "Operational",
                "showLegends": True,
                "visibility": True,
                "displayCacheType": "Permanent",
                "maxDisplayCacheAge": 5,
                "showPopups": True,
                "serviceLayerID": -1,
                "refreshRate": -1,
                "refreshRateUnit": "esriTimeUnitsSeconds",
                "autoGenerateFeatureTemplates": True,
                "featureElevationExpression": "0",
                "featureTable": {
                    "type": "CIMFeatureTable",
                    "displayField": "id",
                    "editable": True,
                    "dataConnection": {
                        "type": "CIMStandardDataConnection",
                        "workspaceConnectionString": f"DATABASE={folder}",
                        "workspaceFactory": "Shapefile",
                        "dataset": dataset,
                        "datasetType": "esriDTFeatureClass",
                    },
                    "studyAreaSpatialRel": "esriSpatialRelUndefined",
                    "searchOrder": "esriSearchOrderSpatial",
                },
                "htmlPopupEnabled": True,
                "selectable": True,
                "featureCacheType": "Session",
                "displayFilters": [],
                "featureBlendingMode": "Alpha",
                "renderer": {
                    "type": "CIMSimpleRenderer",
                    "symbol": {
                        "type": "CIMSymbolReference",
                        "symbol": {
                            "type": "CIMPolygonSymbol",
                            "symbolLayers": [
                                {
                                    "type": "CIMSolidStroke",
                                    "enable": True,
                                    "capStyle": "Round",
                                    "joinStyle": "Round",
                                    "lineStyle3D": "Strip",
                                    "miterLimit": 10,
                                    "width": line_width,
                                    "color": {"type": "CIMRGBColor", "values": [0, 0, 0, 100]},
                                }
                            ],
                            # No CIMSolidFill layer here on purpose: that's what
                            # leaves the polygon interior with no fill.
                            "angleAlignment": "Map",
                        },
                    },
                },
                "scaleSymbols": True,
                "snappable": True,
            }
        ],
    }
 
    with open(lyrx_path, "w") as f:
        json.dump(cim, f, indent=2)
    return lyrx_path
 
 
def write_outline_only_style(shapefile_path, line_color="0,0,0,255", line_width="0.6"):
    """Write a QGIS .qml style next to the shapefile: no fill, outline only."""
    qml_path = os.path.splitext(shapefile_path)[0] + ".qml"
    qml = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.0">
  <renderer-v2 type="singleSymbol">
    <symbols>
      <symbol type="fill" name="0">
        <layer class="SimpleFill">
          <prop k="color" v="0,0,0,0"/>
          <prop k="style" v="no"/>
          <prop k="outline_color" v="{line_color}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="{line_width}"/>
          <prop k="joinstyle" v="bevel"/>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
</qgis>
"""
    with open(qml_path, "w") as f:
        f.write(qml)
    return qml_path
 
 
def main():
    args = parse_args()
 
    input_path = args.input or prompt_input_path()
    gdf = load_points(input_path)
    print(f"Loaded {len(gdf)} points.")
 
    buffer_ft = args.buffer if args.buffer is not None else prompt_buffer_distance()
    buffer_m = buffer_ft * FEET_TO_METERS
 
    output_path = args.output
    if not output_path:
        output_dir = args.output_dir or prompt_output_dir(os.path.dirname(os.path.abspath(input_path)))
        output_path = default_output_path(input_path, output_dir)
 
    if args.concave:
        print("Using concave hull: buffer distance may not be perfectly uniform at concave (reflex) vertices.")
 
    boundary_gdf = generate_boundary(
        gdf,
        buffer_m,
        use_concave_hull=args.concave,
        concavity=args.concavity,
        use_rectangle=args.rectangle,
        outlier_threshold=None if args.no_outlier_filter else args.outlier_threshold,
        auto_detect_curves=not (args.concave or args.convex or args.rectangle),
        curve_depth_threshold_m=args.curve_depth_threshold * FEET_TO_METERS,
    )
 
    out_ext = os.path.splitext(output_path)[1].lower()
    driver = None
    if out_ext == ".geojson":
        driver = "GeoJSON"
    elif out_ext == ".gpkg":
        driver = "GPKG"
 
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    boundary_gdf.to_file(output_path, driver=driver)
    print(f"Boundary polygon saved to {output_path}")
 
    if out_ext == ".shp":
        qml_path = write_outline_only_style(output_path)
        lyrx_path = write_arcgis_lyrx(output_path)
        print(f"QGIS style saved to {qml_path}")
        print(f"ArcGIS Pro layer file saved to {lyrx_path}")
        print("In ArcGIS Pro, add the .lyrx file to the map (not the .shp directly) to get the no-fill style automatically.")
 
    print(f"Area: {boundary_gdf.to_crs(estimate_utm_crs(boundary_gdf)).area.iloc[0]:,.1f} m^2")
 
 
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
 

# --- ADMA EDIT 2 -----------------------------------------------------------

SHAPE_CHOICES = {
    'auto': 'Follow a real curve or notch, otherwise a straight-sided hull.',
    'convex': 'Always a straight-sided hull. Uniform buffer on every side, but '
              'cuts across a pond or waterway.',
    'concave': 'Always follow the point cloud closely. The buffer is not '
               'perfectly uniform at inward corners.',
    'rectangle': 'A single rotated rectangle. Best for straight rectangular '
                 'pass patterns, where it ignores GPS jitter.',
}


def process_boundary_generation(
    input_path,
    output_dir,
    buffer_ft=0.0,
    shape='auto',
    concavity=0.3,
    outlier_threshold=6.0,
    remove_outliers=True,
    curve_depth_threshold_ft=15.0,
):
    """
    Run the generator for ADMA and report what happened.

    This is main() without the input() prompts: a worker has no console to
    answer them. Returns (success, message, output_files) the way every
    other ADMA tool does.
    """
    if shape not in SHAPE_CHOICES:
        return False, f"Unknown shape {shape!r}. Choose one of: {', '.join(sorted(SHAPE_CHOICES))}.", {}

    if not os.path.exists(input_path):
        return False, f"Input file not found: {os.path.basename(input_path)}", {}

    try:
        gdf = load_points(input_path)
    except ValueError as exc:
        return False, str(exc), {}
    except Exception as exc:
        return False, f"Could not read the point file: {exc}", {}

    if gdf.empty:
        return False, "The input file contains no points.", {}

    notes = []
    os.makedirs(output_dir, exist_ok=True)
    output_path = default_output_path(input_path, output_dir)

    try:
        boundary_gdf = generate_boundary(
            gdf,
            float(buffer_ft) * FEET_TO_METERS,
            use_concave_hull=(shape == 'concave'),
            concavity=float(concavity),
            use_rectangle=(shape == 'rectangle'),
            outlier_threshold=float(outlier_threshold) if remove_outliers else None,
            auto_detect_curves=(shape == 'auto'),
            curve_depth_threshold_m=float(curve_depth_threshold_ft) * FEET_TO_METERS,
            notes=notes,
        )
    except Exception as exc:
        return False, f"Could not build the boundary: {exc}", {}

    boundary_gdf.to_file(output_path)

    base = os.path.splitext(output_path)[0]
    output_files = {
        'boundary_components': [
            base + ext for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
            if os.path.exists(base + ext)
        ],
        'qgis_style': write_outline_only_style(output_path),
        'arcgis_layer': write_arcgis_lyrx(output_path),
    }

    area_m2 = float(boundary_gdf.to_crs(estimate_utm_crs(boundary_gdf)).area.iloc[0])
    acres = area_m2 / 4046.8564224

    message = (
        f"Boundary from {len(gdf)} point(s): {area_m2:,.0f} m2 ({acres:,.1f} acres), "
        f"buffered {buffer_ft:g} ft, {shape} shape."
    )
    if notes:
        message += ' ' + ' '.join(notes)

    return True, message, output_files
