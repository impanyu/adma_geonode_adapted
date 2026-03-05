"""
Yield Summary Tool for ADMA.

This tool processes treatment sector and yield shapefiles to generate:
1. Buffer shapefile - buffered treatment polygons
2. Summary shapefile - merged spatial result with yield statistics
3. Summary Excel - tabular summary without geometry
4. Statistics Excel - ANOVA and treatment comparison table
"""

import geopandas as gpd
import pandas as pd
import numpy as np
import os
from typing import Dict, Tuple
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

try:
    from scipy.stats import f_oneway
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
import difflib


# SCHEMA DEFINITIONS
TREATMENT_COLS = {
    "section_nu": [
        "section_nu", "section_no", "section", "sector", "sec_num",
        "sectionnum", "section_id", "sector_no", "secno", "sect_num", "Section", "Sector", "SECTOR", "Section_id"
    ],
    "Treatment": [
        "treatment", "treat", "trt", "treatmt", "treat_name",
        "treatment_name", "Treatment", "rx"
    ]
}

YIELD_COLS = {
    "Yield": [
        "yield", "yld", "yield_bu", "yld_bu", "Yield",
        "yield_bu_ac", "yield_ac", "yield_avg", "Yld_Vol_Dr"
    ],
    "Moisture": [
        "moisture", "moist", "Moisture__", "Moisture",
        "moisture_percent", "moisture"
    ]
}


# HELPER FUNCTIONS
def normalize_col(col):
    return (
        col.lower()
        .strip()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("%", "")
        .replace(".", "")
    )


def pick_best_column(columns, aliases, keywords=None, cutoff=0.7):
    columns_norm = {c: normalize_col(c) for c in columns}
    aliases_norm = [normalize_col(a) for a in aliases]

    # 1. Exact alias match
    for col, col_n in columns_norm.items():
        if col_n in aliases_norm:
            return col

    # 2. Keyword match
    if keywords:
        for col, col_n in columns_norm.items():
            if any(k in col_n for k in keywords):
                return col

    # 3. Fuzzy match
    matches = difflib.get_close_matches(
        list(columns_norm.values()),
        aliases_norm,
        n=1,
        cutoff=cutoff
    )

    if matches:
        for col, col_n in columns_norm.items():
            if col_n == matches[0]:
                return col

    return None


def normalize_treatment_labels(series):
    return (
        series.astype(str)
        .str.lower()
        .str.strip()
        .replace({
            "grower": "Grower",
            "farmer": "Grower",
            "control": "Grower",
            "check": "Grower",
            "sentinel": "Sentinel",
            "sen": "Sentinel",
            "trial": "Sentinel",
            "test": "Sentinel"
        })
    )


