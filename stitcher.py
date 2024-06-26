# sticher.py 
import os
import psutil
import random
import json
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import dask.array as da
from dask_image.imread import imread
from skimage import io, registration
from scipy.ndimage import shift as nd_shift
from aicsimageio.writers import OmeTiffWriter
from aicsimageio.writers import OmeZarrWriter
from aicsimageio import types
from basicpy import BaSiC

class Stitcher:
    def __init__(self, input_folder, output_name='', apply_flatfield=0):
        self.input_folder = input_folder
        self.image_folder = os.path.join(input_folder, '0')
        if not os.path.isdir(self.image_folder):
            raise Exception(f"{input_folder}/0 is not a valid directory")
        self.output_path = os.path.join(input_folder, "stitched", output_name)
        if not os.path.exists(os.path.join(input_folder, "stitched")):
            os.makedirs(os.path.join(input_folder, "stitched"))
        self.apply_flatfield = apply_flatfield

        self.processed_files = set()
        self.is_reversed = {'rows': False, 'cols': False, 'z-planes': False}
        self.selected_modes = {}
        self.acquisition_params = {}
        self.channel_names = []
        self.flatfields = {}
        self.stitching_data = {}
        self.stitched_images = None

        self.dtype = np.uint16
        self.num_t = self.num_z = self.num_c = 1
        self.num_cols = self.num_rows = 0
        self.input_height = self.input_width = 0
        self.v_shift = self.h_shift = (0,0)


    def extract_selected_modes(self):
        configs_path = os.path.join(self.input_folder, 'configurations.xml')
        tree = ET.parse(configs_path)
        root = tree.getroot()
        for mode in root.findall('.//mode'):
            if mode.get('Selected') == '1':
                mode_id = mode.get('ID')
                self.selected_modes[mode_id] = {
                    'Name': mode.get('Name'),
                    'ExposureTime': mode.get('ExposureTime'),
                    'AnalogGain': mode.get('AnalogGain'),
                    'IlluminationSource': mode.get('IlluminationSource'),
                    'IlluminationIntensity': mode.get('IlluminationIntensity')
                }

    def extract_acquisition_parameters(self):
        acquistion_params_path = os.path.join(self.input_folder, 'acquisition parameters.json')
        with open(acquistion_params_path, 'r') as file:
            self.acquisition_params = json.load(file)

    def determine_directions(self):
        coordinates = pd.read_csv(os.path.join(self.image_folder, 'coordinates.csv'))
        i_rev = not coordinates.sort_values(by='i')['y (mm)'].is_monotonic_increasing
        j_rev = not coordinates.sort_values(by='j')['x (mm)'].is_monotonic_increasing
        k_rev = not coordinates.sort_values(by='k')['z (um)'].is_monotonic_increasing
        self.is_reversed = {'rows': i_rev, 'cols': j_rev, 'z-planes': k_rev}
        print(self.is_reversed)

    def parse_filenames(self, four_input_format=False):
        # Read the first image to get its dimensions and dtype
        sorted_input_files = sorted([filename for filename in os.listdir(self.image_folder) 
                             if (filename.endswith(".bmp") or filename.endswith(".tiff"))
                             and 'focus_camera' not in filename])

        first_filename = sorted_input_files[0]

        try:
            well, i, j, k, channel_name = os.path.splitext(first_filename)[0].split('_', 4)
            k = int(k)
            print("well_i_j_k_channel_name: ", os.path.splitext(first_filename)[0])
            four_input_format = True
        except ValueError as ve:
            print("i_j_k_channel_name: ", os.path.splitext(first_filename)[0])
            four_input_format = False

        first_image = imread(os.path.join(self.image_folder, first_filename))
        self.dtype = np.dtype(first_image.dtype)
        self.input_height, self.input_width = first_image.shape[-2:]
        del first_image

        channel_names = set()
        max_i = max_j = max_k = 0
        # Read all image filenames to get data for stitching 
        for filename in sorted_input_files:
            if four_input_format == True:
                _, i, j, k, channel_name = os.path.splitext(filename)[0].split('_', 4) 
            else:
                i, j, k, channel_name = os.path.splitext(filename)[0].split('_', 3)

            i, j, k = int(i), int(j), int(k)
            channel_names.add(channel_name)
            channel_data = self.stitching_data.setdefault(channel_name, {})
            z_data = channel_data.setdefault(k, [])
            z_data.append({
                'row': i,
                'col': j,
                'z_level': k,
                'channel': channel_name,
                'filename': filename
            })
            max_k = max(max_k, k)
            max_j = max(max_j, j)
            max_i = max(max_i, i)
        
        self.channel_names = sorted(list(channel_names))
        self.num_c = len(self.channel_names)
        self.num_z = max_k + 1
        self.num_cols = max_j + 1
        self.num_rows = max_i + 1


    def get_flatfields(self, progress_callback=None):
        #print("getting flatfields...")
        for c_i, channel in enumerate(self.channel_names):
            channel_tiles = []
            #print("channel:", channel)
            # Create a shuffled list of all tile for each channel
            for z_level, z_data in self.stitching_data[channel].items():
                channel_tiles.extend(z_data)
            random.shuffle(channel_tiles)

            images = []
            for tile_info in channel_tiles[:min(32, len(channel_tiles))]:
                #print("row:", tile_info['row'], "col:", tile_info['col'], "z_level:", tile_info['z_level'])
                filepath = os.path.join(self.image_folder, tile_info['filename'])
                images.append(imread(filepath)[0])

            images = np.array(images)
            # print(images.shape)
            basic = BaSiC(get_darkfield=False, smoothness_flatfield=1)
            basic.fit(images)
            #np.save(os.path.join(self.input_folder, f'{channel}_flatfield.npy'), basic.flatfield)
            self.flatfields[c_i] = basic.flatfield
            progress_callback(c_i + 1, self.num_c)

    def calculate_horizontal_shift(self, img1_path, img2_path, max_overlap):
        img1 = imread(img1_path)[0]
        img2 = imread(img2_path)[0]
        margin = self.input_height // 10 # set margin amount 
        img1_roi = img1[margin:-margin, -max_overlap:]
        img2_roi = img2[margin:-margin, :max_overlap]
        shift, error, diffphase = registration.phase_cross_correlation(img1_roi, img2_roi, upsample_factor=10)
        return round(shift[0]), round(shift[1] - img1_roi.shape[1])

    def calculate_vertical_shift(self, img1_path, img2_path, max_overlap):
        img1 = imread(img1_path)[0]
        img2 = imread(img2_path)[0]
        margin = self.input_width // 10
        img1_roi = img1[-max_overlap:, margin:-margin]
        img2_roi = img2[:max_overlap, margin:-margin]
        shift, _, diffphase = registration.phase_cross_correlation(img1_roi, img2_roi, upsample_factor=10)
        return round(shift[0] - img1_roi.shape[0]), round(shift[1])

    def calculate_shifts(self, z_level=0, channel=""):
        channel = self.channel_names[0] if channel not in self.channel_names else channel
        dx_mm = self.acquisition_params['dx(mm)']  # physical distance between adjacent scans in x direction
        dy_mm = self.acquisition_params['dy(mm)']  # physical distance between adjacent scans in y direction
        obj_mag = self.acquisition_params['objective']['magnification']
        obj_tube_lens_mm = self.acquisition_params['objective']['tube_lens_f_mm']
        sensor_pixel_size_um = self.acquisition_params['sensor_pixel_size_um']

        obj_focal_length_mm = obj_tube_lens_mm / obj_mag  # Objective focal length
        tube_lens_mm = self.acquisition_params['tube_lens_mm']  # Actual tube lens focal length used in your system
        actual_mag = tube_lens_mm / obj_focal_length_mm  # Actual magnification
        pixel_size_um = sensor_pixel_size_um / actual_mag
        # Convert mm to pixels
        dx_pixels = round(dx_mm * 1000 / pixel_size_um) 
        dy_pixels = round(dy_mm * 1000 / pixel_size_um)
        # Calculate max overlaps based on the movement between images and the size of the images
        
        x_overlap_estimate = max(self.input_width - dx_pixels, 0)
        y_overlap_estimate = max(self.input_height - dy_pixels, 0)

        #x_overlap_estimate = max(0, self.input_width - dx_pixels)
        #y_overlap_estimate = max(0, self.input_height - dy_pixels)
        print(y_overlap_estimate)
        print(x_overlap_estimate)

        col_left, col_right = ((self.num_cols - 1) // 2, (self.num_cols - 1) // 2 + 1) 
        if self.is_reversed['cols']:
            col_left, col_right = col_right, col_left
        row_top, row_bottom = ((self.num_cols - 1) // 2, (self.num_cols - 1) // 2 + 1 )
        if self.is_reversed['rows']:
            row_top, row_bottom = row_bottom, row_top

        img1_path = img2_path_vertical = img2_path_horizontal = None
        for tile_info in self.stitching_data[channel][z_level]:
            if tile_info['col'] == col_left and tile_info['row'] == row_top:
                img1_path = os.path.join(self.image_folder, tile_info['filename'])
            elif tile_info['col'] == col_left and tile_info['row'] == row_bottom:
                img2_path_vertical = os.path.join(self.image_folder, tile_info['filename'])
            elif tile_info['col'] == col_right and tile_info['row'] == row_top:
                img2_path_horizontal = os.path.join(self.image_folder, tile_info['filename'])

        if img1_path == None:
            raise Exception(f"no input file found for c:{channel} k:{z_level} j:{col_left} i:{row_top}")
        if img2_path_vertical == None or img2_path_vertical == img1_path or y_overlap_estimate == 0:
            v_shift = (0,0)
        else:
            v_shift = self.calculate_vertical_shift(img1_path, img2_path_vertical, y_overlap_estimate)
            # check if valid
            # self.v_shift = (self.v_shift[0], 0) if self.v_shift[1] > y_overlap_estimate * 2 else self.v_shift # bad registration
        
        if img2_path_horizontal == None or img2_path_horizontal == img1_path or x_overlap_estimate == 0:
            h_shift = (0,0)
        else:
            h_shift = self.calculate_horizontal_shift(img1_path, img2_path_horizontal, x_overlap_estimate)
            # check if valid
            # self.h_shift = (0, self.h_shift[1]) if self.h_shift[0] > x_overlap_estimate * 2 else self.h_shift # bad registration
        print(img1_path, "vertically adjacent to", img2_path_vertical)
        print(img1_path, "horizintally adjacent to ", img2_path_horizontal)
        # print("vertical shift:", self.v_shift, ", horizontal shift:", self.h_shift)
        print("v_shift =", v_shift)
        print("h_shift =", h_shift)
        self.v_shift = v_shift
        self.h_shift = h_shift

    def get_tczyx_shape(self):
        """Estimates the memory usage for the stitched image array."""
        element_size = np.dtype(self.dtype).itemsize  # Byte size of one array element
        x_max = self.input_width + ((self.num_cols - 1) * (self.input_width + self.h_shift[1])) + abs((self.num_rows - 1) * self.v_shift[1])
        y_max = self.input_height + ((self.num_rows - 1) * (self.input_height + self.v_shift[0])) + abs((self.num_cols - 1) * self.h_shift[0])
        tczyx_shape = (1, len(self.channel_names), self.num_z, y_max, x_max)
        memory_bytes = np.prod(tczyx_shape) * element_size # # Total memory in bytes
        return tczyx_shape, memory_bytes // 2 # needs less with dask

    def stitch_images_cropped(self, progress_callback=None):
        tczyx_shape, _ = self.get_tczyx_shape()
        chunks = (1, 1, 1, self.input_height, self.input_width)
        self.stitched_images = da.zeros(tczyx_shape, dtype=self.dtype, chunks=chunks)
        # print(self.stitched_images.shape)
        total_tiles = sum(len(z_data) for channel_data in self.stitching_data.values() for z_data in channel_data.values())
        processed_tiles = 0

        for channel_idx, channel in enumerate(self.channel_names):
            for z_level, z_data in self.stitching_data[channel].items():
                for tile_info in z_data:
                    self.stitch_single_image(tile_info, channel_idx, z_level)
                    processed_tiles += 1
                    if progress_callback is not None:
                        progress_callback(processed_tiles, total_tiles)

    def stitch_single_image(self, tile_info, channel_idx, z_level):
        tile = imread(os.path.join(self.image_folder, tile_info['filename']))[0]

        if self.apply_flatfield:
            tile = (tile / self.flatfields[channel_idx]).clip(min=np.iinfo(self.dtype).min, 
                                                          max=np.iinfo(self.dtype).max).astype(self.dtype)

        # Get tile grid location (row, col)
        row = self.num_rows - 1 - tile_info['row'] if self.is_reversed['rows'] else tile_info['row']
        col = self.num_cols - 1 - tile_info['col'] if self.is_reversed['cols'] else tile_info['col']

        # Determine crop for tile edges 
        top_crop = max(0, (-self.v_shift[0] // 2) - abs(self.h_shift[0]) // 2) if row > 0 else 0
        bottom_crop = max(0, (-self.v_shift[0] // 2) - abs(self.h_shift[0]) // 2) if row < self.num_rows - 1 else 0
        left_crop = max(0, (-self.h_shift[1] // 2) - abs(self.v_shift[1]) // 2) if col > 0 else 0
        right_crop = max(0, (-self.h_shift[1] // 2) - abs(self.v_shift[1]) // 2) if col < self.num_cols - 1 else 0

        tile = tile[top_crop:tile.shape[0]-bottom_crop, left_crop:tile.shape[1]-right_crop]

        # Initialize starting coordinates based on tile position and shift
        y = row * (self.input_height + self.v_shift[0]) + top_crop
        # Apply the vertical component of the horizontal shift
        if self.h_shift[0] < 0:
            y -= (self.num_cols - 1 - col) * self.h_shift[0]  # Moves up if negative
        else:
            y += col * self.h_shift[0]  # Moves down if positive

        # Initialize starting coordinates based on tile position and shift
        x = col * (self.input_width + self.h_shift[1]) + left_crop
        # Apply the horizontal component of the vertical shift
        if self.v_shift[1] < 0:
            x -= (self.num_rows - 1 - row) * self.v_shift[1]  # Moves left if negative
        else:
            x += row * self.v_shift[1]  # Moves right if positive
        
        # Place cropped tile on the stitched image canvas
        self.stitched_images[0, channel_idx, z_level, y:y+tile.shape[-2], x:x+tile.shape[-1]] = tile
        # print(f" col:{col}, \trow:{row},\ty:{y}-{y+tile.shape[0]}, \tx:{x}-{x+tile.shape[-1]}")


    def save_as_ome_tiff(self, dz_um=None, sensor_pixel_size_um=None):
        ome_metadata = OmeTiffWriter.build_ome(
            image_name=[os.path.basename(self.output_path)],
            data_shapes=[self.stitched_images.shape],
            data_types=[self.stitched_images.dtype],
            dimension_order=["TCZYX"],
            channel_names=[self.channel_names],
            physical_pixel_sizes=[types.PhysicalPixelSizes(dz_um, sensor_pixel_size_um, sensor_pixel_size_um)]
        )
        OmeTiffWriter.save(
            data=self.stitched_images,
            uri=self.output_path,
            ome_xml=ome_metadata,
            dimension_order=["TCZYX"]
        )
        self.stitched_images = None

    def save_as_ome_zarr(self, dz_um=None, sensor_pixel_size_um=None):
        #print(self.stitched_images.shape)
        #print(self.stitched_images.dtype)
        default_color_hex = 0xFFFFFF        
        default_intensity_min = np.iinfo(self.stitched_images.dtype).min
        default_intensity_max = np.iinfo(self.stitched_images.dtype).max

        channel_colors = [default_color_hex] * self.num_c
        channel_minmax = [(default_intensity_min, default_intensity_max)] * self.num_c

        zarr_writer = OmeZarrWriter(self.output_path)
        zarr_writer.build_ome(
            size_z=self.num_z,
            image_name=os.path.basename(self.output_path),
            channel_names=self.channel_names,
            channel_colors=channel_colors,
            channel_minmax=channel_minmax
        )
        zarr_writer.write_image(
            image_data=self.stitched_images,
            image_name=os.path.basename(self.output_path),
            physical_pixel_sizes=types.PhysicalPixelSizes(dz_um, sensor_pixel_size_um, sensor_pixel_size_um),
            channel_names=self.channel_names,
            channel_colors=channel_colors,
            dimension_order="TCZYX",
            chunk_dims=(1, 1, 1, self.input_height, self.input_width)
        )
        self.stitched_images = None
