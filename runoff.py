import sys
import os
import re
import glob
import numpy as np
import pandas as pd
import xarray as xr
import geopandas as gp
from shapely.geometry import Point, Polygon
from argparse import ArgumentParser
import fiona

fiona.drvsupport.supported_drivers["LIBKML"] = "rw" # https://gis.stackexchange.com/a/258370/609

class runoff(object):
    """
    Access the freshwater runoff database

    Parameters
    ----------
    base: Path to dataset folder
    roi: Region of interest (point or ring).
       If point: Accesses nearest outlet
       If ring: Accesses all outlets within ring
    upstream: If True, include all upstream ice outlets for any land outlet

    Outputs
    --------
    Returns GeoPandas GeoDataFrame if outlets() called
    Returns xarray Dataset if runoff() called
    """

    def __init__(self, base=None, roi=None, upstream=False, quiet=True):
        if roi is None:
            print("Error: Not initialized with ROI")
            return
        self._roi = roi
        if base is None:
            print("Error: Not initialized with 'base' folder")
            return
        self._base = base
        self._quiet = quiet
        self._upstream = upstream
        
        self.msg("Using '%s' as base folder" % self._base)

        # These will hold results
        self._outlets = {}     # pandas geodataframes of outlets and basins
        self._outlets_u = {}   # upsteam
        self._runoff = {}      # pandas table of runoff per outlet
        self._runoff_u = {}    # upstream
        
        # populate with keys
        for key in ["land", "ice"]: self._outlets[key] = None
        for key in [_ for _ in self._outlets.keys() if 'ice' in _]: self._outlets_u[key] = None
        for rcm in ["MAR", "RACMO"]: 
            for key in self._outlets.keys(): 
                self._runoff[rcm + '_' + key] = None
            for key in [_ for _ in self._outlets.keys() if 'ice' in _]: 
                self._runoff_u[rcm + "_" + key] = None


    def msg(self, *args, **kwargs):
        if not self._quiet:
            print(*args, file=sys.stderr, **kwargs)


    def outlets(self):
        """Load outlets and subset to ROI. 
        Optional end-point: Return this object to end-user."""
        self.msg("Loading outlets and basins...")
        for key in self._outlets.keys():
                self.msg("    Loading %s" % key)
                self._outlets[key] \
                    = (gp.read_file(self._base + "/" + key + "/outlets.gpkg").set_index("cat"))\
                    .merge(gp.read_file(self._base + "/" + key + "/basins_filled.gpkg")\
                           .set_index("cat"), left_index=True, right_index=True)\
                    .rename(columns={"geometry_x":"outlet", "geometry_y":"basin"})
        self.subset_to_ROI()

        # Return datastructure
        # Merge all dataframes with new columns (domain, k, upstream) to distinguish them
        o = self._outlets["land"].reset_index()
        # o["domain"] = "land"; o["k"] = 100; o["upstream"] = False
        o["domain"] = "land"; o["upstream"] = False
        for key in [_ for _ in self._outlets.keys() if 'ice' in _]:
            if self._outlets[key] is not None:
                # d,k = key.split("_"); k=int(k)
                otmp = self._outlets[key].reset_index()
                # otmp["domain"], otmp["k"], otmp["upstream"] = d,k,False
                otmp["domain"], otmp["upstream"] = key,False
                o = o.append(otmp)
        if self._upstream: 
            for key in self._outlets_u.keys():
                if self._outlets_u[key] is not None:
                    # d,k = key.split("_")
                    otmp = self._outlets_u[key].reset_index()
                    # otmp["domain"], otmp["k"], otmp["upstream"] = d,k,True
                    otmp["domain"], otmp["upstream"] = key,True
                    o = o.append(otmp)
        o['coast_id'] = o['coast_id'].fillna(-1).astype(np.int)
        o['coast_x'] = o['coast_x'].fillna(-1).astype(np.int)
        o['coast_y'] = o['coast_y'].fillna(-1).astype(np.int)

        o = o.reset_index().drop(columns="index").rename(columns={"cat":"id"})
        o.index.name = "index"
        o = gp.GeoDataFrame(o, crs="EPSG:3413")
        return o


    def RCM(self):
        """Load runoff within ROI. Return this object to the end-user."""
        self.msg("Loading runoff data...")
        for key in self._runoff.keys():
            # r,d,k = key.split("_")
            r,d = key.split("_")
            self.msg("    Loading %s" % key)
            # file_list = self._base + "/" + d+"_"+k + "/runoff/" + r + "_*.nc"
            file_list = self._base + "/" + d + "/runoff/" + r + "_*.nc"
            # load all runoff at all outlets
            self._runoff[key] = xr.open_mfdataset(file_list, combine="by_coords").rename({"runoff": key})

        self.outlets()           # load outlets, and subset them to ROI
        self.runoff_at_outlets() # subset runoff to these outlets (also populate _runoff_u)

        # Return datastructure: Initialize with land and MAR
        key="MAR_land"; geo_key='_'.join(key.split("_")[1:])
        rtmp = self._runoff[key]; rtmp.columns.name = geo_key
        r = xr.DataArray(rtmp.values, dims=('time',geo_key), 
                         coords={'time':rtmp.index, geo_key:rtmp.columns}).to_dataset(name=key)
        for key in self._runoff.keys():
            if np.size(self._runoff[key]) == 0: continue
            geo_key='_'.join(key.split("_")[1:])
            rtmp = self._runoff[key]; rtmp.columns.name = geo_key
            rtmp = xr.DataArray(rtmp.values, dims=('time',geo_key), 
                                coords={'time':rtmp.index, geo_key:rtmp.columns})\
                     .to_dataset(name=key)
            r = r.merge(rtmp)
        if self._upstream:
            for key in self._runoff_u.keys():
                if np.size(self._runoff_u[key]) == 0: continue
                rtmp = self._runoff_u[key]
                key = key + "_upstream"
                geo_key='_'.join(key.split("_")[1:])
                rtmp.columns.name = geo_key
                rtmp = xr.DataArray(rtmp.values, dims=('time',geo_key), 
                                    coords={'time':rtmp.index, geo_key:rtmp.columns})\
                         .to_dataset(name=key)
                r = r.merge(rtmp)
        return r


    def subset_to_ROI(self):
        self.msg("Subsetting data by ROI...")
        geom = self.parse_ROI()

        # first subset just land
        if (geom[0].geom_type == "Point"):
            self.msg("ROI is point... finding basins that contain point")
            self._outlets["land"] \
                = self._outlets["land"]\
                      .iloc[[_.contains(geom[0]) for _ in  self._outlets["land"]["basin"]]]
        elif (geom[0].geom_type == "Polygon"):
            self.msg("ROI is geometry... finding all points inside geometry")
            self._outlets["land"] \
                = self._outlets["land"]\
                      .iloc[[_.within(geom[0]) for _ in self._outlets["land"]["outlet"]]]

        if self._upstream: # use full dataset before subsetting to find upstream basins
              self.msg("    Finding basins upstream of land basins within ROI")
              for key in self._outlets_u.keys():
                  self.msg("        %s" % key)
                  self._outlets_u[key] \
                      = (self._outlets[key][self._outlets[key]['coast_id']\
                                            .isin(self._outlets["land"].index)])
              
        # now subset ice
        if (geom[0].geom_type == "Point"):
            for key in [_ for _ in self._outlets.keys() if 'ice' in _]:
                self._outlets[key]\
                    = self._outlets[key]\
                          .iloc[[_.contains(geom[0]) for _ in self._outlets[key]["basin"]]]
        elif (geom[0].geom_type == "Polygon"):
            for key in [_ for _ in self._outlets.keys() if 'ice' in _]:
                self._outlets[key] \
                    = self._outlets[key]\
                          .iloc[[_.within(geom[0]) for _ in self._outlets[key]["outlet"]]]

        # clean up duplicates. Code works w/ multi-index
        for key in self._outlets: 
            if self._outlets[key] is not None: 
                self._outlets[key] \
                    = self._outlets[key].groupby(level=self._outlets[key].index.names).first()
        for key in self._outlets_u: 
            if self._outlets_u[key] is not None: 
                self._outlets_u[key] \
                    = self._outlets_u[key].groupby(level=self._outlets_u[key].index.names).first()
                
            
    def parse_ROI(self):
        """ 
        ROI should be string to geometry file (KML, Geopackage, etc.), or string for coordinates.
        If coordinates, can be "lon,lat" for a point, or "lon1,lat1 lon2,lat2 ... lon_n,lat_n" 
        for boundary in EPSG:4326 coordinates, or x,y or x1,y1 x2,y2 ... xn,yn for point or 
        boundary in EPSG:3413 coordinates.

        Geometry file should contain only 1 geometry.
        Coordinate boundaries will be closed via convex hull if not closed.
        """

        self.msg("Parsing ROI...")
        roi = self._roi
        regex = re.compile(".*[a-zA-Z].*")
        if regex.match(roi): # filename, contains letters
            self.msg("    ROI appears to be filename")
            self.msg("    Loading as GeoSeries: ", roi)
            gdf = gp.read_file(roi)
            self.msg("    Converting to EPSG:3413")
            gs = gdf.to_crs("EPSG:3413")["geometry"]
            self.msg("    Adding convex hull to geometry")
            gs = gs.convex_hull
            if gs.shape[0] != 1:
                self.msg("Error: Multiple geometries in ", roi)
        else: # "x,y" OR "x,y x,y ..." or "lon,lat" or "lon,lat lon,lat"
            roi_x_or_lon = np.array([_.split(",")[0] for _ in roi.split(" ")]).astype(np.float)
            roi_y_or_lat = np.array([_.split(",")[1] for _ in roi.split(" ")]).astype(np.float)
            if all((roi_x_or_lon > -360) & (roi_x_or_lon < 360) & \
                   (roi_y_or_lat > -90) & (roi_y_or_lat < 90)):
                if (roi_x_or_lon.size == 1):
                    self.msg("    ROI appears to be point in EPSG:4326 coordinates")
                    if(roi_x_or_lon > 10):
                        print("Warning: Longitude > 10. Should probably be negative?")
                    gs = gp.GeoSeries(data=Point(roi_x_or_lon,roi_y_or_lat), \
                                      crs="EPSG:4326").to_crs("EPSG:3413")
                else:
                    self.msg("    ROI appears to be boundary (from points) in EPSG:4326 coordinates")
                    gs = gp.GeoSeries(data=Polygon(zip(roi_x_or_lon,roi_y_or_lat)), \
                                      crs="EPSG:4326").to_crs("EPSG:3413")
            else:
                if (roi_x_or_lon.size == 1):
                    self.msg("    ROI appears to be point. Assuming EPSG:3413 coordinates")
                    gs = gp.GeoSeries(data=Point(roi_x_or_lon,roi_y_or_lat), crs="EPSG:3413")
                else:
                    self.msg("    ROI appears to be boundary (from points)")
                    self.msg("    Assuming EPSG:3413 coordinates")
                    gs = gp.GeoSeries(data=Polygon(zip(roi_x_or_lon,roi_y_or_lat)), crs="EPSG:3413")
        return gs

        
    def runoff_at_outlets(self):
        if self._upstream:
             self.msg("Selecting upstream runoff at outlets...")
             for key in self._runoff_u.keys():
                 self.msg("    Selecting from: %s" % key)
                 out_key = '_'.join(key.split("_")[1:])
                 self._runoff_u[key] \
                     = self._runoff[key]\
                           .sel({'station': self._outlets_u[out_key].index.values}, drop=True)[key]\
                           .to_dataframe()\
                           .reset_index()\
                           .pivot_table(index="time", columns="station", values=key)
                 
        self.msg("Selecting runoff at outlets...")
        for key in self._runoff.keys():
            self.msg("    Selecting from: %s" % key)
            out_key = '_'.join(key.split("_")[1:])
            self._runoff[key] \
                = self._runoff[key]\
                      .sel({'station': self._outlets[out_key].index.values}, drop=True)[key]\
                      .to_dataframe()\
                      .reset_index()\
                      .pivot_table(index="time", columns="station", values=key)

            
