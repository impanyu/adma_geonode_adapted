"""
Valid Yield Extractor/ Yield Data Cleaning Tool
------------------------------------------------
- Loads treatment plots (polygons), application points, and harvest points.
- Builds Valid Application Area (VAA) using applied vs target rate ± tolerance.
- Keeps only harvest inside VAA (VHA) and creates coarse harvest "strips".
- Labels strips by min length rules and summarizes by plot.


Tool outputs the yield data points where valid application has done
 purpose: filtering yield/harvest points to only where valid application occurred.
"""

from __future__ import annotations
from pathlib import Path
import argparse
import sys
import warnings

import pandas as pd
import geopandas as gpd
from shapely.ops import unary_union

#  Config 

DEFAULT_CRS_METERS = "EPSG:26914"
RATE_TOL = 0.10
MIN_RADIUS_M = 0.5
MAX_HDIST_M = 10.0
MIN_STRIP_LEN_M = 30.0
MIN_TOTAL_LEN_M = 100.0

PLOTS_MAP = {
    "plot_id": ["plot_id", "plotid", "treatment_id", "treat_id", "name", "plot", "id"]
}

APP_MAP = {
    "applied":  ["applied", "appliedrat", "as_applied", "rate_applied", "app_rate", "actualrate", "actual_rate"],
    "rxTarget": ["rxtarget", "targetrate", "target_rate", "prescribedrate", "prescribed_rate", "rx_rate"],
    "swath":    ["swathwidth", "swath", "boomwidth", "width"],
    "time":     ["isotime", "time", "timestamp", "utc", "datetime"],
}

HARV_MAP = {
    "clnYield": ["clnyield", "yield", "yld", "clean_yield", "yld_buac", "Yield"],
    "moisture": ["moisture", "Moist_", "Moisture", "grain_moisture", "moist_", "moist"]
}


#  Helpers 

def _normalize_cols(df: pd.DataFrame) -> dict:
    remap = {}
    for c in df.columns:
        key = "".join(str(c).lower().split()).replace("_", "")
        remap[key] = c
    return remap

def _pick(df: pd.DataFrame, candidates: list[str], required: bool = True) -> str | None:
    remap = _normalize_cols(df)
    for cand in candidates:
        k = "".join(cand.lower().split()).replace("_", "")
        if k in remap:
            return remap[k]
    if required:
        raise KeyError(f"Could not find any of: {candidates} in columns: {list(df.columns)}")
    return None

def _to_crs(gdf: gpd.GeoDataFrame, crs_out: str | None) -> gpd.GeoDataFrame:
    if crs_out:
        try:
            return gdf.to_crs(crs_out)
        except Exception as e:
            warnings.warn(f"CRS transform failed ({e}). Returning original CRS.")
    return gdf

def load_vector(path: Path) -> gpd.GeoDataFrame:
    return gpd.read_file(path)

def load_csv_as_gdf(path: Path, *, easting, northing, lat, lon, wkt, crs_in) -> gpd.GeoDataFrame:
    df = pd.read_csv(path)
    if wkt:
        gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df[wkt]), crs=crs_in)
    elif easting and northing:
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[easting], df[northing]), crs=crs_in)
    elif lat and lon:
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs=crs_in)
    else:
        raise ValueError("CSV requires (easting,northing) or (lat,lon) or WKT.")
    return gdf

def load_any(path: str | Path, crs_out: str | None = None, **csv_opts) -> gpd.GeoDataFrame:
    p = Path(path)
    if p.suffix.lower() in {".csv", ".txt"}:
        crs_in = csv_opts.get("crs_in") or "EPSG:4326"
        gdf = load_csv_as_gdf(p, easting=csv_opts.get("easting"), northing=csv_opts.get("northing"),
                              lat=csv_opts.get("lat"), lon=csv_opts.get("lon"),
                              wkt=csv_opts.get("wkt"), crs_in=crs_in)
    else:
        gdf = load_vector(p)
    return _to_crs(gdf, crs_out)

def standardize_columns(gdf: gpd.GeoDataFrame, mapping: dict) -> gpd.GeoDataFrame:
    out = gdf.copy()
    for std_key, synonyms in mapping.items():
        col = _pick(out, synonyms, required=False)
        if col and col != std_key:
            out[std_key] = out[col]
    return out

