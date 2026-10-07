"""
Point to Polygon Coverage Tool, by Sreeja Vinod (UNL), vendored into ADMA.

Converts machine-logged points (yield, seeding, NH3, fertigation) into
polygons of the ground covered at each point, from the swath width, distance,
heading and Y offset recorded with it. Neighbours along a pass share edges,
reduced-width passes shift to the cut side, and each polygon keeps the point's
attributes plus its area and its overlap with ground already covered.

Her script is reproduced as she sent it on 2026-10-01
(rect_buffer_tool_FinalScript.py). The changes are only these, each marked
ADMA EDIT:

  1. load_points() and to_metric() take what they used to ask for at a prompt
     (an EPSG code) as arguments, and raise instead of calling input(): a
     worker has no console.
  2. main() is split into prepare_points() and make_coverage(), the same
     statements in the same order, so the tool can run without prompts.
     main() keeps the prompts and still works from a terminal.
  3. process_point_to_polygon() is ADMA's entry point.

Run with the default answers, the result is identical polygon for polygon to
the outputs she sent with the script for 23_2436S_HV_Raw and
1330_31_UNL_ARDC_Grain_Harvest_2_Corn_2026.
"""
import os
import sys
import time

import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
import shapely.affinity

FT_PER_M = 3.28084
M2_PER_AC = 4046.8564224
UNIT_TO_M = {"ft": 1 / FT_PER_M, "in": 0.0254, "m": 1.0}

OVERLAP_FLAG_PCT = 25.0   # flag rectangles overlapped at least this much
MAX_GAP_FACTOR = 2.5      # points farther apart than 2.5 x typical spacing = new pass

# Heading adjustment (chosen at run time: none / round / smooth / both)
HEADING_DECIMALS = 1      # rounding: 1 = nearest 0.1 deg, 2 = 0.01 deg, 0 = whole deg
SMOOTH_WINDOW = 5         # smoothing: rolling average over this many points (odd number)


WIDTH_KEYS = ["swth", "swath", "width", "header", "boom"]
DIST_KEYS = ["distance", "dist"]
HEAD_KEYS = ["track", "heading", "hding", "direction", "course", "angle"]
FILTER_KEYS = ["area_count", "areacount", "header_stat", "status"]
OBJ_ID_KEYS = ["obj", "fid", "record", "seq"]
PASS_KEYS = ["pass", "swath_num", "track_num"]
OFFSET_KEYS = ["y_offset", "yoffset", "y_off", "offset_y", "lat_offset"]



# helpers

def ask(msg, default=None):
    tail = f" [{default}]" if default is not None else ""
    val = input(f"{msg}{tail}: ").strip().strip('"').strip("'")
    return val if val else ("" if default is None else str(default))


def guess_col(columns, keywords):
    for kw in keywords:
        for c in columns:
            if kw in c.lower():
                return c
    return None


def pick_column(gdf, label, keywords, numeric=True, allow_none=False):
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    if numeric:
        cols = [c for c in cols if pd.api.types.is_numeric_dtype(gdf[c])]
    if not cols:
        print(f"  No suitable columns for {label}.")
        return None

    guess = guess_col(cols, keywords) if keywords else None
    print(f"\nColumn for {label}:")
    if allow_none:
        print("  0. None")
    for i, c in enumerate(cols, 1):
        s = gdf[c].dropna()
        sample = s.iloc[0] if len(s) else "NA"
        mark = "   <- suggested" if c == guess else ""
        print(f"  {i}. {c}  (e.g. {sample}){mark}")

    default = cols.index(guess) + 1 if guess else (0 if allow_none else 1)
    while True:
        val = ask("Select", default)
        if val.isdigit():
            n = int(val)
            if allow_none and n == 0:
                return None
            if 1 <= n <= len(cols):
                return cols[n - 1]
        print("  Invalid selection.")


# Loading


def load_points(path, epsg=None):
    # ADMA EDIT 1: the two input() prompts became the `epsg` argument and a
    # ValueError saying what is missing.
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt"):
        df = pd.read_csv(path)
        cols = list(df.columns)
        x = guess_col(cols, ["lon", "long", "easting", "x"])
        y = guess_col(cols, ["lat", "northing", "y"])
        if x is None or y is None:
            raise ValueError("Could not find X/Y (longitude/latitude) columns in the CSV.")
        geographic = df[x].abs().max() <= 180 and df[y].abs().max() <= 90
        if not geographic and epsg is None:
            raise ValueError(f"The CSV coordinates in {x}/{y} are not longitude/latitude; "
                             f"give their EPSG code.")
        epsg = 4326 if geographic else epsg
        print(f"  CSV coordinates: {x}, {y} (EPSG:{epsg})")
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[x], df[y]),
                               crs=f"EPSG:{epsg}")
    else:
        gdf = gpd.read_file(path)

    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if not (gdf.geom_type == "Point").all():
        print("  Non-point geometries found, using centroids.")
        gdf["geometry"] = gdf.geometry.centroid
    if gdf.crs is None:
        if epsg is None:
            raise ValueError("The input has no coordinate system (.prj); give its EPSG code.")
        gdf = gdf.set_crs(f"EPSG:{epsg}")
    return gdf.reset_index(drop=True)