# CORE FUNCTION
def run_yield_summary(
    treatment_path: str,
    yield_path: str,
    output_full_path: str,
    total_n_values: str,
    buffer_distance: float = -30.0,
    corn_price: float = 4.35,
    n_price: float = 0.50
):
    """
    Execute the full yield summary workflow.

    Parameters
    ----------
    treatment_path   : Path to the treatment sector shapefile.
    yield_path       : Path to the yield shapefile.
    output_full_path : Output path including the base file name
                       (e.g. D:/out/Yield_Run_01).
    total_n_map      : Dict of {section_nu: TotalN} for non-unused sectors only.
    buffer_distance  : Buffer distance in metres (default -30).
    corn_price       : Corn price per bushel ($/bu, default 4.35).
    n_price          : Nitrogen price per lb N ($/lb N, default 0.50).
    """
    output_folder = os.path.dirname(output_full_path)
    output_name = os.path.basename(output_full_path)

    # LOAD DATA
    t = gpd.read_file(treatment_path).to_crs(epsg=3857)
    y = gpd.read_file(yield_path).to_crs(epsg=3857)

    # Normalize column names
    t.columns = [normalize_col(c) for c in t.columns]
    y.columns = [normalize_col(c) for c in y.columns]

    # COLUMN DETECTION
    t_sec = pick_best_column(
        list(t.columns),
        TREATMENT_COLS["section_nu"],
        ["section", "sector", "sec"]
    )

    t_trt = pick_best_column(
        list(t.columns),
        TREATMENT_COLS["Treatment"],
        ["treat", "trt", "rx"]
    )

    if not (t_sec and t_trt):
        raise ValueError(
            f"Could not identify section/treatment columns.\n"
            f"Available columns: {list(t.columns)}"
        )

    y_yld = pick_best_column(
        list(y.columns),
        YIELD_COLS["Yield"],
        ["yield", "yld"]
    )

    y_mois = pick_best_column(
        list(y.columns),
        YIELD_COLS["Moisture"],
        ["moist", "h2o"]
    )

    if not (y_yld and y_mois):
        raise ValueError(
            f"Could not identify yield/moisture columns.\n"
            f"Available columns: {list(y.columns)}"
        )

    # Standardize column names
    t = t.rename(columns={t_sec: "section_nu", t_trt: "Treatment"})
    y = y.rename(columns={y_yld: "Yield", y_mois: "Moisture"})
    t["Treatment"] = normalize_treatment_labels(t["Treatment"])

    # REMOVE UNUSED SECTORS from treatment layer early
    unused_labels = ["unused", "Unused", "UNUSED"]
    t = t[~t["Treatment"].isin(unused_labels)].copy()

    # PARSE TOTAL N — one value per active (non-unused) sector in section order
    TotalN_list = [float(n.strip()) for n in total_n_values.split(",")]
    if len(TotalN_list) != len(t):
        raise ValueError(
            f"Total N count ({len(TotalN_list)}) does not match "
            f"number of active sectors ({len(t)})."
        )

    # BUFFER & SPATIAL JOIN
    t_buff = t.copy()
    t_buff["geometry"] = t_buff.buffer(buffer_distance)

    result = gpd.sjoin(t_buff, y, predicate="contains", how="left")

    grouped = result.groupby("section_nu").agg(
        mean_yield=("Yield", "mean"),
        mean_moisture=("Moisture", "mean")
    ).reset_index()

    merged = t_buff.merge(grouped, on="section_nu")

    # CALCULATIONS MATRIX
    merged["TotalN"] = TotalN_list
    merged["NUE"] = merged["TotalN"] / merged["mean_yield"]
    merged["PfP"] = (merged["mean_yield"] * 56) / merged["TotalN"]

    Corn_per_bu = corn_price
    N_lb_N_perac = n_price
    merged["MNR"] = (
        merged["mean_yield"] * Corn_per_bu
        - merged["TotalN"] * N_lb_N_perac
    )

    # OUTPUTS
    os.makedirs(output_folder, exist_ok=True)

    # 1. Buffer shapefile — buffered treatment polygons (unused removed)
    t_buff.to_file(os.path.join(output_folder, f"{output_name}_buffer.shp"))
    print(f"  📐 Buffer shapefile saved:  {output_name}_buffer.shp")

    # 2. Summary shapefile — merged spatial result (unused removed)
    merged.to_file(os.path.join(output_folder, f"{output_name}_summary.shp"))
    print(f"  🗺️  Summary shapefile saved: {output_name}_summary.shp")

    # 3. Summary Excel — same data without geometry column (unused removed)
    merged.drop(columns="geometry").to_excel(
        os.path.join(output_folder, f"{output_name}_summary.xlsx"),
        index=False
    )
    print(f"  📊 Summary Excel saved:     {output_name}_summary.xlsx")

    # TREATMENT SUMMARY — unused already removed from merged

    summary = {}
    for tmt in merged["Treatment"].unique():
        df = merged[merged["Treatment"] == tmt]
        summary[tmt] = {
            "Yield": df["mean_yield"].mean(),
            "Moisture": df["mean_moisture"].mean(),
            "TotalN": df["TotalN"].mean()
        }

    stat_df = pd.DataFrame(
        index=["Grower", "Sentinel", "Gr_Sen", "Sen_Gr", "P_Value"],
        columns=["Total_N_rate", "Moisture", "Yield", "PFP", "NUE", "MNR"]
    )

    for k in ["Grower", "Sentinel"]:
        if k in summary:
            stat_df.loc[k, "Yield"] = summary[k]["Yield"]
            stat_df.loc[k, "Moisture"] = summary[k]["Moisture"]
            stat_df.loc[k, "Total_N_rate"] = summary[k]["TotalN"]

    stat_df["NUE"] = stat_df["Total_N_rate"].astype(float) / stat_df["Yield"].astype(float)
    stat_df["PFP"] = (stat_df["Yield"].astype(float) * 56) / stat_df["Total_N_rate"].astype(float)
    stat_df["MNR"] = (
        stat_df["Yield"].astype(float) * Corn_per_bu
        - stat_df["Total_N_rate"].astype(float) * N_lb_N_perac
    )

    stat_df.loc["Gr_Sen", "NUE"] = stat_df.loc["Grower", "NUE"] - stat_df.loc["Sentinel", "NUE"]
    stat_df.loc["Sen_Gr", "PFP"] = stat_df.loc["Sentinel", "PFP"] - stat_df.loc["Grower", "PFP"]
    stat_df.loc["Sen_Gr", "MNR"] = stat_df.loc["Sentinel", "MNR"] - stat_df.loc["Grower", "MNR"]

    # ANOVA
    for metric, label in zip(
        ["mean_yield", "mean_moisture", "TotalN", "PfP", "NUE", "MNR"],
        ["Yield", "Moisture", "Total_N_rate", "PFP", "NUE", "MNR"]
    ):
        groups = [grp[metric].dropna() for _, grp in merged.groupby("Treatment")]
        if all(len(g) > 1 for g in groups):
            _, p_val = f_oneway(*groups)
            stat_df.loc["P_Value", label] = round(p_val, 4)

    stat_df.reset_index(inplace=True)
    stat_df.rename(columns={"index": "Batie_Treatments"}, inplace=True)

    # 4. Stat Excel — ANOVA and treatment comparison table
    stat_df.to_excel(
        os.path.join(output_folder, f"{output_name}_stat_final.xlsx"),
        index=False
    )
    print(f"  📈 Stat Excel saved:        {output_name}_stat_final.xlsx")

    print(f"\n✅ Done! All 4 outputs saved in: {output_folder}")
    print(f"   1. {output_name}_buffer.shp")
    print(f"   2. {output_name}_summary.shp")
    print(f"   3. {output_name}_summary.xlsx")
    print(f"   4. {output_name}_stat_final.xlsx")


