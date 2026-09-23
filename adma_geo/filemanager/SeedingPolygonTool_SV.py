"""Seeding polygon tool.

Authored by Sreeja Vinod (GIS Specialist, UNL); delivered 31 August 2026 as
seeding_polygon_tool_SV_latest(5).py and vendored here unchanged apart from
three marked edits: a non-interactive matplotlib backend, an optional
plot_path so the figure can be written to a file instead of a window, and
that path echoed back in the results dict. Keeping the rest byte-identical
means her next revision can be dropped in with the same three edits.

ADMA calls process_seeding_data() through filemanager.tasks; peek_columns()
backs the column pickers on the tool page.
"""
# libraries
import os
import sys
import warnings
import argparse
 
import geopandas as gpd
import pandas as pd
import numpy as np
import matplotlib
# ADMA runs this from a Celery worker with no display attached. Select the
# non-interactive backend before pyplot is imported, or importing this module
# fails outright on the server.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import MultiPolygon, Polygon, Point, LineString
from shapely.ops import unary_union
 
warnings.filterwarnings("ignore", category=UserWarning)
 
# fn reads the shapefile and return a list of all attribute column names except geometry
def peek_columns(seeding_path):
    try:
        gdf_head = gpd.read_file(seeding_path, rows=1)
    except Exception:
        gdf_head = gpd.read_file(seeding_path)
    return [c for c in gdf_head.columns if c != "geometry"]
 
 
