# necessary basic libraries
import glob
import os
import random
import shutil
from pathlib import Path
import numpy as np
import cv2
import json
import rasterio
import geopandas
from geopandas.tools import sjoin
from rasterio.mask import mask
from rasterio.io import DatasetReader
from shapely.geometry import box
import geopandas as gpd
from fiona.crs import from_epsg

# class img_data(DatasetReader):
#    """
#    Class for image data to be processed for tiling
#    """
#
#    def __init__(self):
#        self.x_origin = self.bounds[0]
#        self.y_origin = self.bounds[3]
#        self.pixelSizeX = self.affine[0]
#        self.pixelSizeY = -self.affine[4]
#


def getFeatures(gdf):
  """
    Function to parse features from GeoDataFrame in such a manner that rasterio wants them
    """
  return [json.loads(gdf.to_json())["features"][0]["geometry"]]


def tile_data(data,
              out_dir,
              buffer=30,
              tile_width=200,
              tile_height=200,
              crowns=None):
  """
    Function to tile up image and (if included) corresponding crowns
    """
  for minx in np.arange(data.bounds[0], data.bounds[2] - tile_width, tile_width,
                        int):
    # print("minx:", minx)
    for miny in np.arange(data.bounds[1], data.bounds[3] - tile_height,
                          tile_height, int):
      # print("miny:", miny)

      # new tiling bbox including the buffer
      bbox = box(
          minx - buffer,
          miny - buffer,
          minx + tile_width + buffer,
          miny + tile_height + buffer,
      )
      # define the bounding box of the tile, excluding the buffer (hence selecting just the central part of the tile)
      bbox_central = box(minx, miny, minx + tile_width, miny + tile_height)

      # turn the bounding boxes into geopandas DataFrames
      geo = gpd.GeoDataFrame({"geometry": bbox}, index=[0], crs=from_epsg(4326))
      geo_central = gpd.GeoDataFrame({"geometry": bbox_central},
                                     index=[0],
                                     crs=from_epsg(4326))    # 3182

      # here we are cropping the tiff to the bounding box of the tile we want
      coords = getFeatures(geo)
      # print("Coords:", coords)

      # define the tile as a mask of the whole tiff with just the bounding box
      out_img, out_transform = mask(data, shapes=coords, crop=True)
      # print('out transform:', out_transform)

      # This can be useful when reprojecting later as know the crs format to put it into
      # epsg_code = int(data.crs.data["init"][5:])
      # print(epsg_code)

      # copy the metadata then update it, the "nodata" and "dtype" where important as made larger
      # tifs have outputted tiles which were not just black
      out_meta = data.meta.copy()
      out_meta.update({
          "driver": "GTiff",
          "height": out_img.shape[1],
          "width": out_img.shape[2],
          "transform": out_transform,
          "nodata": None,
          "dtype": "uint8",
      })
      # print('Out Meta:',out_meta)

      # Saving the tile as a new tiff, named by the origin of the tile. If tile appears blank in folder can show the image here and may
      # need to fix RGB data or the dtype
      # show(out_img)

      out_tif = out_dir + "/tile_" + str(minx) + "_" + str(miny) + ".tif"
      with rasterio.open(out_tif, "w", **out_meta) as dest:
        dest.write(out_img)

      # read in the tile we have just saved
      clipped = rasterio.open(out_dir + "/tile_" + str(minx) + "_" + str(miny) +
                              ".tif")
      # read it as an array
      # show(clipped)
      arr = clipped.read()

      # each band of the tiled tiff is a colour!
      R = arr[0]
      G = arr[1]
      B = arr[2]

      # stack up the bands in an order appropriate for saving with cv2, then rescale to the correct 0-255 range for cv2

      rgb = np.dstack((B, G, R))    # BGR for cv2
      rgb_rescaled = rgb    # scale to image
      # print('rgb rescaled', rgb_rescaled)

      # save this as jpg or png...we are going for png...again, named with the origin of the specific tile
      # here as a naughty method
      cv2.imwrite(
          out_dir + "tile_" + str(minx) + "_" + str(miny) + ".png",
          rgb_rescaled,
      )

      # img = cv2.imread(
      #    "gdrive/MyDrive/JamesHirst/NY/LargeArea_images/naip_cayuga/naip_cayuga_tiled_by_me/tile_"
      #    + str(minx)
      #    + "_"
      #    + str(miny)
      #    + ".png"
      # )
      # print('png shape:', img.shape)
      if crowns is not None:
        # select the crowns that intersect the non-buffered central
        # section of the tile using the inner join
        # JB : a better solution would be to clip crowns to tile extent
        overlapping_crowns = sjoin(crowns, geo_central, how="inner")
        # Maybe left join to keep information of crowns?
        overlapping_crowns = overlapping_crowns.explode(index_parts=True)
        # print("Overlapping crowns:", overlapping_crowns)

        # translate to 0,0 to overlay on png
        # this now works as a universal approach.
        if minx == data.bounds[0] and miny == data.bounds[1]:
          # print("We are in the bottom left!")
          moved = overlapping_crowns.translate(-minx, -miny)
        elif miny == data.bounds[1]:
          # print("We are on the bottom, but not bottom left")
          moved = overlapping_crowns.translate(-minx + buffer, -miny)
        elif minx == data.bounds[0]:
          # print("We are along the left hand side, but not bottom left!")
          moved = overlapping_crowns.translate(-minx, -miny + buffer)
        else:
          # print("We are in the middle!")
          moved = overlapping_crowns.translate(-minx + buffer, -miny + buffer)
        # print("Moved coords:", moved)

        # scale to deal with the resolution
        scalingx = 1 / (data.transform[0])
        scalingy = -1 / (data.transform[4])
        moved_scaled = moved.scale(scalingx, scalingy, origin=(0, 0))
        # print(moved_scaled)
        impath = {
            "imagePath":
                (out_dir + "tile_" + str(minx) + "_" + str(miny) + ".png")
        }

        # save as a geojson, a format compatible with detectron2, again named by the origin of the tile.
        # If the box selected from the image is outside of the mapped region due to the image being on a slant
        # then the shp file will have no info on the crowns and hence will create an empty gpd Dataframe.
        # this causes an error so skip creating geojson. The training code will also ignore png so no problem.
        try:
          filename = "./tile_" + str(minx) + "_" + str(miny) + ".geojson"
          # Try this to keep columns
          # moved_scaled = overlapping_crowns.set_geometry(moved_scaled)
          moved_scaled.to_file(
              driver="GeoJSON",
              filename=filename,
          )
          with open(filename, "r") as f:
            shp = json.load(f)
            shp.update(impath)
          with open(filename, "w") as f:
            json.dump(shp, f)
        except:
          print("ValueError: Cannot write empty DataFrame to file.")
          continue
         
