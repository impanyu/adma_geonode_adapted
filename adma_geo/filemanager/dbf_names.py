"""
Column names a shapefile can actually hold.

A shapefile's attributes live in a .dbf, which caps field names at 10
characters and truncates anything longer without complaint. Two columns whose
first ten characters match therefore become one, and which one survives is not
defined -- so 'soil_organic_carbon' and 'soil_organic_matter' silently turn
into a single 'soil_organ'. Recent geopandas refuses the write outright rather
than lose a column, which turns the silent loss into a crash instead.

Both failures come from the same missing step: shorten, then make unique.
"""
DBF_FIELD_LIMIT = 10


def unique_dbf_name(label, taken):
    """
    A <=10 character version of ``label`` not already in ``taken``.

    ``taken`` is updated, so repeated calls build a set of distinct names.
    """
    base = str(label)[:DBF_FIELD_LIMIT]
    if base not in taken:
        taken.add(base)
        return base

    for n in range(1, 1000):
        suffix = str(n)
        candidate = f'{base[:DBF_FIELD_LIMIT - len(suffix)]}{suffix}'
        if candidate not in taken:
            taken.add(candidate)
            return candidate

    raise ValueError(f'Cannot build a unique column name for {label!r}')


def dbf_safe_columns(frame):
    """Rename a GeoDataFrame's attribute columns so a shapefile can hold them."""
    geometry_name = frame.geometry.name if frame.geometry is not None else None
    taken = set()
    renames = {}
    for column in frame.columns:
        if column == geometry_name:
            continue
        safe = unique_dbf_name(column, taken)
        if safe != column:
            renames[column] = safe
    return frame.rename(columns=renames) if renames else frame