def pick_column(df, candidates):
    lower = {c.lower(): c for c in df.columns}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    raise ValueError(f"None of the expected columns found. Looked for: {candidates}")


#  Geoprocessing 

def join_points_to_plots(points: gpd.GeoDataFrame, plots: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    joined = gpd.sjoin(points, plots[["plot_id", "geometry"]], how="left", predicate="within")
    for col in joined.columns:
        if col.startswith("index_"):
            joined = joined.drop(columns=[col])
    return joined

def infer_swath_m(app: gpd.GeoDataFrame) -> pd.Series:
    if "swath" in app.columns:
        s = pd.to_numeric(app["swath"], errors="coerce")
        s_m = s.where(~((s >= 8) & (s <= 60)), s * 0.3048)
        return s_m.fillna(6.0)
    return pd.Series(6.0, index=app.index)

def classify_vap(app_joined: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    j = app_joined.copy()
    for k in ("applied", "rxTarget"):
        if k not in j.columns:
            raise KeyError(f"Required column '{k}' is missing after standardization.")
        j[k] = pd.to_numeric(j[k], errors="coerce")
    j["is_vap"] = (j["applied"] >= (1 - RATE_TOL) * j["rxTarget"]) & \
                  (j["applied"] <= (1 + RATE_TOL) * j["rxTarget"])
    return j

def compute_vaa(app_joined: gpd.GeoDataFrame, plots: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    swath_m = infer_swath_m(app_joined)
    app = app_joined.copy()
    app["_rad"] = (swath_m / 2.0).clip(lower=MIN_RADIUS_M)
    app = app[app["plot_id"].notna()]
    plot_index = plots.set_index("plot_id")
    rows = []
    print(f"  • computing VAA per plot (n={app['plot_id'].nunique()})...")
    for pid, pts in app.groupby("plot_id"):
        if pid not in plot_index.index:
            continue
        gplot = plot_index.loc[pid, "geometry"]
        vpts = pts[pts["is_vap"]]
        ipts = pts[~pts["is_vap"]]
        if vpts.empty:
            continue
        v_union = unary_union([g.buffer(r) for g, r in zip(vpts.geometry, vpts["_rad"])])
        if not ipts.empty:
            i_union = unary_union([g.buffer(r) for g, r in zip(ipts.geometry, ipts["_rad"])])
            geom_final = v_union.difference(i_union)
        else:
            geom_final = v_union
        geom_final = geom_final.intersection(gplot)
        if not geom_final.is_empty:
            rows.append({"plot_id": pid, "geometry": geom_final})
    vaa = gpd.GeoDataFrame(rows, crs=plots.crs)
    if not vaa.empty:
        vaa["vaa_area_m2"] = vaa.geometry.area
    return vaa

def compute_vha(harv_pts: gpd.GeoDataFrame, vaa: gpd.GeoDataFrame, outdir: Path) -> gpd.GeoDataFrame:
    print("🟣 Computing Harvest Area (HA) and Valid Harvest Area (VHA)...")
    hp = harv_pts.copy()
    hp["geometry"] = hp.geometry.buffer(1.5)
    ha = hp.dissolve(by="plot_id").reset_index()[["plot_id", "geometry"]]
    ha["ha_area_m2"] = ha.geometry.area
    ha_path = outdir / "harvest_area.shp"
    ha.to_file(ha_path, driver="ESRI Shapefile")
    print(f"   • Saved Harvest Area (HA) → {ha_path}")
    vha = gpd.overlay(ha, vaa[["plot_id", "geometry"]], how="intersection")
    vha["vha_area_m2"] = vha.geometry.area
    print("   • Valid Harvest Area (VHA) computed successfully.")
    return vha

def make_harvest_strips(harv_pts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    g = harv_pts.copy()
    g["geometry"] = g.geometry.buffer(MAX_HDIST_M)
    strips = g.dissolve(by="plot_id").reset_index()
    strips["strip_id"] = range(len(strips))
    return strips[["plot_id", "strip_id", "geometry"]]

def label_strips(strips: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    s = strips.copy()
    s["len_m"] = s.geometry.length
    s["is_individual_valid"] = s["len_m"] >= MIN_STRIP_LEN_M
    agg = s[s["is_individual_valid"]].groupby("plot_id")["len_m"].sum().rename("sum_valid_len_m")
    s = s.merge(agg, on="plot_id", how="left")
    s["sum_valid_len_m"] = s["sum_valid_len_m"].fillna(0.0)
    s["meets_total"] = s["sum_valid_len_m"] >= MIN_TOTAL_LEN_M
    s["label"] = s.apply(lambda r: "VHS" if (r["is_individual_valid"] and r["meets_total"]) else "NVHS", axis=1)
    return s


#  Interactive input prompt 

def prompt_args() -> argparse.Namespace:
    
    print("=" * 60)
    print("  Yield Data Cleaning Tool — Interactive Mode")
    print("  (Tip: drag and drop files into terminal to paste path)")
    print("=" * 60)

    def ask(prompt, default=None):
        suffix = f" [{default}]" if default else ""
        while True:
            val = input(f"\n{prompt}{suffix}: ").strip()
            if val:
                return val
            if default:
                return default
            print("  ⚠  Required — please enter a value.")

    args = argparse.Namespace()

    print("\n Required files ")
    args.plots = ask("Treatment plots shapefile (.shp)")
    args.app   = ask("As-applied data file (.shp or .csv)")
    args.harv  = ask("Harvest data file (.shp or .csv)")

    print("\n Optional settings ")
    args.crs = ask("Target CRS", default=DEFAULT_CRS_METERS)
    args.out  = ask("Output folder", default="out")

    # Set all CSV options to None by default
    for prefix in ("plots", "app", "harv"):
        for opt in ("wkt", "easting", "northing", "lat", "lon"):
            setattr(args, f"{prefix}_{opt}", None)
        setattr(args, f"{prefix}_crs_in", "EPSG:4326")

    # If any file is a CSV, ask for coordinate options
    for prefix, label, fpath in [("plots", "Plots", args.plots),
                                   ("app",   "Application", args.app),
                                   ("harv",  "Harvest", args.harv)]:
        if Path(fpath).suffix.lower() in {".csv", ".txt"}:
            print(f"\n── {label} CSV coordinate type ")
            print("  1) Easting / Northing")
            print("  2) Latitude / Longitude")
            print("  3) WKT column")
            choice = input("  Enter 1, 2, or 3 [1]: ").strip() or "1"
            if choice == "1":
                setattr(args, f"{prefix}_easting",  ask(f"  {label} Easting column name"))
                setattr(args, f"{prefix}_northing", ask(f"  {label} Northing column name"))
            elif choice == "2":
                setattr(args, f"{prefix}_lat", ask(f"  {label} Latitude column name"))
                setattr(args, f"{prefix}_lon", ask(f"  {label} Longitude column name"))
            else:
                setattr(args, f"{prefix}_wkt", ask(f"  {label} WKT column name"))
            setattr(args, f"{prefix}_crs_in", ask(f"  {label} source CRS", default="EPSG:4326"))

    print("\n" + "=" * 60)
    print("  Starting pipeline...")
    print("=" * 60 + "\n")
    return args


#  Core pipeline 

def run(args: argparse.Namespace) -> None:
    """Run the full data cleaning pipeline given a parsed args namespace."""

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(" Loading layers ...")

    # Load plots
    if Path(args.plots).suffix.lower() in {".csv", ".txt"}:
        plots = load_any(args.plots, crs_out=args.crs, wkt=args.plots_wkt,
                         easting=args.plots_easting, northing=args.plots_northing,
                         lat=args.plots_lat, lon=args.plots_lon, crs_in=args.plots_crs_in)
    else:
        plots = load_any(args.plots, crs_out=args.crs)
    plots = standardize_columns(plots, PLOTS_MAP)

    plot_id_col = pick_column(plots, ["Plot_Number", "Plot_Num", "Plot_Numbe", "Plot No",
                                       "PlotNo", "PlotID", "Plot_ID", "Plot", "plot_number"])
    print(f"Using plots ID column: {plot_id_col}")
    plots = plots.rename(columns={plot_id_col: "plot_id"})
    plots["plot_id"] = plots["plot_id"].astype(str).str.strip()
    plots = plots.dissolve(by="plot_id", as_index=False)
    print(f"Unique plots after dissolve: {plots['plot_id'].nunique()}")

    # Load application
    if Path(args.app).suffix.lower() in {".csv", ".txt"}:
        app = load_any(args.app, crs_out=args.crs, wkt=args.app_wkt,
                       easting=args.app_easting, northing=args.app_northing,
                       lat=args.app_lat, lon=args.app_lon, crs_in=args.app_crs_in)
    else:
        app = load_any(args.app, crs_out=args.crs)
    app = standardize_columns(app, APP_MAP)

    # Load harvest
    if Path(args.harv).suffix.lower() in {".csv", ".txt"}:
        harv = load_any(args.harv, crs_out=args.crs, wkt=args.harv_wkt,
                        easting=args.harv_easting, northing=args.harv_northing,
                        lat=args.harv_lat, lon=args.harv_lon, crs_in=args.harv_crs_in)
    else:
        harv = load_any(args.harv, crs_out=args.crs)
    harv = standardize_columns(harv, HARV_MAP)

    print(f"✔ Plots: {len(plots)} polygons | ✔ App pts: {len(app)} | ✔ Harvest pts: {len(harv)}")
    if plots.empty or app.empty or harv.empty:
        raise SystemExit("One or more layers are empty. Check file paths and contents.")

    # Phase I: VAA
    print(" Phase I: Valid Application Area (VAA)")
    app_joined = join_points_to_plots(app, plots)
    print(f"App pts with plot_id: {app_joined['plot_id'].notna().sum()} / {len(app_joined)}")
    app_joined = classify_vap(app_joined)
    vaa = compute_vaa(app_joined, plots)
    vaa = vaa.rename(columns={"vaa_area_m2": "vaa_area_m"})
    vaa_path = outdir / "valid_application_area.shp"
    vaa.to_file(vaa_path, driver="ESRI Shapefile")
    print(f"   • Saved VAA → {vaa_path}")

    # Phase II: VHA
    print(" Phase II: Valid Harvest Area (VHA)")
    harv_joined = join_points_to_plots(harv, plots)
    print(f"Harv pts with plot_id: {harv_joined['plot_id'].notna().sum()} / {len(harv_joined)}")
    vha = compute_vha(harv_joined, vaa, outdir=outdir)
    vha_path = outdir / "valid_harvest_area.shp"
    vha.to_file(vha_path, driver="ESRI Shapefile")
    print(f"   • Saved VHA → {vha_path}")

    # Save final cleaned yield points
    print(" Saving final cleaned yield points (inside VHA)...")

    def _ensure_vha_plot_id(vha_df):
        for cand in ("plot_id", "plot_id_1", "plot_id_2", "Plot_Number", "Plot_Num"):
            if cand in vha_df.columns:
                vha_df["plot_id"] = vha_df[cand].astype(str).str.strip()
                return vha_df
        raise KeyError("Could not find a plot id column in VHA.")

    vha = _ensure_vha_plot_id(vha)
    clean_yield = gpd.sjoin(harv_joined, vha[["plot_id", "geometry"]], how="inner", predicate="within")
    for c in list(clean_yield.columns):
        if c.startswith("index_"):
            clean_yield = clean_yield.drop(columns=c)
    if "geometry_right" in clean_yield.columns:
        clean_yield = clean_yield.drop(columns="geometry_right")
    if "geometry_left" in clean_yield.columns:
        clean_yield = clean_yield.rename(columns={"geometry_left": "geometry"})
    wanted_cols = [c for c in ["plot_id", "easting", "northing", "yield", "moisture"] if c in clean_yield.columns]
    clean_yield = clean_yield[wanted_cols + ["geometry"]]
    clean_yield_path = outdir / "final_clean_yield_points.shp"
    clean_yield.to_file(clean_yield_path, driver="ESRI Shapefile")
    print(f"   • Saved final cleaned yield points → {clean_yield_path}")

    # Phase III: Strips + labeling
    print(" Phase III: Harvest strips + labeling")
    strips = make_harvest_strips(harv_joined)
    strips_labeled = label_strips(strips)
    if "label" in strips_labeled.columns:
        counts = strips_labeled["label"].astype(str).str.upper().value_counts(dropna=False).to_dict()
        print(f"   • Strip labels: {counts}")
    strips_path = outdir / "harvest_strips_labeled.shp"
    strips_labeled.to_file(strips_path, driver="ESRI Shapefile")
    print(f"   • Saved labeled strips → {strips_path}")

    # Summary by plot
    print(" Summarizing by plot")
    summary = (
        strips_labeled[strips_labeled["label"] == "VHS"]
        .groupby("plot_id")
        .agg(total_vhs_len_m=("len_m", "sum"))
        .reset_index()
        .sort_values("plot_id")
    )
    csv_path = outdir / "summary_by_plot.csv"
    summary.to_csv(csv_path, index=False)
    print(f"   • Summary CSV → {csv_path}")
    print(" Pipeline complete.")

    # Visualization
    try:
        import matplotlib.pyplot as plt

        def pick_existing(base: str):
            for ext in (".shp", ".geojson"):
                p = outdir / f"{base}{ext}"
                if p.exists():
                    return p
            return None

        vaa_f    = pick_existing("valid_application_area")
        vha_f    = pick_existing("valid_harvest_area")
        strips_f = pick_existing("harvest_strips_labeled")

        if all([vaa_f, vha_f, strips_f]):
            print("🖼  Generating visualizations...")
            vaa_v    = gpd.read_file(vaa_f)
            vha_v    = gpd.read_file(vha_f)
            strips_v = gpd.read_file(strips_f)
            crs = vaa_v.crs
            if vha_v.crs != crs:    vha_v    = vha_v.to_crs(crs)
            if strips_v.crs != crs: strips_v = strips_v.to_crs(crs)

            plt.figure(figsize=(9, 7))
            vaa_v.boundary.plot(linewidth=1.0)
            vaa_v.plot(alpha=0.25)
            vha_v.plot(alpha=0.45)
            plt.title("Valid Application Area (VAA) + Valid Harvest Area (VHA)")
            plt.xlabel("Easting (m)"); plt.ylabel("Northing (m)")
            plt.tight_layout()
            plt.savefig(outdir / "viz_vaa_vha.png", dpi=200)
            plt.close()

            plt.figure(figsize=(9, 7))
            vaa_v.boundary.plot(linewidth=0.8)
            strips_v.boundary.plot(linewidth=0.8)
            if "label" in strips_v.columns:
                vhs = strips_v[strips_v["label"].astype(str).str.upper() == "VHS"]
                if not vhs.empty:
                    vhs.boundary.plot(linewidth=2.0)
            plt.title("Harvest Strips (bold = VHS)")
            plt.xlabel("Easting (m)"); plt.ylabel("Northing (m)")
            plt.tight_layout()
            plt.savefig(outdir / "viz_strips.png", dpi=200)
            plt.close()

            print(f"   • Saved viz_vaa_vha.png and viz_strips.png → {outdir}")
        else:
            print("   • Visualization skipped — output files not found.")

    except Exception as viz_err:
        print(f"   • Visualization failed: {viz_err}")


# Entry point 

def main(argv=None):
    # If no arguments given, fall back to interactive prompts
    if argv is None and len(sys.argv) == 1:
        args = prompt_args()
    else:
        p = argparse.ArgumentParser(description="Yield Data Cleaning Tool (flexible, case-friendly)")
        p.add_argument("--plots", required=True, help="Treatment plots file (polygon layer)")
        p.add_argument("--app",   required=True, help="As-applied point layer (vector or CSV)")
        p.add_argument("--harv",  required=True, help="Harvest point layer (vector or CSV)")
        p.add_argument("--crs",   default=DEFAULT_CRS_METERS, help="Target metric CRS (default: %(default)s)")
        p.add_argument("--out",   default="out", help="Output folder (default: %(default)s)")
        p.add_argument("--plots-wkt");  p.add_argument("--plots-easting"); p.add_argument("--plots-northing")
        p.add_argument("--plots-lat");  p.add_argument("--plots-lon");     p.add_argument("--plots-crs-in", default="EPSG:4326")
        p.add_argument("--app-wkt");    p.add_argument("--app-easting");   p.add_argument("--app-northing")
        p.add_argument("--app-lat");    p.add_argument("--app-lon");       p.add_argument("--app-crs-in",   default="EPSG:4326")
        p.add_argument("--harv-wkt");   p.add_argument("--harv-easting");  p.add_argument("--harv-northing")
        p.add_argument("--harv-lat");   p.add_argument("--harv-lon");      p.add_argument("--harv-crs-in",  default="EPSG:4326")
        args = p.parse_args(argv)

    run(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f" Error: {e}", file=sys.stderr)
        sys.exit(1)
