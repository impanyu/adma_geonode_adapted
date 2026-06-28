import geopandas as gpd
from shapely.geometry import Point
from django.test import SimpleTestCase

from filemanager.ValidYieldExtractorTool import (
    resolve_plot_id_column,
    apply_column_override,
)


def _points_gdf(**cols):
    n = len(next(iter(cols.values())))
    geom = [Point(i, i) for i in range(n)]
    return gpd.GeoDataFrame({**cols, "geometry": geom}, crs="EPSG:4326")


class ResolvePlotIdColumnTests(SimpleTestCase):
    def test_override_used_even_when_not_a_known_synonym(self):
        plots = _points_gdf(Weird_Name=["a", "b"])
        self.assertEqual(resolve_plot_id_column(plots, "Weird_Name"), "Weird_Name")

    def test_falls_back_to_autodetect_when_override_empty(self):
        plots = _points_gdf(Plot_Number=["1", "2"])
        self.assertEqual(resolve_plot_id_column(plots, None), "Plot_Number")

    def test_missing_override_column_raises_valueerror(self):
        plots = _points_gdf(Plot_Number=["1", "2"])
        with self.assertRaises(ValueError) as ctx:
            resolve_plot_id_column(plots, "Nope")
        self.assertIn("Nope", str(ctx.exception))


class ApplyColumnOverrideTests(SimpleTestCase):
    def test_override_copies_chosen_column_to_std_key(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        out = apply_column_override(gdf, "applied", "Rate_Actual")
        self.assertEqual(list(out["applied"]), [10.0, 20.0])

    def test_noop_when_override_is_falsy(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        out = apply_column_override(gdf, "applied", None)
        self.assertNotIn("applied", out.columns)

    def test_missing_override_column_raises_valueerror(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        with self.assertRaises(ValueError) as ctx:
            apply_column_override(gdf, "applied", "Missing")
        self.assertIn("Missing", str(ctx.exception))