def unique_columns(gdf):
    """Rename duplicate column names so the shapefile can be written.

    Shapefile field names are case-insensitive and limited to 10 characters,
    so 'Product' and 'PRODUCT', or two names sharing the first 10 characters,
    also count as duplicates. Repeats get a numeric suffix (Product_1).
    """
    seen, new, renamed = set(), [], []
    for c in gdf.columns:
        if c == gdf.geometry.name:
            new.append(c)
            continue
        name, k = c, 1
        while name.lower()[:10] in seen:
            sfx = f"_{k}"
            name = c[:10 - len(sfx)] + sfx
            k += 1
        seen.add(name.lower()[:10])
        new.append(name)
        if name != c:
            renamed.append(f"{c} -> {name}")
    gdf = gdf.copy()
    gdf.columns = new
    return gdf, renamed


def to_metric(gdf, log=print):  # ADMA EDIT 1: log instead of print
    """Project to a CRS with meter units (UTM if needed)."""
    unit = (gdf.crs.axis_info[0].unit_name or "").lower() if gdf.crs.is_projected else ""
    if gdf.crs.is_projected and unit in ("metre", "meter"):
        return gdf
    utm = gdf.estimate_utm_crs()
    log(f"  Reprojecting to {utm.name} (meters).")
    return gdf.to_crs(utm)



# Auto-detection

def auto_filter(gdf):
    """Keep 'On' records if an on/off column (e.g. Area_Count) exists."""
    for c in gdf.columns:
        if c == gdf.geometry.name or not any(k in c.lower() for k in FILTER_KEYS):
            continue
        vals = gdf[c].astype(str).str.strip().str.lower()
        if vals.isin(["on", "off"]).mean() > 0.9:
            mask = (vals == "on").to_numpy()
            return gdf[mask].reset_index(drop=True), f"{c} = On ({(~mask).sum():,} Off removed)"
    return gdf, "none found"


def median_step(gdf):
    """Median distance between consecutive points, in CRS units."""
    x, y = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
    s = np.hypot(np.diff(x), np.diff(y))
    s = s[s > 0]
    return np.median(s) if len(s) else np.inf


def auto_object_id(gdf):
    """Put points in driving order and make sure each has an object ID.

    If an object ID column exists (e.g. Obj__Id), points are sorted by it only
    when the IDs are unique AND sorting gives tighter spacing between
    consecutive points than the file order. IDs that restart or repeat (for
    example several loads in one file) would otherwise interleave points from
    different parts of the field. If no ID column exists, OBJ_ID is created
    from the file order (1, 2, 3 ...).
    """
    num = [c for c in gdf.columns if c != gdf.geometry.name
           and pd.api.types.is_numeric_dtype(gdf[c])]
    col = guess_col(num, OBJ_ID_KEYS)
    if not col:
        gdf = gdf.copy()
        gdf.insert(0, "OBJ_ID", np.arange(1, len(gdf) + 1))
        return gdf, "OBJ_ID (created from file order)"

    if not gdf[col].is_unique:
        return gdf, f"{col} (not unique, kept file order)"
    sorted_gdf = gdf.sort_values(col, kind="stable").reset_index(drop=True)
    if median_step(sorted_gdf) <= median_step(gdf) * 1.05:
        return sorted_gdf, f"{col} (sorted)"
    return gdf, f"{col} (file order kept, sorting by ID scrambled the path)"


def unit_from_name(col):
    """Unit hint from the column name (e.g. Distance_f, Width_m, Dist_ft)."""
    c = col.lower().rstrip("_")
    if c.endswith(("_ft", "_f", "feet", "(ft)")) or "_ft_" in c:
        return "ft"
    if c.endswith(("_in", "inch", "inches", "(in)")):
        return "in"
    if c.endswith(("_m", "meter", "metre", "(m)")) or "_m_" in c:
        return "m"
    return None


def unit_from_spacing(values, spacing_m):
    """Unit whose conversion best matches the measured point spacing."""
    v = np.nanmedian(values[values > 0]) if (values > 0).any() else np.nan
    if not np.isfinite(v) or not np.isfinite(spacing_m) or spacing_m <= 0:
        return None, np.nan
    ratio = v / spacing_m       # ~3.28 for ft, ~39.4 for in, ~1 for m
    best = min(UNIT_TO_M, key=lambda u: abs(np.log(ratio * UNIT_TO_M[u])))
    return best, ratio


# Geometry

def neighbor_vectors(x, y, pass_id, max_gap):
    """Forward and backward step vectors, broken at pass changes and gaps."""
    n = len(x)
    dx_f = np.full(n, np.nan)
    dy_f = np.full(n, np.nan)
    dx_f[:-1] = np.diff(x)
    dy_f[:-1] = np.diff(y)
    d_f = np.hypot(dx_f, dy_f)

    same_next = np.zeros(n, dtype=bool)
    same_next[:-1] = pass_id[1:] == pass_id[:-1]
    ok_f = same_next & (d_f > 0) & (d_f <= max_gap)

    dx_b = np.roll(dx_f, 1); dx_b[0] = np.nan
    dy_b = np.roll(dy_f, 1); dy_b[0] = np.nan
    d_b = np.roll(d_f, 1); d_b[0] = np.nan
    ok_b = np.roll(ok_f, 1); ok_b[0] = False
    return dx_f, dy_f, d_f, ok_f, dx_b, dy_b, d_b, ok_b