# set up the input
def process_seeding_data(
    seeding_path,
    output_folder=None,
    show_plot=True,
    generate_boundary=True,
    boundary_buffer_ft=3.0,
    polygon_gap_ft=0.0,
    product_col=None,
    width_col=None,
    rate_col=None,
    max_gap_ft=None,
    plot_path=None,
):
 
    clip_to_boundary = generate_boundary
 
    # VALIDATE INPUT
 
    if not seeding_path.lower().endswith(".shp"):
        raise ValueError("Input must be a .shp file")
 
    if not os.path.isfile(seeding_path):
        raise FileNotFoundError(f"Shapefile not found: {seeding_path}")
 
    if output_folder is None:
        output_folder = os.path.dirname(seeding_path)
 
    os.makedirs(output_folder, exist_ok=True)
 
    # READ INPUT
 
    print("\nReading input shapefile...")
    gdf = gpd.read_file(seeding_path)
 
    gdf = gdf[~gdf.geometry.isna()].copy()
    gdf = gdf[gdf.geometry.is_valid].copy()
 
    if gdf.empty:
        raise ValueError("No valid geometries in input shapefile")
 
    if not np.all(gdf.geometry.geom_type == "Point"):
        raise ValueError("Input shapefile must contain Point geometries")
 
    print(f"Loaded {len(gdf)} valid points")
 
    # BUILD POLYGONS (ONE PER PRODUCT)
 
    # convert the data to a projected coordinate system for accurate distance calculations. 
 
    original_crs = gdf.crs
    crs_m = gdf.estimate_utm_crs()
    df = gdf.to_crs(crs_m).copy()
 
    #identify the required columns- product, applied rate and swath width
 
    def find_col(cols, pred, label):
        for c in cols:
            if pred(c):
                return c
        raise KeyError(f"Missing required column: {label}")
 
    def resolve_col(user_choice, cols, pred, label, required=True):
        """If the user explicitly picked a column (from the dropdown), use that column directly. Otherwise fall back
        to the original name-guessing heuristic."""
        if user_choice:
            match = next((c for c in cols if c.lower() == str(user_choice).lower()), None)
            if match is None:
                raise KeyError(
                    f"Column '{user_choice}' not found in shapefile (expected for {label})"
                )
            return match
        if required:
            return find_col(cols, pred, label)
        return next((c for c in cols if pred(c)), None)
 
    product_col = resolve_col(
        product_col, df.columns, lambda c: c.lower() in {"product", "product_name"}, "Product", required=True
    )
    width_col = resolve_col(
        width_col,
        df.columns,
        lambda c: any(k in c.lower() for k in ["swath", "width", "Swathwidth", "Swath", "SwathWidth", "Swath_Width", "Swath_width", "swath_width", "SWATHWIDTH", "SWATH" ]),
        "swath/width/boom",
        required=True,
    )
    rate_col = resolve_col(
        rate_col,
        df.columns,
        lambda c: c.lower() in {"appliedrate", "appliedrat", "rate", "Appliedrate", "AppliedRate", "Appiedrate", "APPLIEDRATE", "Applied_rate", "Appled_Rate", "App_Rate", "App_rate", "AppRate", "Apprate", "AppliRate", "Applirate", "Appli_rate", "Appli_Rate"},
        "Applied Rate",
        required=False,
    )
    # validate each point has a valid product name, poistive application rate and remove invalid records
 
    prod_col = product_col  
 
    df[prod_col] = df[prod_col].astype(str).str.strip()
    valid = df[prod_col].ne("").fillna(False) & df[prod_col].ne("nan")
 
    if rate_col:
        df[rate_col] = pd.to_numeric(df[rate_col], errors="coerce")
        rate_invalid = df[rate_col].le(0) | df[rate_col].isna()
        dropped_by_rate = rate_invalid.sum()
        if dropped_by_rate > 0:
            print(f"Points dropped (zero/null applied rate): {dropped_by_rate}")
        valid &= df[rate_col].gt(0).fillna(False)
 
    # Keep zero-rate points in projected CRS for boundary exclusion later
    zero_rate_pts = df.loc[~valid].copy()
 
    df = df.loc[valid].copy()
    if df.empty:
        raise ValueError("No valid applied points after filtering")
 
    print(f"Processing {len(df)} points across {df[prod_col].nunique()} products")
 
    df[width_col] = pd.to_numeric(df[width_col], errors="coerce")
    swath_med = float(df[width_col].median())
    use_feet = 6 <= swath_med <= 20
    print(f"Detected swath width median: {swath_med:.3f} ({'feet' if use_feet else 'meters'} assumed)")
 
    # automatically detects whether the swath width is in feet or meter and convert to meter if necassary
 
    # remaining application points are grouped by product, and each point is buffered by it's swath width
    # so that overlapping buffers merge into a single polygon representing the spatial coverage of that product. 
    df["swath_m"] = df[width_col] * (0.3048 if use_feet else 1.0)
 
    gap_m = max(polygon_gap_ft, 0.0) * 0.3048
    half_gap_m = gap_m / 2.0
 
    # Optional user override for max_gap_m (the maximum distance between
    # consecutive points that still counts as the same pass). Left as None,
    # each product keeps its own automatic default (3x median swath, floor
    # 15 m); if given, it is converted from feet and applied uniformly to
    # every product instead of the automatic per-product value.
    max_gap_m_override = max_gap_ft * 0.3048 if max_gap_ft is not None else None
 
    poly_rows = []
    
 
    for product, g in df.groupby(prod_col):
        print(f"  Processing product: {product} ({len(g)} points)")
 
        #Determine the maximum distance for connecting consecutive points
 
        g = g.sort_index()
        merge_dist = g["swath_m"].median() * 0.55
        if max_gap_m_override is not None:
            max_gap_m = max_gap_m_override
        else:
            max_gap_m = max(g["swath_m"].median() * 3.0, 15.0)
 
        try:
            xs = g.geometry.x.to_numpy()
            ys = g.geometry.y.to_numpy()
            sw = g["swath_m"].to_numpy()
 
            segment_polys = []
 
            if len(g) == 1:
                segment_polys.append(
                    Point(xs[0], ys[0]).buffer(sw[0] / 2.0 + 0.02, cap_style=3)
                )
            else:
                for i in range(len(g) - 1):
                    x1, y1, sw1 = xs[i], ys[i], sw[i]
                    x2, y2, sw2 = xs[i + 1], ys[i + 1], sw[i + 1]
                    seg_len = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5  #Check the distance between consecutive points
                    #If the distance exceeds max_gap_m, they are treated as separate application areas.
 
                    if seg_len > max_gap_m:
                        # Points too far apart to be the same pass, buffer separately
                        #Buffer isolated points individually
                        segment_polys.append(Point(x1, y1).buffer(sw1 / 2.0 + 0.02, cap_style=3))
                        segment_polys.append(Point(x2, y2).buffer(sw2 / 2.0 + 0.02, cap_style=3))
                    else:
                        seg_radius = ((sw1 + sw2) / 4.0) + 0.02 #Connect nearby points with buffered line segments
                        line = LineString([(x1, y1), (x2, y2)])
                        segment_polys.append(
                            line.buffer(seg_radius, cap_style=2, join_style=2)
                        )
 
            geom = unary_union(segment_polys)
            geom = geom.buffer(merge_dist, join_style=2).buffer(-merge_dist, join_style=2)
 
            if not geom.is_valid:
                geom = geom.buffer(0)
 
            if geom.geom_type == "MultiPolygon":
                num_parts = len(list(geom.geoms))
                print(f"    Found {num_parts} separate areas for this product")
 
                geom_merged = geom.buffer(merge_dist * 2.0, join_style=2).buffer(-merge_dist * 2.0, join_style=2)
 
                if geom_merged.geom_type == "Polygon":
                    geom = geom_merged
                    if len(geom.interiors) > 0:
                        geom = Polygon(geom.exterior)
                    print(f"    Merged into single polygon")
                else:
                    parts = []
                    for i, part in enumerate(geom.geoms):
                        if part.geom_type == "Polygon" and len(part.interiors) > 0:
                            part = Polygon(part.exterior)
 
                        if not part.is_valid:
                            part = part.buffer(0)
 
                        if not part.is_empty and part.area > 0:
                            parts.append(part)
                            print(f"      Area {i+1}: {part.area:.2f} m2")
 
                    if parts:
                        geom = MultiPolygon(parts)
                    else:
                        print(f"    WARNING: No valid parts for product {product}, skipping")
                        continue
            else:
                if geom.geom_type == "Polygon" and len(geom.interiors) > 0:
                    geom = Polygon(geom.exterior)
 
            if not geom.is_valid:
                geom = geom.buffer(0)
 
            if half_gap_m > 0:
                shrunk = geom.buffer(-half_gap_m, join_style=2)
                if not shrunk.is_valid:
                    shrunk = shrunk.buffer(0)
                if not shrunk.is_empty and shrunk.area > 0:
                    if shrunk.geom_type == "Polygon" and len(shrunk.interiors) > 0:
                        shrunk = Polygon(shrunk.exterior)
                    elif shrunk.geom_type == "MultiPolygon":
                        shrunk = MultiPolygon([
                            Polygon(p.exterior) if p.geom_type == "Polygon" and len(p.interiors) > 0 else p
                            for p in shrunk.geoms
                        ])
                    geom = shrunk
                else:
                    print(f"    NOTE: Gap buffer would erase product {product}'s polygon, skipping gap for this one")
 
            if geom.is_empty or geom.area == 0:
                print(f"    WARNING: Empty geometry for product {product}, skipping")
                continue
 
            total_area = geom.area if geom.geom_type == "Polygon" else sum(p.area for p in geom.geoms)
            poly_rows.append({prod_col: product, "geometry": geom})
            print(f"    Created polygon with total area: {total_area:.2f} m2")
 
        except Exception as e:
            print(f"    ERROR processing product {product}: {e}")
            continue
 
    if not poly_rows:
        raise ValueError("No valid polygons created")
 
    polys = gpd.GeoDataFrame(poly_rows, geometry="geometry", crs=crs_m)
 
    polys = polys[polys.geometry.is_valid].copy()
    polys = polys[~polys.geometry.is_empty].copy()
 
    if polys.empty:
        raise ValueError("No valid polygons after validation")
 
    # # OPTIONAL BOUNDARY + CLIP
 
    boundary = None
 
    if generate_boundary:
        pts = df.copy()
        buffer_m = boundary_buffer_ft * 0.3048
        swath_m_all = pts["swath_m"]
 
        # Buffer each applied point by half its own swath width
 
        coverage = gpd.GeoSeries(
            pts.geometry.buffer(swath_m_all / 2.0, cap_style=3), crs=crs_m
        )
 
        union_geom = coverage.union_all()
 
        # Use direct union, no bounding rectangle
        # Preserves true applied shape including any internal gaps
        boundary_geom = union_geom.buffer(buffer_m, join_style=2)
 
        # Explicitly cut out zero-rate point locations
 
        if not zero_rate_pts.empty:
            print(f"\nExcluding {len(zero_rate_pts)} zero-rate point locations from boundary...")
            zero_rate_pts[width_col] = pd.to_numeric(zero_rate_pts[width_col], errors="coerce")
            zero_rate_swath_m = zero_rate_pts[width_col] * (0.3048 if use_feet else 1.0)
 
            exclusion_geom = zero_rate_pts.geometry.buffer(
                zero_rate_swath_m / 2.0, cap_style=3
            ).union_all()
            boundary_geom = boundary_geom.difference(exclusion_geom)
            if not boundary_geom.is_valid:
                boundary_geom = boundary_geom.buffer(0)
            print(f"  Exclusion complete.")
 
        boundary = gpd.GeoDataFrame(geometry=[boundary_geom], crs=crs_m)
        # clip polygon to the field boundary
 
        if clip_to_boundary:
            print(f"\nClipping product polygons to boundary (+{boundary_buffer_ft} ft)...")
            polys = gpd.clip(polys, boundary)
            polys = polys[polys.geometry.is_valid & ~polys.geometry.is_empty].copy()
            if polys.empty:
                raise ValueError("No polygons remained after clipping to boundary")
 
        boundary = boundary.to_crs(original_crs)
 
    #Calculate polygon area 
    polys["area_m2"] = polys.geometry.area
    polys["hectares"] = polys["area_m2"] / 10_000.0
    polys["acres"] = polys["area_m2"] / 4046.8564224
 
    print("\nArea Summary:")
    for idx, row in polys.iterrows():
        print(f"  {row[prod_col]}: {row['hectares']:.2f} ha ({row['acres']:.2f} ac)")
 
    # MEAN VALUES PER PRODUCT
 
    num_cols = [
        c for c in df.columns
        if c not in {prod_col, "geometry", "swath_m"}
        and pd.api.types.is_numeric_dtype(df[c])
    ]
 
    if num_cols:
        means = (
            df.groupby(prod_col)[num_cols]
            .mean(numeric_only=True)
            .reset_index()
        )
        rename_dict = {c: f"{c}_mean" for c in num_cols}
        means = means.rename(columns=rename_dict)
        polys = polys.merge(means, on=prod_col, how="left")
 
    polys = polys.to_crs(original_crs)
 
    # SAVE OUTPUTS
 
    base_name = os.path.splitext(os.path.basename(seeding_path))[0]
    csv_path = os.path.join(output_folder, f"{base_name}_summary.csv")
 
    polys_path = os.path.join(output_folder, f"{base_name}_seeding_polys.shp")
 
    polys_save = polys.copy()
    col_mapping = {c: c[:10] for c in polys_save.columns if c != "geometry"}
    polys_save = polys_save.rename(columns=col_mapping)
    polys_save.to_file(polys_path)
 
    # CSV SUMMARY
 
    summary = polys[[prod_col, "area_m2", "hectares", "acres"]].copy()
    summary = summary.rename(columns={prod_col: "Product"})
    summary.to_csv(csv_path, index=False)
 
    # PLOT
 
    if show_plot:
        try:
            fig, ax = plt.subplots(figsize=(12, 10))
 
            polys.plot(ax=ax, column=prod_col, legend=True, alpha=0.6,
                       edgecolor='black', linewidth=1.5,
                       legend_kwds={'loc': 'upper left', 'bbox_to_anchor': (1, 1)})
 
            if boundary is not None:
                boundary.boundary.plot(ax=ax, color="red", linewidth=2, linestyle='--')
 
            ax.set_title("Seeding Polygons by Product", fontsize=14, fontweight='bold')
            ax.set_xlabel("Longitude")
            ax.set_ylabel("Latitude")
            ax.set_aspect('equal')
 
            plt.tight_layout()
            # ADMA passes plot_path to capture the figure for the run preview;
            # plt.show() would block forever with no display attached.
            if plot_path:
                fig.savefig(plot_path, dpi=110, bbox_inches="tight")
                plt.close(fig)
            else:
                plt.show()
 
        except Exception as e:
            print(f"\nWarning: Could not display plot - {str(e)}")
            print("Output files were still created successfully.")
 
    # RETURN RESULTS
 
    results = {
        'polygons_path': polys_path,
        'summary_path': csv_path,
        'swath_units': 'feet' if use_feet else 'meters',
        'products_processed': len(polys),
        'averaged_columns': num_cols if num_cols else [],
        'boundary_generated': generate_boundary,
        'clipped_to_boundary': clip_to_boundary,
        'product_col_used': prod_col,
        'width_col_used': width_col,
        'rate_col_used': rate_col,
        'max_gap_m_override_ft': max_gap_ft,
        'plot_path': plot_path if (show_plot and plot_path) else None,
    }
 
    print("\n" + "="*60)
    print("COMPLETED SUCCESSFULLY")
    print("="*60)
    print(f"Polygons : {polys_path}")
    print(f"Summary  : {csv_path}")
    print(f"\nColumns used -> Product: {prod_col} | Width: {width_col} | Rate: {rate_col}")
    print(f"Detected swath units: {results['swath_units']}")
    print(f"Products processed: {results['products_processed']}")
    print(f"Boundary generated: {generate_boundary}")
    print(f"Clipped to boundary: {clip_to_boundary}")
    if max_gap_ft is not None:
        print(f"Max pass-gap override: {max_gap_ft} ft (applied to all products)")
    else:
        print("Max pass-gap: automatic per-product default")
    if num_cols:
        print(f"Averaged columns: {', '.join(num_cols)}")
 
    return results
 
 