def process_yield_summary(
    treatment_path: str,
    yield_path: str,
    output_dir: str,
    total_n_values: str,
    buffer_distance: float = -30.0,
    corn_price: float = 4.35,
    n_price: float = 0.50
) -> Tuple[bool, str, Dict]:
    """
    ADMA-compatible wrapper for yield summary processing.
    
    Args:
        treatment_path: Path to the treatment sector shapefile
        yield_path: Path to the yield shapefile
        output_dir: Directory where output files will be saved
        total_n_values: Comma-separated Total N values for active sectors
        buffer_distance: Buffer distance in meters (default -30)
        corn_price: Corn price per bushel ($/bu, default 4.35)
        n_price: Nitrogen price per lb N ($/lb N, default 0.50)
        
    Returns:
        Tuple of (success: bool, message: str, output_files: Dict)
    """
    output_files = {}
    
    try:
        # Validate input files
        if not treatment_path.lower().endswith(".shp"):
            return False, "Treatment file must be a .shp file", {}
        
        if not yield_path.lower().endswith(".shp"):
            return False, "Yield file must be a .shp file", {}
        
        if not os.path.isfile(treatment_path):
            return False, f"Treatment shapefile not found: {treatment_path}", {}
        
        if not os.path.isfile(yield_path):
            return False, f"Yield shapefile not found: {yield_path}", {}
        
        # Validate total_n_values
        if not total_n_values or not total_n_values.strip():
            return False, "Total N values are required", {}
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate output base name from treatment file
        base_name = os.path.splitext(os.path.basename(treatment_path))[0]
        output_full_path = os.path.join(output_dir, f"{base_name}_yield")
        
        # Run the core yield summary function
        run_yield_summary(
            treatment_path=treatment_path,
            yield_path=yield_path,
            output_full_path=output_full_path,
            total_n_values=total_n_values,
            buffer_distance=buffer_distance,
            corn_price=corn_price,
            n_price=n_price
        )
        
        output_name = f"{base_name}_yield"
        
        # Collect output file paths
        shapefile_extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg']
        
        # Buffer shapefile
        buffer_shp = os.path.join(output_dir, f"{output_name}_buffer.shp")
        if os.path.exists(buffer_shp):
            output_files['buffer'] = buffer_shp
            output_files['buffer_components'] = []
            for ext in shapefile_extensions:
                component_path = os.path.join(output_dir, f"{output_name}_buffer{ext}")
                if os.path.exists(component_path):
                    output_files['buffer_components'].append(component_path)
        
        # Summary shapefile
        summary_shp = os.path.join(output_dir, f"{output_name}_summary.shp")
        if os.path.exists(summary_shp):
            output_files['summary_shp'] = summary_shp
            output_files['summary_shp_components'] = []
            for ext in shapefile_extensions:
                component_path = os.path.join(output_dir, f"{output_name}_summary{ext}")
                if os.path.exists(component_path):
                    output_files['summary_shp_components'].append(component_path)
        
        # Summary Excel
        summary_xlsx = os.path.join(output_dir, f"{output_name}_summary.xlsx")
        if os.path.exists(summary_xlsx):
            output_files['summary_xlsx'] = summary_xlsx
        
        # Statistics Excel
        stat_xlsx = os.path.join(output_dir, f"{output_name}_stat_final.xlsx")
        if os.path.exists(stat_xlsx):
            output_files['stat_xlsx'] = stat_xlsx
        
        message = (
            f"Yield Summary completed successfully. "
            f"Generated {len([k for k in output_files if not k.endswith('_components')])} output files."
        )
        
        return True, message, output_files
        
    except ValueError as e:
        return False, str(e), output_files
    except Exception as e:
        return False, f"Processing error: {str(e)}", output_files