def record_data(crowns, 
                out_dir,
                column='status'):
  """Function that will record a list of classes into a file that can be readed during training."""
  
  list_of_classes = crowns[column].unique().tolist()
  
  #write it into file "classes.txt"
  out_tif = out_dir + 'classes.txt'
  f = open(out_tif, "w")
  for i in list_of_classes:
    f.write("%s\n" % i)
  f.close()

def tile_data_train(data,
                    out_dir,
                    buffer=30,
                    tile_width=200,
                    tile_height=200,
                    crowns=None,
                    threshold=0):
  """
    Function to tile up image and (if included) corresponding crowns.
    Only outputs tiles with crowns in.
    """
  # Should clip data to crowns straight off to speed things up
  os.makedirs(out_dir, exist_ok=True)
  # More efficient if we could clip to crowns immediately...
  #out_img, out_transform = mask(data, shapes=crowns.buffer(buffer), crop=True)
  for minx in np.arange(data.bounds[0], data.bounds[2] - tile_width, tile_width,
                        int):
    # print("minx:", minx)
    for miny in np.arange(data.bounds[1], data.bounds[3] - tile_height,
                          tile_height, int):
      # print("miny:", miny)
      # Naming conventions
      tilename = Path(data.name).stem
      out_path = out_dir + tilename + "_" + str(minx) + "_" + str(
          miny) + "_" + str(tile_width) + "_" + str(buffer)
      # new tiling bbox including the buffer
      bbox = box(
          minx - buffer,
          miny - buffer,
          minx + tile_width + buffer,
          miny + tile_height + buffer,
      )
      # define the bounding box of the tile, excluding the buffer (hence selecting just the central part of the tile)
      #bbox_central = box(minx, miny, minx + tile_width, miny + tile_height)

      # turn the bounding boxes into geopandas DataFrames
      geo = gpd.GeoDataFrame({"geometry": bbox}, index=[0], crs=from_epsg(4326))
      # geo_central = gpd.GeoDataFrame(
      #    {"geometry": bbox_central}, index=[0], crs=from_epsg(4326)
      # )  # 3182
      # overlapping_crowns = sjoin(crowns, geo_central, how="inner")

      # skip forward if there are no crowns in a tile
      # overlapping_crowns = sjoin(crowns, geo, predicate="within", how="inner")
      overlapping_crowns = gpd.clip(crowns, geo)
      # Discard tiles with no crowns
      if overlapping_crowns.empty:
        continue
      # if len(overlapping_crowns) < threshold:
      #    continue
      # Discard tiles that do no have a sufficient coverage of training crowns
      if (overlapping_crowns.dissolve().area[0] / geo.area[0]) < threshold:
        continue
      # here we are cropping the tiff to the bounding box of the tile we want
      coords = getFeatures(geo)
      # print("Coords:", coords)

      # define the tile as a mask of the whole tiff with just the bounding box
      out_img, out_transform = mask(data, shapes=coords, crop=True)
      
      # Discard scenes with many out-of-range pixels
      out_sumbands = np.sum(out_img, 0)
      zero_mask = np.where(out_sumbands == 0, 1 , 0)
      nan_mask = np.where(out_sumbands == 765, 1 , 0)
      sumzero = zero_mask.sum()
      sumnan = nan_mask.sum()
      totalpix = out_img.shape[1] * out_img.shape[2]
      if sumzero > 0.25 * totalpix:
        continue
      elif sumnan > 0.25 * totalpix:
        continue

      #out_img = out_img.astype("uint8")
      # Or to really narrow down the crop onto the crown area
      # newbox = overlapping_crowns.total_bounds
      # newbox = gpd.GeoDataFrame(
      #    {"geometry": box(newbox[0], newbox[1], newbox[2], newbox[3])},
      #    index=[0],
      #    crs=from_epsg(4326),
      # )
      # newbox = getFeatures(newbox)

      # out_img, out_transform = mask(data, shapes=newbox, crop=True)

      # print('out transform:', out_transform)

      # This can be useful when reprojecting later as know the crs format to put it into
      # epsg_code = int(data.crs.data["init"][5:])
      # print(epsg_code)

      # copy the metadata then update it, the "nodata" and "dtype" where important as made larger
      # tifs have outputted tiles which were not just black
      out_meta = data.meta.copy()
      out_meta.update({
          "driver": "GTiff",
          "height": out_img.shape[1],
          "width": out_img.shape[2],
          "transform": out_transform,
          "nodata": None,
          "dtype": "uint8", # this causes issue - comment out for the Malaysia data
      })
      # print('Out Meta:',out_meta)

      # Saving the tile as a new tiff, named by the origin of the tile. If tile appears blank in folder can show the image here and may
      # need to fix RGB data or the dtype
      # show(out_img)
      out_tif = out_path + ".tif"
      with rasterio.open(out_tif, "w", **out_meta) as dest:
        dest.write(out_img)

      # read in the tile we have just saved
      clipped = rasterio.open(out_tif)
      # read it as an array
      # show(clipped)
      arr = clipped.read()

      # each band of the tiled tiff is a colour!
      R = arr[0]
      G = arr[1]
      B = arr[2]

      # stack up the bands in an order appropriate for saving with cv2, then rescale to the correct 0-255 range for cv2

      rgb = np.dstack((B, G, R))    # BGR for cv2
      
      if np.max(G) > 255:
        rgb_rescaled = 255*rgb/65535
      else:
        rgb_rescaled = rgb    # scale to image
      # print('rgb rescaled', rgb_rescaled)

      # save this as jpg or png...we are going for png...again, named with the origin of the specific tile
      # here as a naughty method
      cv2.imwrite(
          out_path + ".png",
          rgb_rescaled,
      )

      # img = cv2.imread(
      #    "gdrive/MyDrive/JamesHirst/NY/LargeArea_images/naip_cayuga/naip_cayuga_tiled_by_me/tile_"
      #    + str(minx)
      #    + "_"
      #    + str(miny)
      #    + ".png"
      # )
      # print('png shape:', img.shape)

      # select the crowns that intersect the non-buffered central
      # section of the tile using the inner join
      # JB : a better solution would be to clip crowns to tile extent
      # overlapping_crowns = sjoin(crowns, geo_central, how="inner")
      # Maybe left join to keep information of crowns?

      overlapping_crowns = overlapping_crowns.explode(index_parts=True)
      # print("Overlapping crowns:", overlapping_crowns)

      # translate to 0,0 to overlay on png
      # this now works as a universal approach.
      if minx == data.bounds[0] and miny == data.bounds[1]:
        # print("We are in the bottom left!")
        moved = overlapping_crowns.translate(-minx, -miny)
      elif miny == data.bounds[1]:
        # print("We are on the bottom, but not bottom left")
        moved = overlapping_crowns.translate(-minx + buffer, -miny)
      elif minx == data.bounds[0]:
        # print("We are along the left hand side, but not bottom left!")
        moved = overlapping_crowns.translate(-minx, -miny + buffer)
      else:
        # print("We are in the middle!")
        moved = overlapping_crowns.translate(-minx + buffer, -miny + buffer)
      # print("Moved coords:", moved)

      # scale to deal with the resolution
      scalingx = 1 / (data.transform[0])
      scalingy = -1 / (data.transform[4])
      moved_scaled = moved.scale(scalingx, scalingy, origin=(0, 0))
      # print(moved_scaled)

      impath = {"imagePath": (out_path + ".png")}

      # save as a geojson, a format compatible with detectron2, again named by the origin of the tile.
      # If the box selected from the image is outside of the mapped region due to the image being on a slant
      # then the shp file will have no info on the crowns and hence will create an empty gpd Dataframe.
      # this causes an error so skip creating geojson. The training code will also ignore png so no problem.
      try:
        filename = out_path + ".geojson"
        moved_scaled = overlapping_crowns.set_geometry(moved_scaled)
        moved_scaled.to_file(
            driver="GeoJSON",
            filename=filename,
        )
        with open(filename, "r") as f:
          shp = json.load(f)
          shp.update(impath)
        with open(filename, "w") as f:
          json.dump(shp, f)
      except:
        print("ValueError: Cannot write empty DataFrame to file.")
        continue
        

