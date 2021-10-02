"""Command-line interface for PyDaymet."""
from pathlib import Path
from typing import List, Optional, Union

import click
import geopandas as gpd
import pandas as pd
import shapely.geometry as sgeom
from shapely.geometry import MultiPolygon, Point, Polygon

from . import pydaymet as daymet
from .exceptions import InvalidInputRange, InvalidInputType, MissingCRS, MissingItems


def get_target_df(
    tdf: Union[pd.DataFrame, gpd.GeoDataFrame], req_cols: List[str]
) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
    """Check if all required columns exists in the dataframe.

    It also re-orders the columns based on req_cols order.
    """
    missing = [c for c in req_cols if c not in tdf]
    if len(missing) > 0:
        raise MissingItems(missing)
    return tdf[req_cols]


def _get_region(gid: str, geom: Union[Polygon, MultiPolygon, Point]) -> str:
    """Get the Daymer region of an input geometry (point or polygon)."""
    region_bbox = {
        "na": sgeom.box(-136.8989, 6.0761, -6.1376, 69.077),
        "hi": sgeom.box(-160.3055, 17.9539, -154.7715, 23.5186),
        "pr": sgeom.box(-67.9927, 16.8443, -64.1195, 19.9381),
    }
    for region, bbox in region_bbox.items():
        if bbox.contains(geom):
            return region
    msg = f"Input location with ID of {gid} is outside the Daymet spatial range."
    raise InvalidInputRange(msg)


def get_region(geodf: gpd.GeoDataFrame) -> List[str]:
    """Get the Daymer region of a geo-dataframe."""
    return [
        _get_region(i, p) for i, p in geodf[["id", "geometry"]].itertuples(index=False, name=None)
    ]


variables = click.option(
    "--variables",
    "-v",
    multiple=True,
    default=["prcp"],
    help="Target variables. You can pass this flag multiple times for multiple variables.",
)

time_scale = click.option(
    "-t",
    "--time_scale",
    type=click.Choice(["daily", "monthly", "annual"], case_sensitive=False),
    default="daily",
    help="Target time scale.",
)

pet = click.option(
    "-p",
    "--pet",
    type=click.Choice(
        ["penman_monteith", "hargreaves_samani", "priestley_taylor", "none"], case_sensitive=True
    ),
    default="none",
    help="Compute PET.",
)

save_dir = click.option(
    "-s",
    "--save_dir",
    type=click.Path(exists=False),
    default="clm_daymet",
    help="Path to a directory to save the requested files. "
    + "Extension for the outputs is .nc for geometry and .csv for coords.",
)


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
def cli():
    """Command-line interface for PyDaymet."""


@cli.command("coords", context_settings=CONTEXT_SETTINGS)
@click.argument("fpath", type=click.Path(exists=True))
@variables
@time_scale
@pet
@save_dir
def coords(
    fpath: Path,
    variables: Optional[Union[List[str], str]] = None,
    time_scale: str = "daily",
    pet: str = "none",
    save_dir: Union[str, Path] = "clm_daymet",
):
    """Retrieve climate data for a list of coordinates.

    \b
    FPATH: Path to a csv file with four columns:
        - ``id``: Feature identifiers that daymet uses as the output netcdf filenames.
        - ``start``: Start time.
        - ``end``: End time.
        - ``lon``: Longitude of the points of interest.
        - ``lat``: Latitude of the points of interest.

    \b
    Examples:
        $ cat coords.csv
        id,lon,lat,start,end
        california,-122.2493328,37.8122894,2012-01-01,2014-12-31
        $ pydaymet coords coords.csv -v prcp -v tmin -p hargreaves_samani
    """  # noqa: D301
    fpath = Path(fpath)
    if fpath.suffix != ".csv":
        raise InvalidInputType("file", ".csv")

    _pet = None if pet == "none" else pet

    target_df = get_target_df(pd.read_csv(fpath), ["id", "start", "end", "lon", "lat"])
    points = gpd.GeoDataFrame(
        target_df.id, geometry=gpd.points_from_xy(target_df.lon, target_df.lat), crs="epsg:4326"
    )
    target_df["region"] = get_region(points)
    target_df["dates"] = list(target_df[["start", "end"]].itertuples(index=False, name=None))
    target_df["coords"] = list(target_df[["lon", "lat"]].itertuples(index=False, name=None))
    target_df = target_df[["id", "coords", "dates", "region"]]

    count = "1 point" if len(target_df) == 1 else f"{len(target_df)} points"
    click.echo(f"Found coordinates of {count} in {fpath.resolve()}.")

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    with click.progressbar(
        target_df.itertuples(index=False, name=None),
        label="Getting single-pixel climate data",
        length=len(target_df),
    ) as bar:
        for i, coords, dates, region in bar:
            fname = Path(save_dir, f"{i}.csv")
            if fname.exists():
                continue
            clm = daymet.get_bycoords(
                coords, dates, region=region, variables=variables, time_scale=time_scale, pet=_pet
            )
            clm.to_csv(fname, index=False)


@cli.command("geometry", context_settings=CONTEXT_SETTINGS)
@click.argument("fpath", type=click.Path(exists=True))
@variables
@time_scale
@pet
@save_dir
def geometry(
    fpath: Path,
    variables: Optional[Union[List[str], str]] = None,
    time_scale: str = "daily",
    pet: str = "none",
    save_dir: Union[str, Path] = "clm_daymet",
):
    """Retrieve climate data for a dataframe of geometries.

    \b
    FPATH: Path to a shapefile (.shp) or geopackage (.gpkg) file.
    This file must have four columns and contain a ``crs`` attribute:
        - ``id``: Feature identifiers that daymet uses as the output netcdf filenames.
        - ``start``: Start time.
        - ``end``: End time.
        - ``geometry``: geometries of regions of interest.

    \b
    Examples:
        $ pydaymet geometry geo.gpkg -v prcp -v tmin -p hargreaves_samani
    """  # noqa: D301
    fpath = Path(fpath)
    if fpath.suffix not in (".shp", ".gpkg"):
        raise InvalidInputType("file", ".shp or .gpkg")

    target_df = gpd.read_file(fpath)
    if target_df.crs is None:
        raise MissingCRS
    target_df = target_df.to_crs("epsg:4326")

    target_df = get_target_df(target_df, ["id", "start", "end", "geometry"])
    target_df["region"] = get_region(target_df)
    target_df["dates"] = list(target_df[["start", "end"]].itertuples(index=False, name=None))
    target_df = target_df[["id", "geometry", "dates", "region"]]

    count = "1 geometry" if len(target_df) == 1 else f"{len(target_df)} geometries"
    click.echo(f"Found {count} in {fpath.resolve()}.")

    _pet = None if pet == "none" else pet
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    with click.progressbar(
        target_df.itertuples(index=False, name=None),
        label="Getting gridded climate data",
        length=len(target_df),
    ) as bar:
        for i, geometry, dates, region in bar:
            fname = Path(save_dir, f"{i}.nc")
            if fname.exists():
                continue
            clm = daymet.get_bygeom(
                geometry, dates, region=region, variables=variables, time_scale=time_scale, pet=_pet
            )
            clm.to_netcdf(fname)