def compute_heading(vec):
    """Compass heading (0 = north, clockwise) from consecutive points."""
    dx_f, dy_f, _, ok_f, dx_b, dy_b, _, ok_b = vec
    dx = np.where(ok_f, dx_f, np.where(ok_b, dx_b, np.nan))
    dy = np.where(ok_f, dy_f, np.where(ok_b, dy_b, np.nan))
    return np.degrees(np.arctan2(dx, dy)) % 360


def compute_spacing(vec):
    """Distance to previous point on the same pass (next point at pass start)."""
    _, _, d_f, ok_f, _, _, d_b, ok_b = vec
    return np.where(ok_b, d_b, np.where(ok_f, d_f, np.nan))


def smooth_heading(heading, vec, window):
    """Rolling average of heading within each continuous run of points.

    Uses a circular mean (averages sin and cos), so 359 and 1 deg average to
    0 deg, not 180. Runs break at pass changes and spacing gaps, so the
    average never mixes two passes. Ends of a run use fewer points.
    """
    ok_f = vec[3]
    run_id = np.r_[0, np.cumsum(~ok_f[:-1])]
    rad = np.radians(heading)
    df = pd.DataFrame({"run": run_id, "s": np.sin(rad), "c": np.cos(rad)})
    roll = (df.groupby("run")[["s", "c"]]
              .rolling(window, center=True, min_periods=1).mean()
              .reset_index(level=0, drop=True).sort_index())
    smoothed = np.degrees(np.arctan2(roll["s"].to_numpy(), roll["c"].to_numpy())) % 360
    return np.where(np.isfinite(heading), smoothed, heading)


def build_rectangles(x, y, heading_deg, width, height, anchor):
    """Rotated rectangles (vectorized).

    anchor 'center'  : point at the rectangle center
    anchor 'trailing': point at the leading edge; rectangle extends backward
                       over the distance just traveled (yield monitor logging)
    """
    h = np.radians(heading_deg)
    ux, uy = np.sin(h), np.cos(h)     # along travel
    px, py = np.cos(h), -np.sin(h)    # to the right of travel
    hw = width / 2.0
    if anchor == "trailing":
        front, back = np.zeros_like(height), height
    else:
        front, back = height / 2.0, height / 2.0

    cx = np.stack([x + ux * front - px * hw, x + ux * front + px * hw,
                   x - ux * back + px * hw, x - ux * back - px * hw], axis=1)
    cy = np.stack([y + uy * front - py * hw, y + uy * front + py * hw,
                   y - uy * back + py * hw, y - uy * back - py * hw], axis=1)
    return shapely.polygons(np.stack([cx, cy], axis=2))


def build_connected(x, y, heading_deg, width, height, ok_f, valid):
    """Connected swath polygons that share edges with their neighbors.

    Consecutive points on the same pass are joined by a cut line placed at
    the midpoint between them, oriented across the average of their two
    headings. Each point's polygon runs from its back cut line to its front
    cut line, its own swath width wide. Neighbors therefore share an edge
    exactly: no sawtooth, no gaps, no overlap along the pass.

    At the start or end of a run (pass change, gap, or stop), the polygon
    extends beyond the point by half its distance value, limited to half the
    real spacing to its joined neighbor (or the typical spacing for a point
    standing alone). This keeps points logged right after a gap, whose
    distance value includes the gap, from reaching back over earlier points.
    Any polygon that folds over itself (very sharp turn) falls back to a
    rectangle of the same limited length.

    Returns polygons for all points (use only the valid ones) and the number
    of fallbacks.
    """
    n = len(x)
    h = np.radians(heading_deg)
    ux, uy = np.sin(h), np.cos(h)

    # next point values (last point has none)
    x_n, y_n, h_n = np.r_[x[1:], np.nan], np.r_[y[1:], np.nan], np.r_[h[1:], np.nan]

    # join point i to i + 1 only if both are valid, on the same pass, and
    # their spacing is within MAX_GAP_FACTOR x their own distance values
    step_next = np.hypot(x_n - x, y_n - y)
    local_max = MAX_GAP_FACTOR * np.fmax(height, np.r_[height[1:], 0.0])
    link = (ok_f & valid & np.r_[valid[1:], False]
            & (step_next <= local_max))                # point i joined to i + 1
    link_prev = np.r_[False, link[:-1]]                # point i joined to i - 1

    # Length used at the open end of a run. The distance value right after a
    # gap includes the gap itself, so it is limited to the real spacing to the
    # joined neighbor (or the typical spacing when the point stands alone).
    step_prev = np.r_[np.nan, step_next[:-1]]
    typical = np.nanmedian(step_next[link]) if link.any() else np.nanmedian(height[valid])
    ref = np.where(link_prev, step_prev, np.where(link, step_next, typical))
    end_len = np.fmin(height, ref)
    half_end = end_len / 2.0

    # front cut line of each point
    mid_x, mid_y = (x + x_n) / 2.0, (y + y_n) / 2.0
    mid_h = np.arctan2(np.sin(h) + np.sin(h_n), np.cos(h) + np.cos(h_n))
    fx = np.where(link, mid_x, x + ux * half_end)
    fy = np.where(link, mid_y, y + uy * half_end)
    fh = np.where(link, mid_h, h)

    # back cut line = previous point's front cut line, if joined
    bx = np.where(link_prev, np.r_[np.nan, fx[:-1]], x - ux * half_end)
    by = np.where(link_prev, np.r_[np.nan, fy[:-1]], y - uy * half_end)
    bh = np.where(link_prev, np.r_[np.nan, fh[:-1]], h)

    hw = width / 2.0
    fpx, fpy = np.cos(fh) * hw, -np.sin(fh) * hw   # right-hand offset at front
    bpx, bpy = np.cos(bh) * hw, -np.sin(bh) * hw   # right-hand offset at back

    cx = np.stack([bx - bpx, bx + bpx, fx + fpx, fx - fpx], axis=1)
    cy = np.stack([by - bpy, by + bpy, fy + fpy, fy - fpy], axis=1)
    coords = np.nan_to_num(np.stack([cx, cy], axis=2))
    polys = shapely.polygons(coords)

    # fallback: self-intersecting or degenerate shapes become rectangles
    bad = valid & (~shapely.is_valid(polys) | (shapely.area(polys) <= 0))
    if bad.any():
        polys[bad] = build_rectangles(x[bad], y[bad], heading_deg[bad],
                                      width[bad], end_len[bad], "center")
    return polys, int(bad.sum())