def to_traintest_folders(tiles_folder="./",
                         out_folder="./data/",
                         test_frac=0.2,
                         folds=1):
  """
  To send tiles to training (+validation) and test folder
  """

  Path(out_folder + "train").mkdir(parents=True, exist_ok=True)
  Path(out_folder + "test").mkdir(parents=True, exist_ok=True)

  # First split between train and test
  #split = np.array([4, 1])
  split = np.array([(1 - test_frac), test_frac])
  summed = np.sum(split)
  percs = 100 * split / summed
  percs = np.cumsum(percs)

  filenames = glob.glob(tiles_folder + "*.png")
  fileroots = [Path(item).stem for item in filenames]
  #jsonnames = glob.glob(tiles_folder + "*.geojson")
  #stemname = Path(filenames[0]).stem.split("_", 1)[0]
  #indices = [item.split("_", 1)[-1].split(".", 1)[0] for item in filenames]

  num = list(range(0, len(filenames)))
  random.shuffle(num)

  for i in range(0, len(filenames)):
    #print(i)
    if num[i] < np.percentile(num, percs[0]):
      #shutil.copy(filenames[i], out_folder + "train/")
      shutil.copy(tiles_folder + fileroots[i] + ".geojson",
                  out_folder + "train/")
    # elif num[i] < np.percentile(num, percs[1]):
    #    shutil.copy(filenames[i], "./data/val/")
    #    shutil.copy("./data/" + stemname + "_" + indices[i] + ".geojson", "./data/val/")
    else:
      #shutil.copy(filenames[i], out_folder + "test/")
      shutil.copy(tiles_folder + fileroots[i] + ".geojson",
                  out_folder + "test/")

  #filenames = glob.glob(out_folder + "/train/*.png")
  filenames = glob.glob(out_folder + "/train/*.geojson")
  fileroots = [Path(item).stem for item in filenames]
  #stemname = Path(filenames[0]).stem.split("_", 1)[0]

  #indices = [item.split("_", 1)[-1].split(".", 1)[0] for item in filenames]
  num = list(range(0, len(filenames)))
  random.shuffle(num)
  #random.shuffle(indices)
  ind_split = np.array_split(fileroots, folds)

  for i in range(0, folds):
    Path(out_folder + "/train/fold_" + str(i + 1) + "/").mkdir(parents=True,
                                                               exist_ok=True)
    for name in ind_split[i]:
      #print(ind)
      #shutil.move(
      #    out_folder + "train/" + name + ".png",
      #    out_folder + "train/fold_" + str(i + 1) + "/",
      #)
      shutil.move(
          out_folder + "train/" + name + ".geojson",
          out_folder + "train/fold_" + str(i + 1) + "/",
      )