# MAIN — interactive entry point
def main():
    print("=" * 60)
    print("         YIELD SUMMARY TOOL")
    print("=" * 60)

    treatment_path = input(
        "\n[1/6] Enter full path to treatment sector shapefile:\n> "
    ).strip()

    yield_path = input(
        "\n[2/6] Enter full path to yield shapefile:\n> "
    ).strip()

    output_full_path = input(
        "\n[3/6] Enter full output path including base file name\n"
        "      (e.g. D:/out/Yield_Run_01  — no extension needed):\n> "
    ).strip()

    total_n_values = input(
        "\n[4/6] Enter comma-separated Total N values for active sectors only\n"
        "      (Grower and Sentinel sectors, excluding unused, in section order\n"
        "       e.g. 122,99.5,122,98.5,105.5,122,122,65.5):\n> "
    ).strip()

    buffer_input = input(
        "\n[5/6] Enter buffer distance in meters\n"
        "      (press Enter to use default -30):\n> "
    ).strip()
    buffer_distance = float(buffer_input) if buffer_input else -30.0

    corn_input = input(
        "\n[6/6] Enter corn price per bushel ($/bu)\n"
        "      (press Enter to use default 4.35):\n> "
    ).strip()
    corn_price = float(corn_input) if corn_input else 4.35

    n_input = input(
        "\n      Enter nitrogen price per lb ($/lb N)\n"
        "      (press Enter to use default 0.50):\n> "
    ).strip()
    n_price = float(n_input) if n_input else 0.50

    print("\n" + "=" * 60)
    print("Processing... please wait.")
    print("=" * 60 + "\n")

    run_yield_summary(
        treatment_path=treatment_path,
        yield_path=yield_path,
        output_full_path=output_full_path,
        total_n_values=total_n_values,
        buffer_distance=buffer_distance,
        corn_price=corn_price,
        n_price=n_price
    )


if __name__ == "__main__":
    main()