def _earlier_cover(i, test_geom, geoms, tree, pass_id, skip):
    """Union of polygons recorded before point i that touch test_geom
    (ignoring its immediate neighbors on the same pass)."""
    cand = tree.query(test_geom, predicate="intersects")
    near = (pass_id[cand] == pass_id[i]) & (np.abs(cand - i) <= skip)
    cand = cand[(cand < i) & ~near]
    if not len(cand):
        return None
    return geoms[cand[0]] if len(cand) == 1 else shapely.union_all(geoms[cand])


def _overlap_area(geom, cover):
    return 0.0 if cover is None else shapely.intersection(geom, cover).area


def shift_to_cut_side(geoms, heading_deg, width, pass_id, offset_m=None, skip=2):
    """Move polygons sideways to the strip that was actually cut.

    1. Machine offset (preferred): where the offset column (e.g. Y_Offset_f)
       has a value, the polygon is moved sideways by that amount. The sign
       convention (+ = right or left of travel) differs between monitors, so
       it is decided from the data: both signs are tried on a sample of
       points and the one giving less overlap with earlier coverage is used.
    2. Overlap test (fallback): reduced-width points with no offset are tried
       shifted left and right by (full width - width) / 2, and the side with
       less overlap with earlier coverage is kept. Ties follow the previous
       partial point on the same pass, otherwise the polygon stays centered.

    Full width = median recorded width. Returns polygons, partial flag,
    signed shift in meters (+ right of travel), shift source, and a note on
    the offset sign.
    """
    n = len(geoms)
    full = np.nanmedian(width)
    tol = max(0.02 * full, 0.15)                       # 2 % or 0.15 m (0.5 ft)
    partial = (width > 0) & (width < full - tol)
    shift = np.zeros(n)
    src = np.full(n, "", dtype=object)
    sign_note = None

    h = np.radians(heading_deg)
    px, py = np.cos(h), -np.sin(h)                     # unit vector to the right
    orig = geoms
    geoms = geoms.copy()
    tree = shapely.STRtree(orig)

    #  machine offset 
    has_off = np.zeros(n, dtype=bool)
    if offset_m is not None:
        has_off = np.isfinite(offset_m) & (np.abs(offset_m) > 0.003)
    if has_off.any():
        idx = np.flatnonzero(has_off)
        sample = idx[partial[idx]] if partial[idx].any() else idx
        sample = sample[:: max(1, len(sample) // 400)][:400]
        score = {}
        for s in (1, -1):
            total = 0.0
            for i in sample:
                g = shapely.affinity.translate(orig[i], s * px[i] * offset_m[i],
                                               s * py[i] * offset_m[i])
                total += _overlap_area(g, _earlier_cover(i, g, orig, tree, pass_id, skip))
            score[s] = total
        sign = 1 if score[1] <= score[-1] else -1
        tie = abs(score[1] - score[-1]) < 1e-6
        sign_note = (f"positive offset = {'right' if sign > 0 else 'left'} of travel"
                     + (" (assumed, data could not tell)" if tie else " (from data)"))
        for i in idx:
            d = sign * offset_m[i]
            geoms[i] = shapely.affinity.translate(orig[i], px[i] * d, py[i] * d)
            shift[i] = d
            src[i] = "offset"

    # overlap test for reduced-width points without offset 
    prev_side = {}
    for i in np.flatnonzero(partial & ~has_off):
        off = (full - width[i]) / 2.0
        dx, dy = px[i] * off, py[i] * off
        right = shapely.affinity.translate(orig[i], dx, dy)
        left = shapely.affinity.translate(orig[i], -dx, -dy)
        cover = _earlier_cover(i, shapely.union(left, right), geoms, tree, pass_id, skip)
        ov_r, ov_l = _overlap_area(right, cover), _overlap_area(left, cover)
        if abs(ov_r - ov_l) > 0.01 * orig[i].area:
            side = 1 if ov_r < ov_l else -1
        else:
            side = prev_side.get(pass_id[i], 0)        # tie: follow the pass
        if side:
            geoms[i] = right if side > 0 else left
            shift[i] = side * off
            src[i] = "overlap"
            prev_side[pass_id[i]] = side
    return geoms, partial, shift, src, sign_note


def overlap_with_earlier(geoms, pass_id, skip_neighbors=2):
    """Percent of each rectangle already covered by EARLIER rectangles.

    Rows must be sorted by object ID. Rectangles within `skip_neighbors`
    records on the same pass are ignored (they only touch edge to edge).
    """
    n = len(geoms)
    tree = shapely.STRtree(geoms)
    i_idx, j_idx = tree.query(geoms, predicate="intersects")
    keep = j_idx < i_idx
    near = (pass_id[i_idx] == pass_id[j_idx]) & (i_idx - j_idx <= skip_neighbors)
    keep &= ~near
    i_idx, j_idx = i_idx[keep], j_idx[keep]

    pct = np.zeros(n)
    if len(i_idx) == 0:
        return pct

    order = np.argsort(i_idx, kind="stable")
    i_idx, j_idx = i_idx[order], j_idx[order]
    uniq, starts = np.unique(i_idx, return_index=True)
    ends = np.append(starts[1:], len(i_idx))
    areas = shapely.area(geoms)

    for k, i in enumerate(uniq):
        js = j_idx[starts[k]:ends[k]]
        cover = geoms[js[0]] if len(js) == 1 else shapely.union_all(geoms[js])
        inter = shapely.intersection(geoms[i], cover).area
        pct[i] = 100.0 * inter / areas[i] if areas[i] > 0 else 0.0
    return np.clip(pct, 0, 100)




# --- ADMA EDIT 2 -----------------------------------------------------------
# main() split in three so a worker can run the tool: prepare_points() and
# make_coverage() are main()'s own statements, in order, with print() routed
# through `log` and sys.exit() raised as ToolError; main() keeps the prompts
# and calls them, so the script still works from a terminal exactly as before.

class ToolError(Exception):
    """What main() used to sys.exit() with."""


def prepare_points(path, log=print, epsg=None):
    """Load, put in driving order, filter, project, and find the passes."""
    gdf = load_points(path, epsg=epsg)
    n_loaded = len(gdf)
    src_crs = gdf.crs
    log(f"  Loaded {n_loaded:,} points. CRS: {src_crs.name}")

    #  auto: object ID, filter, projection 
    gdf, oid_label = auto_object_id(gdf)
    gdf, filt_label = auto_filter(gdf)
    if gdf.empty:
        raise ToolError("No points left after filtering.")
    gdf = to_metric(gdf, log=log)

    x = gdf.geometry.x.to_numpy()
    y = gdf.geometry.y.to_numpy()

    # typical spacing between consecutive points and the pass-break distance
    steps = np.hypot(np.diff(x), np.diff(y))
    spacing_m = np.median(steps[steps > 0]) if (steps > 0).any() else np.nan
    max_gap_m = MAX_GAP_FACTOR * spacing_m if np.isfinite(spacing_m) else 5.0

    #  auto: pass ID 
    pass_col = guess_col([c for c in gdf.columns if c != gdf.geometry.name], PASS_KEYS)
    if pass_col:
        pass_id = pd.factorize(gdf[pass_col].astype(str))[0]
        p_label = f"{pass_col}"
    else:
        step0 = np.r_[0.0, np.hypot(np.diff(x), np.diff(y))]
        pass_id = np.cumsum(step0 > max_gap_m)
        p_label = f"detected from gaps > {max_gap_m * FT_PER_M:.1f} ft"
    vec = neighbor_vectors(x, y, pass_id, max_gap_m)

    return dict(path=path, gdf=gdf, n_loaded=n_loaded, src_crs=src_crs,
                oid_label=oid_label, filt_label=filt_label, x=x, y=y,
                spacing_m=spacing_m, max_gap_m=max_gap_m, pass_col=pass_col,
                pass_id=pass_id, p_label=p_label, vec=vec)


def make_coverage(ctx, w_col, d_col, h_col, adj, out_dir, log=print):
    """Build, summarise and save the polygons. Returns (path, summary dict)."""
    gdf, x, y = ctx["gdf"], ctx["x"], ctx["y"]
    spacing_m, max_gap_m = ctx["spacing_m"], ctx["max_gap_m"]
    pass_col, pass_id, p_label, vec = ctx["pass_col"], ctx["pass_id"], ctx["p_label"], ctx["vec"]

    notes = []

    #  height and its units 
    if d_col:
        d_vals = pd.to_numeric(gdf[d_col], errors="coerce").to_numpy(dtype=float)
        name_u = unit_from_name(d_col)
        data_u, ratio = unit_from_spacing(d_vals, spacing_m)
        # unit in the column name wins; point spacing is used only when the
        # name has no unit, and otherwise just checked
        d_unit = name_u or data_u or "ft"
        src = "from name" if name_u else ("from point spacing" if data_u else "assumed")
        if name_u and data_u and name_u != data_u:
            notes.append(f"'{d_col}' name says {name_u}, but values look like {data_u} "
                         f"compared with point spacing (ratio {ratio:.2f}). Used {name_u}; "
                         f"check the BUF_H_FT values.")
        height_m = d_vals * UNIT_TO_M[d_unit]
        h_label = f"{d_col} ({d_unit}, {src})"
    else:
        d_unit = None
        height_m = compute_spacing(vec)
        h_label = "computed point spacing"

    # Gap limit also tied to the distance column, so a scrambled or sparse
    # point order can never let far-apart points be joined.
    if d_col:
        med_d = np.nanmedian(height_m[height_m > 0]) if (height_m > 0).any() else np.nan
        if np.isfinite(med_d) and spacing_m > 3 * med_d:
            notes.append(f"Points do not look like they are in driving order (median "
                         f"spacing {spacing_m * FT_PER_M:.1f} ft vs median distance "
                         f"{med_d * FT_PER_M:.1f} ft). Few points could be joined; "
                         f"check the order of the input file.")
        if np.isfinite(med_d) and MAX_GAP_FACTOR * med_d < max_gap_m:
            max_gap_m = MAX_GAP_FACTOR * med_d
            if not pass_col:
                step0 = np.r_[0.0, np.hypot(np.diff(x), np.diff(y))]
                pass_id = np.cumsum(step0 > max_gap_m)
                p_label = f"detected from gaps > {max_gap_m * FT_PER_M:.1f} ft"
            vec = neighbor_vectors(x, y, pass_id, max_gap_m)

    #  width and its units 
    w_vals = pd.to_numeric(gdf[w_col], errors="coerce").to_numpy(dtype=float)
    w_unit = unit_from_name(w_col) or d_unit or "ft"
    if not unit_from_name(w_col):
        src = f"same as {d_col}" if d_unit else "assumed"
        notes.append(f"'{w_col}' has no unit in its name; used {w_unit} ({src}).")
    width_m = w_vals * UNIT_TO_M[w_unit]
    w_label = f"{w_col} ({w_unit})"

    #  heading 
    calc_hd = compute_heading(vec)
    if h_col:
        heading = pd.to_numeric(gdf[h_col], errors="coerce").to_numpy(dtype=float)
        n_fill = int((~np.isfinite(heading)).sum())
        heading = np.where(np.isfinite(heading), heading, calc_hd) % 360
        hd_label = f"{h_col}" + (f" ({n_fill:,} missing, computed)" if n_fill else "")
    else:
        heading = calc_hd
        hd_label = "computed from consecutive points"

    do_smooth, do_round = adj in ("2", "3"), adj in ("1", "3")

    heading_raw = heading.copy()
    if do_smooth:
        heading = smooth_heading(heading, vec, SMOOTH_WINDOW)
        hd_label += f", smoothed ({SMOOTH_WINDOW}-point average within pass)"
    if do_round:
        heading = np.round(heading, HEADING_DECIMALS) % 360
        hd_label += f", rounded to {10 ** -HEADING_DECIMALS:g} deg"

    shape = "connected"   # neighbors share edges; rectangles only at sharp turns

    n_capped = int(np.sum(height_m > max_gap_m))
    height_m = np.minimum(height_m, max_gap_m)

    #  validity 
    bad_w = ~(np.isfinite(width_m) & (width_m > 0))
    bad_h = ~(np.isfinite(height_m) & (height_m > 0))
    bad_a = ~np.isfinite(heading)
    valid = ~(bad_w | bad_h | bad_a)
    if valid.sum() == 0:
        raise ToolError("No valid points. Check the selected columns.")

    #  build 
    t0 = time.time()
    n_fallback = 0
    if shape == "connected":
        all_geoms, n_fallback = build_connected(
            x, y, np.nan_to_num(heading), np.nan_to_num(width_m),
            np.nan_to_num(height_m), vec[3], valid)
        geoms = all_geoms[valid]
    else:
        geoms = build_rectangles(x[valid], y[valid], heading[valid],
                                 width_m[valid], height_m[valid], "center")

    # sideways offset column from the monitor (e.g. Y_Offset_f), if present
    num_cols = [c for c in gdf.columns if c != gdf.geometry.name
                and pd.api.types.is_numeric_dtype(gdf[c])]
    off_col = guess_col(num_cols, OFFSET_KEYS)
    offset_m = None
    if off_col:
        off_vals = pd.to_numeric(gdf[off_col], errors="coerce").to_numpy(dtype=float)
        off_unit = unit_from_name(off_col) or w_unit
        offset_m = (off_vals * UNIT_TO_M[off_unit])[valid]

    # move polygons to the cut side (offset column first, overlap test as backup)
    geoms, partial, shift_m, shift_src, sign_note = shift_to_cut_side(
        geoms, heading[valid], width_m[valid], pass_id[valid], offset_m)
    n_partial = int(partial.sum())
    n_by_offset = int((shift_src == "offset").sum())
    n_by_overlap = int((shift_src == "overlap").sum())
    n_centered = int((partial & (shift_src == "")).sum())

    out = gdf.loc[valid].drop(columns=gdf.geometry.name).reset_index(drop=True)
    out["BUF_W_FT"] = np.round(width_m[valid] * FT_PER_M, 3)
    out["PARTIAL"] = partial.astype(int)
    out["SHIFT_FT"] = np.round(shift_m * FT_PER_M, 2)
    out["SHIFT_SRC"] = shift_src
    if shape == "connected":
        # actual length of each polygon along the pass = area / width
        out["BUF_H_FT"] = np.round(shapely.area(geoms) / width_m[valid] * FT_PER_M, 3)
    else:
        out["BUF_H_FT"] = np.round(height_m[valid] * FT_PER_M, 3)
    out["HEAD_DEG"] = np.round(heading[valid], 2)
    if do_smooth or do_round:
        out["HEAD_RAW"] = np.round(heading_raw[valid], 4)
    out["AREA_AC"] = np.round(shapely.area(geoms) / M2_PER_AC, 7)
    ovlp_pct = overlap_with_earlier(geoms, pass_id[valid])
    ovlp_ft2 = ovlp_pct / 100.0 * shapely.area(geoms) * FT_PER_M ** 2
    out["OVLP_PCT"] = np.round(ovlp_pct, 1)
    out["OVLP_FT2"] = np.round(ovlp_ft2, 2)
    # overlap area spread along the rectangle length = equivalent strip width
    # strip width = overlap area / length = OVLP_PCT x width (area = width x length);
    # no division by length, so very short polygons (stopped points) stay exact
    out["OVLP_FT"] = np.round(ovlp_pct / 100.0 * width_m[valid] * FT_PER_M, 2)
    out["OVLP_FLAG"] = (out["OVLP_PCT"] >= OVERLAP_FLAG_PCT).astype(int)
    out = gpd.GeoDataFrame(out, geometry=geoms, crs=gdf.crs)
    t_build = time.time() - t0

    #  summary 
    sum_ac = out["AREA_AC"].sum()
    cov_ac = shapely.union_all(geoms).area / M2_PER_AC
    ovl = 100 * (sum_ac - cov_ac) / sum_ac if sum_ac else 0.0
    nf = int(out["OVLP_FLAG"].sum())

    log("\n" + "-" * 62)
    log("  Summary")
    log("-" * 62)
    log(f"  Points loaded        : {ctx['n_loaded']:,}")
    log(f"  Filter               : {ctx['filt_label']}")
    log(f"  Object ID            : {ctx['oid_label']}")
    log(f"  Passes               : {p_label} ({len(np.unique(pass_id)):,})")
    log(f"  Point spacing        : {spacing_m * FT_PER_M:.2f} ft (median)")
    log(f"  Width                : {w_label}")
    log(f"  Height               : {h_label}")
    log(f"  Heading              : {hd_label}")
    shape_label = ("connected (neighbors share edges)" if shape == "connected"
                   else "rectangle (each point separate)")
    if n_fallback:
        shape_label += f", {n_fallback:,} sharp-turn points used rectangles"
    log(f"  Shape                : {shape_label}")
    if off_col:
        log(f"  Offset column        : {off_col} ({n_by_offset:,} points with a value"
            + (f"; {sign_note}" if sign_note else "") + ")")
    else:
        log("  Offset column        : none found")
    log(f"  Reduced-width points : {n_partial:,}  (shifted by overlap test: "
        f"{n_by_overlap:,}, left centered: {n_centered:,})")
    log(f"  Dropped, no width    : {int(bad_w.sum()):,}")
    log(f"  Dropped, no height   : {int((bad_h & ~bad_w).sum()):,}")
    log(f"  Dropped, no heading  : {int((bad_a & ~bad_w & ~bad_h).sum()):,}")
    if n_capped:
        log(f"  Heights capped at {max_gap_m * FT_PER_M:.1f} ft: {n_capped:,}")
    log(f"  Rectangles           : {len(out):,}  ({t_build:.1f} s)")
    log(f"  Width  min/med/max   : {out.BUF_W_FT.min():.2f} / "
        f"{out.BUF_W_FT.median():.2f} / {out.BUF_W_FT.max():.2f} ft")
    log(f"  Height min/med/max   : {out.BUF_H_FT.min():.2f} / "
        f"{out.BUF_H_FT.median():.2f} / {out.BUF_H_FT.max():.2f} ft")
    log(f"  Sum of areas         : {sum_ac:,.2f} ac")
    log(f"  Covered area         : {cov_ac:,.2f} ac")
    log(f"  Overlap (whole field): {ovl:.1f} %")
    log(f"  Overlap strip, ft    : median {out.OVLP_FT.median():.2f} / "
        f"max {out.OVLP_FT.max():.2f}  (max {out.OVLP_PCT.max():.1f} %)")
    log(f"  Flagged (>= {OVERLAP_FLAG_PCT:g} %)   : {nf:,} ({100 * nf / len(out):.1f} %)")
    for n in notes:
        log(f"  Note: {n}")

    #  export 
    base = os.path.splitext(os.path.basename(ctx["path"]))[0]
    rect_path = os.path.join(out_dir, f"{base}_rect.shp")
    out, renamed = unique_columns(out)
    if renamed:
        log(f"\n  Duplicate column names renamed: {', '.join(renamed)}")
    out.to_crs(ctx["src_crs"]).to_file(rect_path)
    log(f"\n  Saved: {rect_path}")

    return rect_path, dict(
        n_loaded=ctx["n_loaded"], polygons=len(out), sum_ac=float(sum_ac),
        covered_ac=float(cov_ac), overlap_pct=float(ovl), flagged=nf,
        partial=n_partial, dropped=int((~valid).sum()), notes=notes,
        width=w_label, height=h_label, heading=hd_label,
    )


# Main

def main():
    print("=" * 62)
    print("  Rectangular Buffer Tool  |  yield / seeding / fertigation")
    print("=" * 62)

    path = ask("\nPath to point file (.shp, .gpkg, .geojson, .csv)")
    if not os.path.exists(path):
        sys.exit(f"File not found: {path}")

    out_dir = ask("Output folder", os.path.dirname(os.path.abspath(path)))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create output folder: {e}")

    try:
        try:
            ctx = prepare_points(path)
        except ValueError as exc:            # no CRS, or projected CSV columns
            print(f"  {exc}")
            ctx = prepare_points(path, epsg=ask("EPSG code", 4326))

        gdf = ctx["gdf"]
        #  user: three columns 
        w_col = pick_column(gdf, "SWATH WIDTH (rectangle width)", WIDTH_KEYS)
        if w_col is None:
            sys.exit("A swath width column is required.")
        d_col = pick_column(gdf, "DISTANCE (rectangle height), 0 = compute from points",
                            DIST_KEYS, allow_none=True)
        h_col = pick_column(gdf, "HEADING (rectangle angle), 0 = compute from points",
                            HEAD_KEYS, allow_none=True)

        adj = ask("\nHeading adjustment: 0 = none, 1 = round, 2 = smooth, "
                  "3 = smooth + round", 0)
        while adj not in ("0", "1", "2", "3"):
            adj = ask("  Enter 0, 1, 2 or 3", 0)

        make_coverage(ctx, w_col, d_col, h_col, adj, out_dir)
    except ToolError as exc:
        sys.exit(str(exc))
    print("Done.")


# --- ADMA EDIT 3 -----------------------------------------------------------
# The entry point ADMA's worker calls.

HEADING_ADJUSTMENTS = {
    "none": ("0", "Use the headings as recorded"),
    "round": ("1", f"Round to the nearest {10 ** -HEADING_DECIMALS:g} degree"),
    "smooth": ("2", f"Average over {SMOOTH_WINDOW} points within each pass"),
    "smooth_round": ("3", "Smooth, then round"),
}


def numeric_columns(gdf):
    return [c for c in gdf.columns if c != gdf.geometry.name
            and pd.api.types.is_numeric_dtype(gdf[c])]


def suggest_columns(gdf):
    """The columns main() would offer as defaults, so a form can preselect them."""
    cols = numeric_columns(gdf)
    return {
        "width": guess_col(cols, WIDTH_KEYS),
        "distance": guess_col(cols, DIST_KEYS),
        "heading": guess_col(cols, HEAD_KEYS),
        "offset": guess_col(cols, OFFSET_KEYS),
        "pass": guess_col([c for c in gdf.columns if c != gdf.geometry.name], PASS_KEYS),
    }


AUTO = "auto"


def process_point_to_polygon(input_path, output_dir, width_col=AUTO,
                             distance_col=AUTO, heading_col=AUTO,
                             heading_adjust="none", epsg=None):
    """
    Run the tool without prompts and report the way every ADMA tool does:
    (success, message, output_files).

    A column given as AUTO gets the default main() would suggest -- what a
    user pressing Enter at the prompt gets. For distance and heading, None
    means "compute from the points", which is choosing 0 at the prompt. The
    two have to stay distinct: a form that offers "compute from points" must
    not have that quietly replaced by the suggested column.
    """
    if heading_adjust not in HEADING_ADJUSTMENTS:
        return False, (f"Unknown heading adjustment {heading_adjust!r}. Choose one of: "
                       f"{', '.join(HEADING_ADJUSTMENTS)}."), {}
    if not os.path.exists(input_path):
        return False, f"Input file not found: {os.path.basename(input_path)}", {}

    log_lines = []
    try:
        ctx = prepare_points(input_path, log=log_lines.append, epsg=epsg)
    except (ToolError, ValueError) as exc:
        return False, str(exc), {}
    except Exception as exc:
        return False, f"Could not read the point file: {exc}", {}

    gdf = ctx["gdf"]
    numeric = numeric_columns(gdf)
    guessed = suggest_columns(gdf)

    for label, col in (("Swath width", width_col), ("Distance", distance_col),
                       ("Heading", heading_col)):
        if col and col != AUTO and col not in numeric:
            return False, (f"{label} column {col!r} is not a numeric column of this file. "
                           f"Numeric columns: {', '.join(numeric) or 'none'}."), {}

    if width_col in (AUTO, None, ""):
        width_col = guessed["width"]
    if not width_col:
        return False, ("Could not tell which column holds the swath width. Choose it "
                       f"from: {', '.join(numeric) or 'no numeric columns'}."), {}
    distance_col = guessed["distance"] if distance_col == AUTO else (distance_col or None)
    heading_col = guessed["heading"] if heading_col == AUTO else (heading_col or None)

    os.makedirs(output_dir, exist_ok=True)
    try:
        rect_path, s = make_coverage(ctx, width_col, distance_col, heading_col,
                                     HEADING_ADJUSTMENTS[heading_adjust][0],
                                     output_dir, log=log_lines.append)
    except ToolError as exc:
        return False, str(exc), {}

    base = os.path.splitext(rect_path)[0]
    output_files = {
        "coverage_components": [
            base + ext for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")
            if os.path.exists(base + ext)
        ],
    }

    message = (
        f"{s['polygons']:,} coverage polygons from {s['n_loaded']:,} points: "
        f"{s['covered_ac']:,.2f} ac covered ({s['sum_ac']:,.2f} ac summed, "
        f"{s['overlap_pct']:.1f}% overlap); {s['flagged']:,} flagged at "
        f">= {OVERLAP_FLAG_PCT:g}% overlap, {s['partial']:,} reduced-width. "
        f"Width {s['width']}; distance {s['height']}; heading {s['heading']}."
    )
    if s["dropped"]:
        message += f" {s['dropped']:,} point(s) dropped for a missing width, distance or heading."
    if s["notes"]:
        message += " " + " ".join(s["notes"])
    return True, message, output_files


if __name__ == "__main__":
    main()
