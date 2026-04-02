import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import geometry_mask


# -------------------------------
# Helpers for Windows paths
# -------------------------------

def clean_path(p: str) -> str:
    p = (p or "").strip()

    # remove wrapping quotes "..."
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1].strip()

    # remove accidental single leading quote only
    if p.startswith('"') or p.startswith("'"):
        p = p[1:].strip()

    # normalize slashes (Windows accepts forward slashes)
    return p.replace("\\", "/")


def ask(prompt: str) -> str:
    return input(prompt).strip()


def fix_polygons(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf["geometry"] = gdf.geometry.buffer(0)  # fixes many invalid polygons
    return gdf


def ensure_same_crs(gdf: gpd.GeoDataFrame, target_crs) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError("A shapefile CRS is missing (crs=None). Please define CRS in GIS or re-export.")
    if gdf.crs != target_crs:
        return gdf.to_crs(target_crs)
    return gdf


def _ensure_points(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """If NDRE layer is polygons/lines, convert to representative points for stable joins."""
    geom_types = set(gdf.geom_type.unique())
    if not geom_types.issubset({"Point", "MultiPoint"}):
        gdf = gdf.copy()
        gdf["geometry"] = gdf.representative_point()
    return gdf


# -------------------------------
# STANDARD + UAV (your working method)
# -------------------------------

def calculate_si_standard_uav_and_update_csv(
    buffer_shp_path: str,
    ndre_shp_path: str,
    csv_path: str,
    field_column: str,
    si_column_name: str,
    ndre_col: str = "ndre",
    quantile_value: float = 0.95
) -> None:
    buffer_gdf = gpd.read_file(buffer_shp_path)
    ndre_gdf = gpd.read_file(ndre_shp_path)
    df_csv = pd.read_csv(csv_path)

    if field_column not in buffer_gdf.columns:
        raise ValueError(f"'{field_column}' not found in buffer shapefile. Columns: {list(buffer_gdf.columns)}")
    if field_column not in df_csv.columns:
        raise ValueError(f"'{field_column}' not found in CSV. Columns: {list(df_csv.columns)}")
    if ndre_col not in ndre_gdf.columns:
        raise ValueError(f"'{ndre_col}' not found in NDRE shapefile. Columns: {list(ndre_gdf.columns)}")

    if buffer_gdf.crs is None or ndre_gdf.crs is None:
        raise ValueError("CRS missing in buffer or NDRE shapefile.")
    if buffer_gdf.crs != ndre_gdf.crs:
        ndre_gdf = ndre_gdf.to_crs(buffer_gdf.crs)

    buffer_gdf = fix_polygons(buffer_gdf)
    ndre_gdf = ndre_gdf[ndre_gdf.geometry.notna()].copy()
    ndre_gdf = _ensure_points(ndre_gdf)

    joined = gpd.sjoin(
        ndre_gdf[[ndre_col, "geometry"]],
        buffer_gdf[[field_column, "geometry"]],
        predicate="within",
        how="inner"
    )
    if joined.empty:
        print("Buffer CRS:", buffer_gdf.crs)
        print("NDRE CRS:  ", ndre_gdf.crs)
        print("Buffer bounds:", buffer_gdf.total_bounds)
        print("NDRE bounds:  ", ndre_gdf.total_bounds)
        raise ValueError("No NDRE features matched any buffer polygon.")

    joined[ndre_col] = pd.to_numeric(joined[ndre_col], errors="coerce")
    joined = joined.dropna(subset=[ndre_col])
    if joined.empty:
        raise ValueError("All NDRE values became NA after cleaning.")

    p_ref = joined[ndre_col].quantile(quantile_value)
    if pd.isna(p_ref) or p_ref == 0:
        raise ValueError(f"Invalid reference quantile value: {p_ref}")

    stats = (
        joined.groupby(field_column, as_index=False)
              .agg(avg_ndre=(ndre_col, "mean"))
    )
    stats[si_column_name] = stats["avg_ndre"] / p_ref

    print("\n✅ Reference NDRE (p95):", p_ref)
    print("\n✅ SI preview (first 10 rows):")
    print(stats[[field_column, "avg_ndre", si_column_name]].head(10))

    out = df_csv.merge(stats[[field_column, si_column_name]], on=field_column, how="left")
    out.to_csv(csv_path, index=False)

    print(f"\n✅ Updated CSV saved (overwritten): {csv_path}")
    print(f"✅ Added column: {si_column_name}")


# -------------------------------
# SATELLITE helpers (NDRE raster + polygon mean)
# -------------------------------

def read_ndre_from_rasters(nir_tif: str, rededge_tif: str):
    with rasterio.open(nir_tif) as nir_src, rasterio.open(rededge_tif) as re_src:
        if nir_src.shape != re_src.shape:
            raise ValueError(f"Raster shapes differ: NIR={nir_src.shape}, RedEdge={re_src.shape}")
        if nir_src.transform != re_src.transform:
            raise ValueError("Raster transforms differ (not aligned).")
        if nir_src.crs != re_src.crs:
            raise ValueError("Raster CRS differ (not aligned).")

        nir = nir_src.read(1).astype("float32")
        re = re_src.read(1).astype("float32")

        if nir_src.nodata is not None:
            nir = np.where(nir == nir_src.nodata, np.nan, nir)
        if re_src.nodata is not None:
            re = np.where(re == re_src.nodata, np.nan, re)

        denom = nir + re
        ndre = np.where(np.isfinite(denom) & (denom != 0), (nir - re) / denom, np.nan)

        meta = {
            "crs": nir_src.crs,
            "transform": nir_src.transform,
            "height": nir_src.height,
            "width": nir_src.width,
        }
        return ndre, meta


def polygon_mean_from_raster(arr: np.ndarray, meta: dict, geom) -> float:
    mask = geometry_mask(
        [geom],
        out_shape=(meta["height"], meta["width"]),
        transform=meta["transform"],
        invert=True
    )
    vals = arr[mask]
    vals = vals[np.isfinite(vals)]
    return float(np.nanmean(vals)) if vals.size else np.nan


def _raster_values_within_union(arr: np.ndarray, meta: dict, union_geom) -> np.ndarray:
    mask = geometry_mask(
        [union_geom],
        out_shape=(meta["height"], meta["width"]),
        transform=meta["transform"],
        invert=True
    )
    vals = arr[mask]
    vals = vals[np.isfinite(vals)]
    return vals


# -------------------------------
# STANDARD + SATELLITE  (NEW)
# SI = mean NDRE (per plot) / p95 NDRE (all pixels within field/plots union)
# -------------------------------

def calculate_si_standard_satellite_and_update_csv(
    buffer_shp_path: str,
    nir_tif_path: str,
    rededge_tif_path: str,
    csv_path: str,
    field_column: str,
    si_column_name: str,
    quantile_value: float = 0.95
) -> None:
    buffer_gdf = gpd.read_file(buffer_shp_path)
    df_csv = pd.read_csv(csv_path)

    if field_column not in buffer_gdf.columns:
        raise ValueError(f"'{field_column}' not found in buffer shapefile. Columns: {list(buffer_gdf.columns)}")
    if field_column not in df_csv.columns:
        raise ValueError(f"'{field_column}' not found in CSV. Columns: {list(df_csv.columns)}")

    ndre, meta = read_ndre_from_rasters(nir_tif_path, rededge_tif_path)

    # Reproject polygons to raster CRS
    buffer_gdf = ensure_same_crs(buffer_gdf, meta["crs"])
    buffer_gdf = fix_polygons(buffer_gdf)

    # p95 across all NDRE pixels inside plot union
    union_geom = buffer_gdf.unary_union
    all_vals = _raster_values_within_union(ndre, meta, union_geom)
    if all_vals.size == 0:
        raise ValueError("No NDRE pixels found inside buffer/plot union area.")

    p_ref = float(np.nanpercentile(all_vals, quantile_value * 100))
    if not np.isfinite(p_ref) or p_ref == 0:
        raise ValueError(f"Invalid reference p95 value: {p_ref}")

    # Mean NDRE per plot polygon
    means = [polygon_mean_from_raster(ndre, meta, geom) for geom in buffer_gdf.geometry]
    stats = pd.DataFrame({
        field_column: buffer_gdf[field_column].values,
        "avg_ndre": means
    })
    stats[si_column_name] = stats["avg_ndre"] / p_ref

    print("\n✅ STANDARD + SATELLITE reference NDRE (p95):", p_ref)
    print("\n✅ SI preview (first 10 rows):")
    print(stats[[field_column, "avg_ndre", si_column_name]].head(10))

    out = df_csv.merge(stats[[field_column, si_column_name]], on=field_column, how="left")
    out.to_csv(csv_path, index=False)

    print(f"\n✅ Updated CSV saved (overwritten): {csv_path}")
    print(f"✅ Added column: {si_column_name}")


# -------------------------------
# SBF helpers: section_nu assignment (used by your SBF logic)
# -------------------------------

def add_section_numbers_in_groups(gdf: gpd.GeoDataFrame,
                                  section_col: str = "section_nu",
                                  group_size: int = 4,
                                  num_sectors: int = 6) -> gpd.GeoDataFrame:
    """
    Assign section_nu in groups of 4 IB polygons for sectors 1..6.
    """
    gdf = gdf.copy()
    gdf[section_col] = np.nan

    need = group_size * num_sectors
    if len(gdf) < need:
        raise ValueError(
            f"IB polygons={len(gdf)} but need at least {need} (group_size={group_size}, sectors={num_sectors})."
        )

    for i in range(1, num_sectors + 1):
        start = (i - 1) * group_size
        end = i * group_size
        gdf.iloc[start:end, gdf.columns.get_loc(section_col)] = i

    gdf[section_col] = gdf[section_col].astype(int)
    return gdf


# -------------------------------
# SBF + UAV  (NEW)
# Matches SBF logic but uses vector NDRE shapefile instead of rasters:
#   1) mean NDRE per IB polygon
#   2) average Reference IB mean by section_nu
#   3) mean NDRE per buffer polygon
#   4) SI = NDRE_Field / average_NDRE_IB
#   5) merge into CSV by section_nu
# -------------------------------

def calculate_si_sbf_uav_and_update_csv(
    buffer_shp_path: str,
    indicator_shp_path: str,
    ndre_shp_path: str,
    csv_path: str,
    si_column_name: str,
    ndre_col: str = "ndre",
    csv_join_col: str = "section_nu",
    ib_type_col: str = "type",
    section_col: str = "section_nu",
    group_size: int = 4,
    num_sectors: int = 6
) -> None:
    buffer_gdf = gpd.read_file(buffer_shp_path)
    ib_gdf = gpd.read_file(indicator_shp_path)
    ndre_gdf = gpd.read_file(ndre_shp_path)
    df_csv = pd.read_csv(csv_path)

    if csv_join_col not in df_csv.columns:
        raise ValueError(f"CSV must contain '{csv_join_col}'. Columns: {list(df_csv.columns)}")

    if ndre_col not in ndre_gdf.columns:
        raise ValueError(f"'{ndre_col}' not found in NDRE shapefile. Columns: {list(ndre_gdf.columns)}")

    # Align CRS: make NDRE and IB CRS match buffer CRS
    if buffer_gdf.crs is None or ib_gdf.crs is None or ndre_gdf.crs is None:
        raise ValueError("CRS missing in one of: buffer, indicator, or NDRE shapefile.")

    if ib_gdf.crs != buffer_gdf.crs:
        ib_gdf = ib_gdf.to_crs(buffer_gdf.crs)
    if ndre_gdf.crs != buffer_gdf.crs:
        ndre_gdf = ndre_gdf.to_crs(buffer_gdf.crs)

    buffer_gdf = fix_polygons(buffer_gdf)
    ib_gdf = fix_polygons(ib_gdf)
    ndre_gdf = ndre_gdf[ndre_gdf.geometry.notna()].copy()
    ndre_gdf = _ensure_points(ndre_gdf)

    # Required columns
    if section_col not in buffer_gdf.columns:
        raise ValueError(f"Buffer shapefile must have '{section_col}'. Columns: {list(buffer_gdf.columns)}")
    if ib_type_col not in ib_gdf.columns:
        raise ValueError(f"Indicator shapefile must have '{ib_type_col}'. Columns: {list(ib_gdf.columns)}")

    # If IB doesn't have section_nu, create it
    if section_col not in ib_gdf.columns:
        ib_gdf = add_section_numbers_in_groups(
            ib_gdf, section_col=section_col, group_size=group_size, num_sectors=num_sectors
        )

    # ---- 1) NDRE_IB per IB polygon (mean inside each IB polygon) ----
    ib_gdf = ib_gdf.reset_index(drop=True).copy()
    ib_gdf["ib_idx"] = ib_gdf.index

    j_ib = gpd.sjoin(
        ndre_gdf[[ndre_col, "geometry"]],
        ib_gdf[["ib_idx", section_col, ib_type_col, "geometry"]],
        predicate="within",
        how="inner"
    )
    if j_ib.empty:
        raise ValueError("No NDRE features matched Indicator Block polygons.")

    j_ib[ndre_col] = pd.to_numeric(j_ib[ndre_col], errors="coerce")
    j_ib = j_ib.dropna(subset=[ndre_col])

    ib_means = (
        j_ib.groupby("ib_idx", as_index=False)
            .agg(NDRE_IB=(ndre_col, "mean"))
    )
    ib_gdf = ib_gdf.merge(ib_means, on="ib_idx", how="left")

    # ---- 2) sector-wise avg NDRE for Reference only (equal-weight by IB polygon) ----
    ref = ib_gdf[ib_gdf[ib_type_col].astype(str).str.lower() == "reference"].copy()
    if ref.empty:
        raise ValueError("No 'Reference' rows found in indicator shapefile 'type' column.")

    ref_stats = (
        ref.groupby(section_col, as_index=False)
           .agg(average_NDRE_IB=("NDRE_IB", "mean"))
    )

    # ---- 3) NDRE_Field per buffer polygon ----
    buffer_gdf = buffer_gdf.reset_index(drop=True).copy()
    buffer_gdf["buf_idx"] = buffer_gdf.index

    j_buf = gpd.sjoin(
        ndre_gdf[[ndre_col, "geometry"]],
        buffer_gdf[["buf_idx", section_col, "geometry"]],
        predicate="within",
        how="inner"
    )
    if j_buf.empty:
        raise ValueError("No NDRE features matched Buffer polygons.")

    j_buf[ndre_col] = pd.to_numeric(j_buf[ndre_col], errors="coerce")
    j_buf = j_buf.dropna(subset=[ndre_col])

    buf_means = (
        j_buf.groupby("buf_idx", as_index=False)
             .agg(NDRE_Field=(ndre_col, "mean"))
    )
    buffer_gdf = buffer_gdf.merge(buf_means, on="buf_idx", how="left")

    # ---- 4) SI = NDRE_Field / average_NDRE_IB (join by section_nu) ----
    merged = buffer_gdf.drop(columns="geometry").merge(ref_stats, on=section_col, how="left")
    merged[si_column_name] = merged["NDRE_Field"] / merged["average_NDRE_IB"]

    print("\n✅ SBF + UAV preview (first 10 rows):")
    print(merged[[section_col, "NDRE_Field", "average_NDRE_IB", si_column_name]].head(10))

    # ---- 5) Merge SI into CSV by section_nu ----
    si_out = merged[[section_col, si_column_name]].copy()
    si_out = si_out.rename(columns={section_col: csv_join_col})

    out = df_csv.merge(si_out, on=csv_join_col, how="left")
    out.to_csv(csv_path, index=False)

    print(f"\n✅ Updated CSV saved (overwritten): {csv_path}")
    print(f"✅ Added column: {si_column_name}")


# -------------------------------
# SBF + SATELLITE (your existing logic)
# -------------------------------

def calculate_si_sbf_satellite_and_update_csv(
    buffer_shp_path: str,
    indicator_shp_path: str,
    nir_tif_path: str,
    rededge_tif_path: str,
    csv_path: str,
    si_column_name: str,
    csv_join_col: str = "section_nu",
    ib_type_col: str = "type",
    section_col: str = "section_nu",
    group_size: int = 4,
    num_sectors: int = 6
) -> None:
    buffer_gdf = gpd.read_file(buffer_shp_path)
    ib_gdf = gpd.read_file(indicator_shp_path)
    df_csv = pd.read_csv(csv_path)

    if csv_join_col not in df_csv.columns:
        raise ValueError(f"CSV must contain '{csv_join_col}'. Columns: {list(df_csv.columns)}")

    ndre, meta = read_ndre_from_rasters(nir_tif_path, rededge_tif_path)

    # Reproject polygons to raster CRS
    buffer_gdf = ensure_same_crs(buffer_gdf, meta["crs"])
    ib_gdf = ensure_same_crs(ib_gdf, meta["crs"])

    buffer_gdf = fix_polygons(buffer_gdf)
    ib_gdf = fix_polygons(ib_gdf)

    # Ensure required columns
    if section_col not in buffer_gdf.columns:
        raise ValueError(f"Buffer shapefile must have '{section_col}' for joining. Columns: {list(buffer_gdf.columns)}")

    if ib_type_col not in ib_gdf.columns:
        raise ValueError(f"Indicator shapefile must have '{ib_type_col}' (Reference/Canary). Columns: {list(ib_gdf.columns)}")

    # If IB doesn't have section_nu, create it like R does
    if section_col not in ib_gdf.columns:
        ib_gdf = add_section_numbers_in_groups(
            ib_gdf, section_col=section_col, group_size=group_size, num_sectors=num_sectors
        )

    # 1) NDRE_IB per IB polygon
    ib_gdf["NDRE_IB"] = [polygon_mean_from_raster(ndre, meta, geom) for geom in ib_gdf.geometry]

    # 2) sector-wise avg NDRE for Reference only
    ref = ib_gdf[ib_gdf[ib_type_col].astype(str).str.lower() == "reference"].copy()
    if ref.empty:
        raise ValueError("No 'Reference' rows found in indicator shapefile 'type' column.")

    ref_stats = (
        ref.groupby(section_col, as_index=False)
           .agg(average_NDRE_IB=("NDRE_IB", "mean"))
    )

    # 3) NDRE_Field per buffer polygon
    buffer_gdf["NDRE_Field"] = [polygon_mean_from_raster(ndre, meta, geom) for geom in buffer_gdf.geometry]

    merged = buffer_gdf.drop(columns="geometry").merge(ref_stats, on=section_col, how="left")
    merged[si_column_name] = merged["NDRE_Field"] / merged["average_NDRE_IB"]

    print("\n✅ SBF + SATELLITE preview (first 10 rows):")
    print(merged[[section_col, "NDRE_Field", "average_NDRE_IB", si_column_name]].head(10))

    # 4) Merge SI into CSV by section_nu
    si_out = merged[[section_col, si_column_name]].copy()
    si_out = si_out.rename(columns={section_col: csv_join_col})

    out = df_csv.merge(si_out, on=csv_join_col, how="left")
    out.to_csv(csv_path, index=False)

    print(f"\n✅ Updated CSV saved (overwritten): {csv_path}")
    print(f"✅ Added column: {si_column_name}")


# -------------------------------
# Run interactively (NOW supports all 4 combos)
# -------------------------------

if __name__ == "__main__":
    treatment = ask("Select treatment methodology (SBF or STANDARD): ").strip().upper()
    imagery = ask("Select imagery type (UAV or SATELLITE): ").strip().upper()

    si_column_name = ask("Enter the column header name for SI (e.g., SI_08_01): ").strip()

    if treatment == "STANDARD" and imagery == "UAV":
        field_column = ask("Enter the field column name used for grouping (e.g., Plot_Numbe, Field_ID): ").strip()
        buffer_shp = clean_path(ask("Enter full path to buffer_sector shapefile (.shp): "))
        ndre_shp = clean_path(ask("Enter full path to NDRE shapefile (.shp): "))
        csv_path = clean_path(ask("Enter full path to CSV file (.csv): "))

        calculate_si_standard_uav_and_update_csv(
            buffer_shp_path=buffer_shp,
            ndre_shp_path=ndre_shp,
            csv_path=csv_path,
            field_column=field_column,
            si_column_name=si_column_name,
            ndre_col="ndre",
            quantile_value=0.95
        )

    elif treatment == "STANDARD" and imagery == "SATELLITE":
        field_column = ask("Enter the field column name used for grouping (e.g., Plot_Numbe, Field_ID): ").strip()
        buffer_shp = clean_path(ask("Enter full path to buffer_sector shapefile (.shp): "))
        nir_tif = clean_path(ask("Enter full path to NIR GeoTIFF (.tif): "))
        rededge_tif = clean_path(ask("Enter full path to RedEdge GeoTIFF (.tif): "))
        csv_path = clean_path(ask("Enter full path to CSV file (.csv): "))

        calculate_si_standard_satellite_and_update_csv(
            buffer_shp_path=buffer_shp,
            nir_tif_path=nir_tif,
            rededge_tif_path=rededge_tif,
            csv_path=csv_path,
            field_column=field_column,
            si_column_name=si_column_name,
            quantile_value=0.95
        )

    elif treatment == "SBF" and imagery == "UAV":
        buffer_shp = clean_path(ask("Enter full path to buffer_sector shapefile (.shp): "))
        indicator_shp = clean_path(ask("Enter full path to Indicator Block shapefile (.shp): "))
        ndre_shp = clean_path(ask("Enter full path to NDRE shapefile (.shp): "))
        csv_path = clean_path(ask("Enter full path to CSV file (.csv): "))

        calculate_si_sbf_uav_and_update_csv(
            buffer_shp_path=buffer_shp,
            indicator_shp_path=indicator_shp,
            ndre_shp_path=ndre_shp,
          
            csv_path=csv_path,
            si_column_name=si_column_name,
            ndre_col="ndre",
            csv_join_col="section_nu",
            ib_type_col="type",
            section_col="section_nu",
            group_size=4,
            num_sectors=6
        )

    elif treatment == "SBF" and imagery == "SATELLITE":
        buffer_shp = clean_path(ask("Enter full path to buffer_sector shapefile (.shp): "))
        indicator_shp = clean_path(ask("Enter full path to Indicator Block shapefile (.shp): "))
        nir_tif = clean_path(ask("Enter full path to NIR GeoTIFF (.tif): "))
        rededge_tif = clean_path(ask("Enter full path to RedEdge GeoTIFF (.tif): "))
        csv_path = clean_path(ask("Enter full path to CSV file (.csv): "))

        calculate_si_sbf_satellite_and_update_csv(
            buffer_shp_path=buffer_shp,
            indicator_shp_path=indicator_shp,
            nir_tif_path=nir_tif,
            rededge_tif_path=rededge_tif,
            csv_path=csv_path,
            si_column_name=si_column_name,
            csv_join_col="section_nu",
            ib_type_col="type",
            section_col="section_nu",
            group_size=4,
            num_sectors=6
        )

    else:
        raise SystemExit("Invalid combination. Supported: STANDARD/UAV, STANDARD/SATELLITE, SBF/UAV, SBF/SATELLITE.")