if __name__ == "__main__":
  # Right let's test this first with Sepilok 10cm resolution, then I need to try it with 50cm resolution.
  img_path = "/content/drive/Shareddrives/detectreeRGB/benchmark/Ortho2015_benchmark/P4_Ortho_2015.tif"
  crown_path = "gdrive/MyDrive/JamesHirst/NY/Buffalo/Buffalo_raw_data/all_crowns.shp"
  out_dir = "./"
  # Read in the tiff file
  # data = img_data.open(img_path)
  # Read in crowns
  data = rasterio.open(img_path)
  crowns = geopandas.read_file(crown_path)
  print(
      "shape =",
      data.shape,
      ",",
      data.bounds,
      "and number of bands =",
      data.count,
      ", crs =",
      data.crs,
  )

  buffer = 20
  tile_width = 200
  tile_height = 200
  # resolution = 0.6 # in metres per pixel - @James Ball can you get this from the tiff?

  tile_data_train(data, buffer, tile_width, tile_height, out_dir, crowns)
  to_traintest_folders(folds=5)
  

 
def modify_crown_classes(crowns):
  """ functions to add a column to the crowns Geodataframe which record the liana conditions when already in class 0,1,2,3,4"""
    # add a column which shows the status of a tree 
  crowns["Lianas"]=crowns["Lianas"].astype(float)
  crowns = crowns.assign(status='healthy')

  for i in range (len(crowns)):
    if crowns["Lianas"][i] == 1:
      crowns["status"][i] = "slightly_infested"
    elif crowns["Lianas"][i] == 2:
      crowns['status'][i] = "partly_infested"
    elif crowns["Lianas"][i] == 3:
      crowns['status'][i] = "greatly_infested"
    elif crowns["Lianas"][i] == 4:
      crowns['status'][i] = "completely_infested"
    elif crowns["Lianas"][i] != crowns["Lianas"][i]:
      crowns['status'][i] = "delete"
    crowns = crowns.drop(crowns[crowns.status == "delete"].index)
  return(crowns)

def add_crown_classes(crowns):
  """ functions to add a column to the crowns Geodataframe which record the liana conditions when the conditions range from 0 to 100"""
    # add a column which shows the status of a tree 
  crowns = crowns.assign(status='healthy')

  for i in range (len(crowns)):
    if crowns["LI_2017"][i] > 0 and crowns["LI_2017"][i] <= 25:
      crowns["status"][i] = "slightly_infested"
    elif crowns["LI_2017"][i] > 25 and crowns["LI_2017"][i] <= 50:
      crowns['status'][i] = "partly_infested"
    elif crowns["LI_2017"][i] > 50 and crowns["LI_2017"][i] <= 75:
      crowns['status'][i] = "greatly_infested"
    elif crowns["LI_2017"][i] > 75:
      crowns['status'][i] = "completely_infested"
    elif crowns["LI_2017"][i] != crowns["LI_2017"][i]:
      crowns['status'][i] = "delete"
    crowns = crowns.drop(crowns[crowns.status == "delete"].index)
  return(crowns)