def parse_arguments():
    parser = ArgumentParser(description="Runoff data access")

    parser.add_argument("--base", type=str, default="./freshwater", required=True,
                        help="Folder containing freshwater data")
    parser.add_argument("--roi", required=True, type=str, 
                        help="x,y OR lon,lat OR x0,y0 x1,y1 ... xn,yn " + \
                        "OR lon0,lat0 lon1,lat1 ... lon_n,lat_n. [lon: degrees E]")
    parser.add_argument("-u", "--upstream", action='store_true', 
                        help="Include upstream ice outlets draining into land basins")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-o", "--outlets", action='store_true', default=False,
                       help="Return outlet IDs (same as basin IDs)")
    group.add_argument("-r", "--RCM", action='store_true', default=False,
                       help="Return RCM runoff for each domain (outlets merged)")
    parser.add_argument("-q", "--quiet", action='store_true', help="Be quiet")
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    """Executed from the command line"""
    args = parse_arguments()
    r = runoff(base=args.base, roi=args.roi, upstream=args.upstream, quiet=args.quiet)
    if args.outlets:
        df = r.outlets()
        print(df.drop(columns=["outlet","basin"]).to_csv(float_format='%.3f'))
    elif args.RCM:
        ds = r.RCM()
        d = [_ for _ in ds.dims.keys() if _ != 'time'] # merge outlets by domain
        print(ds.sum(dim=d).to_dataframe().to_csv(float_format='%.6f'))

else:
    """Executed on import"""
    pass