def pick_column(prompt, cols, optional=False):
    """Terminal equivalent of a dropdown: shows a numbered list of the
    shapefile's actual columns and lets the user pick by number (or type
    the exact name)."""
    suffix = " (press Enter to auto-detect instead)" if optional else ""
    while True:
        choice = input(f"{prompt}{suffix}: ").strip()
        if not choice and optional:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(cols):
            return cols[int(choice) - 1]
        match = next((c for c in cols if c.lower() == choice.lower()), None)
        if match:
            return match
        print("  Not a valid choice. Enter a number from the list above, or the exact column name.")
 
 
def main():
    parser = argparse.ArgumentParser(
        description='Process seeding point data and create polygon outputs.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
 
 
        """
    )
 
    parser.add_argument('-i', '--input', type=str, help='Full path to seeding POINT shapefile (.shp)')
    parser.add_argument('-o', '--output', type=str, default=None, help='Output folder path (default: same as input file)')
    parser.add_argument('--no-plot', action='store_true', help='Disable plot display')
    parser.add_argument('--no-boundary', action='store_true', help='Do not generate an applied-area boundary (used only for clipping, not saved)')
    parser.add_argument('--buffer-ft', type=float, default=3.0, help='Boundary outward buffer in feet (default: 3.0)')
    parser.add_argument('--gap-ft', type=float, default=0.0, help='Gap between adjacent product polygons in feet (default: 0.0, shared boundaries)')
    parser.add_argument('--product-col', type=str, default=None, help='Exact column name to use as Product (skips auto-detection)')
    parser.add_argument('--width-col', type=str, default=None, help='Exact column name to use as swath/implement width (skips auto-detection)')
    parser.add_argument('--rate-col', type=str, default=None, help='Exact column name to use as applied rate (skips auto-detection)')
    parser.add_argument('--list-columns', action='store_true', help='List column names found in the input shapefile, then exit (use with -i)')
    parser.add_argument('--max-gap-ft', type=float, default=None, help='Override the maximum pass-gap distance (feet) used to decide whether consecutive points belong to the same pass; default is computed automatically per product (3x median swath width, minimum 15 m)')
 
    args = parser.parse_args()
 
    if args.list_columns:
        if not args.input:
            print("ERROR: --list-columns requires -i/--input to also be given")
            sys.exit(1)
        if not os.path.isfile(args.input):
            print(f"ERROR: Shapefile not found: {args.input}")
            sys.exit(1)
        cols = peek_columns(args.input)
        print(f"Columns in {args.input}:")
        for c in cols:
            print(f"  {c}")
        sys.exit(0)
 
    if args.input is None:
        seeding_path = input("Enter FULL PATH to seeding POINT shapefile (.shp): ").strip()
 
        if not seeding_path.lower().endswith(".shp"):
            print("ERROR: Input must be a .shp file")
            sys.exit(1)
 
        if not os.path.isfile(seeding_path):
            print("ERROR: Shapefile not found")
            sys.exit(1)
 
        output_folder = input("Enter output folder (leave blank = same folder as input): ").strip()
        if not output_folder:
            output_folder = None
 
        # column name selection 
        cols = peek_columns(seeding_path)
        print("\nColumns found in the shapefile:")
        for i, c in enumerate(cols, 1):
            print(f"  {i}. {c}")
 
        product_col = pick_column("\nSelect the PRODUCT column (enter number)", cols, optional=False)
        width_col = pick_column("Select the SWATH/WIDTH column (enter number)", cols, optional=False)
        rate_col = pick_column("Select the APPLIED RATE column (enter number)", cols, optional=True)
 
        gen_boundary_in = input("\nGenerate applied-area boundary? (Y/n): ").strip().lower()
        generate_boundary = gen_boundary_in != "n"
 
        boundary_buffer_ft = 3.0
        if generate_boundary:
            buf_in = input("Boundary buffer in feet (default 3): ").strip()
            boundary_buffer_ft = float(buf_in) if buf_in else 3.0
 
        gap_in = input("Gap between adjacent product polygons in feet (default 0, shared boundaries): ").strip()
        polygon_gap_ft = float(gap_in) if gap_in else 0.0
 
        max_gap_in = input("Maximum pass-gap in feet (press Enter for automatic per-product default): ").strip()
        max_gap_ft = float(max_gap_in) if max_gap_in else None
 
        show_plot = True
    else:
        seeding_path = args.input
        output_folder = args.output
        show_plot = not args.no_plot
        generate_boundary = not args.no_boundary
        boundary_buffer_ft = args.buffer_ft
        polygon_gap_ft = args.gap_ft
        product_col = args.product_col
        width_col = args.width_col
        rate_col = args.rate_col
        max_gap_ft = args.max_gap_ft
 
    try:
        results = process_seeding_data(
            seeding_path=seeding_path,
            output_folder=output_folder,
            show_plot=show_plot,
            generate_boundary=generate_boundary,
            boundary_buffer_ft=boundary_buffer_ft,
            polygon_gap_ft=polygon_gap_ft,
            product_col=product_col,
            width_col=width_col,
            rate_col=rate_col,
            max_gap_ft=max_gap_ft,
        )
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        sys.exit(1)
 
 
if __name__ == "__main__":
    main()
 