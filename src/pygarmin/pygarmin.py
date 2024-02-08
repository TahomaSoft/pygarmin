#!/usr/bin/env python3
"""Pygarmin

   This is a console user application for communicating with Garmin GPS
   receivers.

   This file is part of the pygarmin distribution
   (https://github.com/quentinsf/pygarmin).

   Copyright 2022 Folkert van der Beek <folkertvanderbeek@gmail.com>

   This program is free software: you can redistribute it and/or modify it under
   the terms of the GNU General Public License as published by the Free Software
   Foundation, version 3.

   This program is distributed in the hope that it will be useful, but WITHOUT
   ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
   FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
   details.

   You should have received a copy of the GNU General Public License along with
   this program. If not, see <http://www.gnu.org/licenses/>.

   This is released under the Gnu General Public Licence. A copy of this can be
   found at https://opensource.org/licenses/gpl-license.html

"""
__version__ = '0.2'

import argparse
import json
import logging
from microbmp import MicroBMP
import os
from PIL import Image, ImagePalette, UnidentifiedImageError
import re
import signal
import sys
from tabulate import tabulate
from tqdm import tqdm
import io
from .garmin import garmin
from .gpx import gpx as GPX

logging_levels = {
    0: logging.NOTSET,
    1: logging.WARNING,
    2: logging.INFO,
    3: logging.DEBUG,
}

log = logging.getLogger('garmin')
log.addHandler(logging.StreamHandler())

def to_pixel_data(pixel_values, bpp):
    """Returns the pixel array of the image.

    :param pixel_values: the pixel values of the image
    :type pixel_values: list[int]
    :param bpp: the color depth of the image, must be in (1, 2, 4, 8)
    :type bpp: int
    :return: pixel_array
    :rtype: bytearray

    """
    if 8 % bpp != 0:
        sys.exit(f"{bpp}-bit color depth is not supported")
    ppb = 8 // bpp
    pixel_array = list()
    for pos in range(0, len(pixel_values), ppb):
        values = pixel_values[pos:pos+ppb]
        pixels = 0
        # Pixels are stored from left to right, so the bits of the first pixel
        # are shifted to the far left
        for idx, value in enumerate(reversed(values)):
            if value.bit_length() > bpp:
                sys.exit(f"Integer {value} cannot be represented by {bpp} bits")
            offset = idx * bpp
            pixels = pixels + (value << offset)
        pixel_array.append(pixels)
    return bytearray(pixel_array)

def to_pixel_values(pixel_array, bpp):
    """Returns the contents of this image as a list of pixel values.

    :param pixel_array: the pixel array of the image
    :type pixel_array: bytes or bytearray
    :param bpp: the color depth of the image, must be in (1, 2, 4, 8)
    :type bpp: int
    :return: list of pixel values
    :rtype: list[int]

    """
    if 8 % bpp != 0:
        sys.exit(f"{bpp}-bit color depth is not supported")
    pixel_values = []
    # Calculate the bitmask, that is the maximum integer that can be
    # represented by the number of bits per pixel
    mask = pow(2, bpp) - 1
    for byte in pixel_array:
        for offset in reversed(range(0, 8, bpp)):
            value = byte >> offset & mask
            pixel_values.append(value)
    return pixel_values

def bmp_to_pil(bmp):
    """Converts BMP to PIL image.

    :param bmp: BMP image
    :type bmp: BMP
    :return: PIL image
    :rtype: Image.Image

    """
    log.info("Converting BMP to PIL image")
    # PIL supports images of 1/8/24/32-bit color depth
    bpp = bmp.DIB_depth
    if bpp == 1:
        log.info("Using black and white mode (1)")
        mode = '1'
    elif bpp in (1, 2, 4):
        # Convert BMP to 8-bit PIL image
        log.info(f"Converting {bpp} bpp image to 8 bpp color depth")
        pixel_values = to_pixel_values(bmp.parray, bpp)
        pixel_data = to_pixel_data(pixel_values, 8)
        log.info("Using palette mode (P)")
        mode = 'P'
    elif bpp == 8:
        pixel_data = bmp.parray
        log.info("Using palette mode (P)")
        mode = 'P'
    elif bpp == 24:
        pixel_data = bmp.parray
        log.info("Using true color mode (RGB)")
        mode = 'RGB'
    else:
        sys.exit(f"{bpp}-bit color depth is not supported")
    image = Image.frombytes(mode=mode,
                                size=(bmp.DIB_w, bmp.DIB_h),
                                data=pixel_data,
                                decoder_name='raw')
    if mode == 'P':
        log.info("Attaching palette to image")
        # The BMP palette is a list of bytearrays, but the PIL palette must be a
        # flat list of integers (or a single bytearray)
        palette = [ channel for color in bmp.palette for channel in color ]
        log.debug(f"RGB color palette: {*[ tuple(color) for color in bmp.palette ], }")
        image.putpalette(palette, rawmode='RGB')
    return image

class ProgressBar(tqdm):

    def update_to(self, object, current, total):
        self.total = total
        self.update(current - self.n)


class Gpsd:

    def __str__(self):
        return json.dumps(self.get_dict())


class TPV(Gpsd):

    def __init__(self, pvt):
        self.pvt = pvt
        self.mode = self._get_mode()
        self.time = self.pvt.get_datetime().astimezone().isoformat()
        self.alt_hae = self.pvt.alt
        self.alt_msl = self.pvt.get_msl_alt()
        # Gpsd assumes a confidence of 50%, and applies a correction to
        # obtain a 95% confidence circle. However, according to the
        # specification the 2-sigma (95th percentile) accuracy value is
        # provided, so no correction is needed.
        self.sep = self.pvt.epe
        self.eph = self.pvt.eph
        self.epv = self.pvt.epv
        self.geoid_sep = -self.pvt.msl_hght  # sign is opposite of garmin sign
        self.lat = self.pvt.get_posn().as_degrees().lat
        self.leapseconds = self.pvt.leap_scnds
        self.lon = self.pvt.get_posn().as_degrees().lon
        self.vel_d = -self.pvt.up  # sign is opposite of garmin sign
        self.vel_e = self.pvt.east
        self.vel_n = self.pvt.north

    def _get_mode(self, product_description=None):
        if product_description:
            fix = self.pvt.get_fix(str(product_description, 'latin_1'))
        else:
            fix = self.pvt.get_fix()
        if fix == '2D' or fix == '2D_diff':
            mode = 2
        elif fix == '3D' or fix == '3D_diff':
            mode = 3
        else:
            mode = 1
        return mode


    def get_dict(self):
        return {'class': 'TPV',
                'device': 'device',
                'mode': self.mode,
                'time': self.time,
                'altHAE': self.alt_hae,
                'altMSL': self.alt_msl,
                'sep': self.sep,
                'eph': self.eph,
                'epv': self.epv,
                'geoidSep': self.geoid_sep,
                'lat': self.lat,
                'leapseconds': self.leapseconds,
                'lon': self.lon,
                'velD': self.vel_d,
                'velE': self.vel_e,
                'velN': self.vel_n,
                }


class SAT(Gpsd):
    def __init__(self, sat):
        self.sat = sat
        self.prn = self.sat.get_prn()
        self.az = self.sat.azmth        # Azimuth, degrees from true north
        self.el = self.sat.elev         # Elevation in degrees
        self.ss = self.sat.snr          # Signal to Noise ratio in dBHz
        self.used = self.sat.is_used()  # Used in current solution?

    def get_dict(self):
        return {'PRN': self.prn,
                'az': self.az,
                'el': self.el,
                'ss': self.ss,
                'used': self.used,
                }


class SKY(Gpsd):
    """A SKY object reports a sky view of the GPS satellite positions. If there
    is no GPS device available, or no skyview has been reported yet, only the
    \"class\" field will reliably be present.

    """
    def __init__(self, pvt):
        self.pvt = pvt

    def get_satellites(self):
        records = self.pvt.get_records()
        satelittes = [ SAT(record) for record in records ]
        return satelittes

    def get_dict(self):
        return {'class': 'SKY',
                'satellites': [satellite.get_dict() for satellite in self.get_satellites()],
                }


class Pygarmin:
    protocol_names = {
        'L000': 'Basic Link Protocol',
        'L001': 'Link Protocol 1',
        'L002': 'Link Protocol 2',
        'A000': 'Product Data Protocol',
        'A001': 'Protocol Capability Protocol',
        'A010': 'Device Command Protocol 1',
        'A011': 'Device Command Protocol 2',
        'T001': 'Transmission Protocol',
        'A100': 'Waypoint Transfer Protocol',
        'A101': 'Waypoint Category Transfer Protocol',
        'A200': 'Route Transfer Protocol',
        'A201': 'Route Transfer Protocol',
        'A300': 'Track Log Transfer Protocol',
        'A301': 'Track Log Transfer Protocol',
        'A302': 'Track Log Transfer Protocol',
        'A400': 'Proximity Waypoint Transfer Protocol',
        'A500': 'Almanac Transfer Protocol',
        'A600': 'Date And Time Initialization Protocol',
        'A650': 'Flightbook Transfer Protocol',
        'A700': 'Position Initialization Protocol',
        'A800': 'PVT Protocol',
        'A900': 'Map Transfer Protocol',
        'A902': 'Map Unlock Protocol',
        'A906': 'Lap Transfer Protocol',
        'A1000': 'Run Transfer Protocol',
        'A1002': 'Workout Transfer Protocol',
        'A1004': 'Fitness User Profile Transfer Protocol',
        'A1005': 'Workout Limits Transfer Protocol',
        'A1006': 'Course Transfer Protocol',
        'A1009': 'Course Limits Transfer Protocol',
        'A1051': 'External Time Data Sync Protocol',
    }

    def __init__(self, port):
        self.port = port
        self.gps = self.get_gps(self.port)


    def get_gps(self, port):
        phys = garmin.USBLink() if port == 'usb:' else garmin.SerialLink(port)
        log.info(f"Listening on port {port}")
        return garmin.Garmin(phys)

    def info(self, args):
        info = "Product information\n"
        info += "===================\n"
        info += f"Product ID: {self.gps.product_id}\n"
        info += f"Software version: {self.gps.software_version:.2f}\n"
        info += f"Product description: {str(self.gps.product_description, 'latin_1')}\n"
        info += f"Unit ID: {self.gps.get_unit_id()}\n"
        args.filename.write(info)

    def protocols(self, args):
        info = "Supported protocols and data types\n"
        info += "==================================\n"
        for protocol_datatypes in self.gps.supported_protocols:
            protocol = protocol_datatypes[0]
            datatypes = protocol_datatypes[1:]
            protocol_name = self.protocol_names.get(protocol, "Unknown Protocol")
            info += "\n"
            info += f"{protocol_name}\n"
            info += f"{'-' * len(protocol_name)}\n"
            if datatypes:
                info += f"{protocol}: {', '.join(datatypes)}\n"
            else:
                info += f"{protocol}\n"
        args.filename.write(info)

    def memory(self, args):
        try:
            data = self.gps.get_memory_properties()
            info = "Memory information\n"
            info += "==================\n"
            info += f"Memory region: {data.mem_region}\n"
            info += f"Maximum number of tiles: {data.max_tiles}\n"
            info += f"Memory size: {data.mem_size}\n"
            args.filename.write(info)
        except garmin.GarminError as e:
            sys.exit(f"{e}")

    def map_info(self, args):
        try:
            records = self.gps.get_map_properties()
            if records is None:
                log.warning("Map not found")
            else:
                info = "Map information\n"
                info += "===============\n"
                for record in records:
                    if isinstance(record, garmin.MapSegment):
                        info += "Map segment description\n"
                        info += "-----------------------\n"
                        info += f"Product ID: {record.pid}\n"
                        info += f"Family ID: {record.fid}\n"
                        info += f"Segment ID: {record.segment_id}\n"
                        info += f"Family name: {str(record.name, 'latin_1')}\n"
                        info += f"Segment name: {str(record.segment_name, 'latin_1')}\n"
                        info += f"Area name: {str(record.area_name, 'latin_1')}\n"
                        info += "\n"
                    elif isinstance(record, garmin.MapSet):
                        info += "Map set description\n"
                        info += "-------------------\n"
                        info += f"Mapset name: {str(record.mapset_name, 'latin_1')}\n"
                        info += "\n"
                    elif isinstance(record, garmin.MapUnlock):
                        info += "Map unlock description\n"
                        info += "----------------------\n"
                        info += f"Unlock code: {str(unlock_code, 'latin_1')}\n"
                        info += "\n"
                    elif isinstance(record, garmin.MapProduct):
                        info += "Map product description\n"
                        info += "-----------------------\n"
                        info += f"Product ID: {record.pid}\n"
                        info += f"Family ID: {record.fid}\n"
                        info += f"Family name: {str(record.name, 'latin_1')}\n"
                        info += "\n"
                args.filename.write(info)
        except garmin.GarminError as e:
            sys.exit(f"{e}")

    def get_waypoints(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                waypoints = self.gps.get_waypoints(callback=progress_bar.update_to)
        else:
            waypoints = self.gps.get_waypoints()
        if self.gps.waypoint_category_transfer is not None:
            if args.progress:
                with ProgressBar() as progress_bar:
                    waypoint_categories = self.gps.waypoint_category_transfer.get_data(callback=progress_bar.update_to)
            else:
                waypoint_categories = self.gps.waypoint_category_transfer.get_data()
        else:
            waypoint_categories = None
        if args.format == 'txt':
            for waypoint in waypoints:
                args.filename.write(f"{str(waypoint)}\n")
        elif args.format == 'garmin':
            for waypoint in waypoints:
                args.filename.write(f"{repr(waypoint)}\n")
        elif args.format == 'gpx':
            gpx = GPX.GPXWaypoints(waypoints)
            args.filename.write(f"{str(gpx)}\n")

    def put_waypoints(self, args):
        data = []
        for line in args.filename:
            object = eval(line)
            data.append(object)
        if args.progress:
            with ProgressBar() as progress_bar:
                self.gps.put_waypoints(data, callback=progress_bar.update_to)
        else:
            self.gps.put_waypoints(data)

    def get_routes(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                routes = self.gps.get_routes(callback=progress_bar.update_to)
        else:
            routes = self.gps.get_routes()
        if args.format == 'txt':
            for point in routes:
                args.filename.write(f"{str(point)}\n")
        elif args.format == 'garmin':
            for point in routes:
                args.filename.write(f"{repr(point)}\n")
        elif args.format == 'gpx':
            gpx = GPX.GPXRoutes(routes)
            args.filename.write(f"{str(gpx)}\n")

    def put_routes(self, args):
        data = []
        for line in args.filename:
            object = eval(line)
            data.append(object)
        if args.progress:
            with ProgressBar() as progress_bar:
                self.gps.put_routes(data, callback=progress_bar.update_to)
        else:
            self.gps.put_routes(data)

    def get_tracks(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                tracks = self.gps.get_tracks(callback=progress_bar.update_to)
        else:
            tracks = self.gps.get_tracks()
        if args.format == 'txt':
            for point in tracks:
                args.filename.write(f"{str(point)}\n")
        elif args.format == 'garmin':
            for point in tracks:
                args.filename.write(f"{repr(point)}\n")
        elif args.format == 'gpx':
            gpx = GPX.GPXTracks(tracks)
            args.filename.write(f"{str(gpx)}\n")

    def put_tracks(self, args):
        data = []
        for line in args.filename:
            object = eval(line)
            data.append(object)
        if args.progress:
            with ProgressBar() as progress_bar:
                self.gps.put_tracks(data, callback=progress_bar.update_to)
        else:
            self.gps.put_tracks(data)

    def get_proximities(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                proximities = self.gps.get_proximities(callback=progress_bar.update_to)
        else:
            proximities = self.gps.get_proximities()
        if args.format == 'txt':
            for waypoint in proximities:
                args.filename.write(f"{str(waypoint)}\n")
        elif args.format == 'garmin':
            for waypoint in proximities:
                args.filename.write(f"{repr(waypoint)}\n")
        elif args.format == 'gpx':
            gpx = self.waypoints_to_gpx(proximities)
            args.filename.write(gpx.to_xml())

    def put_proximities(self, args):
        data = []
        for line in args.filename:
            object = eval(line)
            data.append(object)
        if args.progress:
            with ProgressBar() as progress_bar:
                self.gps.put_proximities(data, callback=progress_bar.update_to)
        else:
            self.gps.put_proximities(data)

    def get_almanac(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                almanacs = self.gps.get_almanac(callback=progress_bar.update_to)
        else:
            almanacs = self.gps.get_almanac()
        if args.format == 'txt':
            func = str
        elif args.format == 'garmin':
            func = repr
        for almanac in almanacs:
            args.filename.write(f"{func(almanac)}\n")

    def get_time(self, args):
        time = self.gps.get_time()
        if args.format == 'txt':
            args.filename.write(f"{time.get_datetime()}\n")
        elif args.format == 'garmin':
            args.filename.write(f"{repr(time)}\n")

    def get_position(self, args):
        position = self.gps.get_position()
        if args.format == 'txt':
            func = str
        elif args.format == 'garmin':
            func = repr
        args.filename.write(f"{func(position.as_degrees())}\n")

    def pvt(self, args):
        def signal_handler(signal, frame):
            self.gps.pvt_off()
            sys.exit(0)
        log.warning("Press Ctrl-C to quit")
        # Catch interrupt from keyboard (Control-C)
        signal.signal(signal.SIGINT, signal_handler)
        self.gps.pvt_on()
        # In PVT mode the device will transmit packets approximately once per
        # second, but the default timeout of 1 second will lead to a timeout
        # error frequently
        self.gps.phys.set_timeout(2)
        while True:
            pvt = self.gps.get_pvt()
            if args.format == 'txt':
                args.filename.write(f"{str(pvt)}\n")
            elif args.format == 'garmin':
                args.filename.write(f"{repr(pvt)}\n")
            elif args.format == 'gpsd':
                if isinstance(pvt, garmin.D800):
                    args.filename.write(f"{TPV(pvt)}\n")
                elif isinstance(pvt, garmin.Satellite):
                    args.filename.write(f"{SKY(pvt)}\n")
                else:
                    log.warning(f"Unknown datatype {type(pvt).__name__}")
            args.filename.flush()

    def get_laps(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                laps = self.gps.get_laps(callback=progress_bar.update_to)
        else:
            laps = self.gps.get_laps()
        for lap in laps:
            if args.format == 'txt':
                args.filename.write(f"{str(lap)}\n")
            elif args.format == 'garmin':
                args.filename.write(f"{repr(lap)}\n")

    def get_runs(self, args):
        if args.progress:
            with ProgressBar() as progress_bar:
                runs = self.gps.get_runs(callback=progress_bar.update_to)
        else:
            runs = self.gps.get_runs()
        for run in runs:
            if args.format == 'txt':
                args.filename.write(f"{str(run)}\n")
            elif args.format == 'garmin':
                args.filename.write(f"{repr(run)}\n")

    def get_map(self, args):
        try:
            if args.progress:
                with ProgressBar() as progress_bar:
                    data = self.gps.get_map(callback=progress_bar.update_to)
            else:
                data = self.gps.get_map()
            if data is None:
                log.warning("Map not found")
            else:
                with open(args.filename, 'wb') as f:
                    f.write(data)
        except garmin.GarminError as e:
            sys.exit(f"{e}")

    def put_map(self, args):
        try:
            with open(args.filename, 'rb') as f:
                if args.progress:
                    with ProgressBar(unit='B', unit_scale=True, unit_divisor=1024, miniters=1) as progress_bar:
                        self.gps.put_map(f, callback=progress_bar.update_to)
                else:
                    self.gps.put_map(f)
        except garmin.GarminError as e:
            sys.exit(f"{e}")

    def del_map(self, args):
        try:
            self.gps.del_map()
        except garmin.GarminError as e:
            sys.exit(f"{e}")

    def get_screenshot(self, args):
        log.info(f"Downloading screenshot")
        if args.progress:
            with ProgressBar() as progress_bar:
                bmp = self.gps.get_screenshot(callback=progress_bar.update_to)
        else:
            bmp = self.gps.get_screenshot()
        log.info(f"Received BMP image of {bmp.DIB_w}x{bmp.DIB_h} pixels and {bmp.DIB_depth} bpp")
        if args.filename is None:
            if args.format is None:
                # No format is given, so use the BMP format by default
                log.info(f"Using the BMP format")
                filename = "Screenshot.bmp"
            else:
                # Determine the filename extension
                log.debug(f"Supported formats: {*Image.registered_extensions().values(), }")
                log.info(f"Trying to find an extension matching the {args.format.upper()} format")
                extensions = [ extension for (extension, format) in Image.registered_extensions().items() if format == args.format.upper() ]
                if len(extensions) == 0:
                    sys.exit(f"Image format {args.format.upper()} is not supported")
                elif len(extensions) == 1:
                    log.info(f"Found extension {extensions[0]}")
                    filename = "Screenshot" + extensions[0]
                elif len(extensions) > 1:
                    log.info(f"Found extensions {*extensions, }")
                    # If a format has multiple extensions, prefer the
                    # extension with the same name as the format.
                    preferred_extension = [ extension for extension in extensions if extension == '.' + args.format.lower() ]
                    if preferred_extension:
                        log.info(f"Prefer extension {preferred_extension[0]}")
                        filename = "Screenshot" + preferred_extension[0]
                    else:
                        sys.exit("The extension could not be determined")
        else:
            filename = args.filename
        if args.format is not None and not path.endswith(args.format.lower()):
            log.warning(f"Override format by saving {path} as {args.format.upper()}")
        log.info(f"Saving {path}")
        if args.format == 'bmp' or path.endswith('bmp'):
            # BMP supports images of 1/2/4/8/24-bit color depth
            bmp.save(path)
        else:
            image = bmp_to_pil(bmp)
            image.save(path, format=args.format)

    def get_image_types(self, args):
        image_types = self.gps.get_image_types()
        info = "Image types\n"
        info += "===========\n"
        args.filename.write(info)
        print(tabulate(image_types, headers='keys', tablefmt='plain'))

    def get_image_list(self, args):
        image_list = self.gps.get_image_list()
        info = "Image list\n"
        info += "===========\n"
        args.filename.write(info)
        print(tabulate(image_list, headers='keys', tablefmt='plain'))

    def get_image(self, args):
        image_list = self.gps.get_image_list()
        if args.index is None:
            indices = [ image['idx'] for image in image_list ]
            log.info("Download all images")
        else:
            indices = args.index
            log.info(f"Download image {*[idx for idx in indices],}")
        for idx in indices:
            basename = image_list[idx].get('name')
            log.info(f"Downloading {basename}")
            if args.progress:
                with ProgressBar() as progress_bar:
                    bmp = self.gps.get_image(idx, callback=progress_bar.update_to)
            else:
                bmp = self.gps.get_image(idx)
            log.info(f"Received BMP image of {bmp.DIB_w}x{bmp.DIB_h} pixels and {bmp.DIB_depth} bpp")
            single_modulo = '(?<!%)%(?!%)'  # match a single % character
            # Determine filename
            if args.filename is None or os.path.isdir(args.filename):
                # No filename is given, so use the basename by default
                if args.format is None:
                    # No format is given, so use the BMP format by default
                    log.info(f"Using the BMP format")
                    filename = basename + '.bmp'
                else:
                    # Determine the filename extension
                    log.debug(f"Supported formats: {*Image.registered_extensions().values(), }")
                    log.info(f"Trying to find an extension matching the {args.format.upper()} format")
                    extensions = [ extension for (extension, format) in Image.registered_extensions().items() if format == args.format.upper() ]
                    if len(extensions) == 0:
                        sys.exit(f"Image format {args.format.upper()} is not supported")
                    elif len(extensions) == 1:
                        log.info(f"Found extension {extensions[0]}")
                        filename = basename + extensions[0]
                    elif len(extensions) > 1:
                        log.info(f"Found extensions {*extensions, }")
                        # If a format has multiple extensions, prefer the
                        # extension with the same name as the format.
                        preferred_extension = [ extension for extension in extensions if extension == '.' + args.format.lower() ]
                        if preferred_extension:
                            log.info(f"Prefer extension {preferred_extension[0]}")
                            filename = basename + preferred_extension[0]
                        else:
                            sys.exit("The extension could not be determined")
                if args.filename is None:
                    path = filename
                else:
                    path = os.path.join(args.filename, filename)
            elif re.search(single_modulo, args.filename) is not None:
                # filename is a formatting string
                path = str(args.filename % idx)
            else:
                # filename doesn't contain a single % and therefore isn't a pattern
                path = args.filename
                if len(indices) > 1:
                    sys.exit(f"Cannot download {len(indices)} files to 1 filename")
            if args.format is not None and not path.endswith(args.format.lower()):
                log.warning(f"Override format by saving {path} as {args.format.upper()}")
            log.info(f"Saving {path}")
            if args.format == 'bmp' or path.endswith('bmp'):
                # BMP supports images of 1/2/4/8/24-bit color depth
                bmp.save(path)
            else:
                image = bmp_to_pil(bmp)
                image.save(path, format=args.format)

    def put_image(self, args):
        files = args.filename
        image_list = self.gps.get_image_list()
        if args.index is None:
            indices = [ image['idx'] for image in image_list if image['writable'] is True ]
        else:
            indices = args.index
        if len(files) != len(indices):
            sys.exit(f"Cannot upload {len(files)} files to {len(indices)} indices")
        for idx, filename in zip(indices, files):
            basename = image_list[idx].get('name')
            log.info(f"{image_list[idx]['writable']}")
            if not image_list[idx]['writable']:
                sys.exit(f"Image {basename} with index {idx} is not writable")
            # If the file is a BMP with the correct color depth, dimensions, and
            # color table it can be uploaded as is
            try:
                log.info(f"Trying to load {filename} image as a BMP image")
                bmp = MicroBMP().load(filename)
                props = self.gps.image_transfer.get_image_properties(idx)
                bpp = props.bpp
                width = props.width
                height = props.height
                colors_used = props.get_colors_used()
                if bpp != bmp.DIB_depth:
                    raise Exception(f"Image has wrong color depth: expected {bpp} bpp, got {bmp.DIB_depth} bpp")
                if width != bmp.DIB_w or height != bmp.DIB_h:
                    raise Exception(f"Image has wrong dimensions: expected {width}x{height} pixels, got {bmp.DIB_w}x{bmp.DIB_h} pixels")
                # Images with a color depth of 1, 2, 4, or 8 bpp have a color table
                if bpp <= 8:
                    image_id = self.gps.image_transfer.get_image_id(idx)
                    color_table = self.gps.image_transfer.get_color_table(image_id)
                    palette = color_table.get_palette()[:colors_used]
                    if bmp.palette != palette:
                        raise Exception("Image has the wrong color palette")
            # If the file is not a BMP image or it has the wrong attributes, it
            # has to be converted before uploading
            except Exception as e:
                log.info(e)
                try:
                    log.info(f"Trying to load {filename} image as a PIL image")
                    image = Image.open(filename)
                    # Convert PIL image to BMP
                    image_id = self.gps.image_transfer.get_image_id(idx)
                    # PIL images with the modes RGBA, LA, and PA have an alpha
                    # channel. Garmin images use magenta (255, 0, 255) as a transparency
                    # color, so it doesn't display.
                    if image.mode in ('RGBA', 'LA', 'PA'):
                        transparency = props.get_color().get_rgb()
                        log.info(f"Replacing the alpha channel with the transparency color {transparency}")
                        # Create a mask with the transparent pixels converted to black
                        alpha = image.getchannel('A')
                        mask = alpha.convert(mode='1')
                        # Create a background image with the transparency color
                        background = Image.new('RGB', image.size, transparency)
                        # Paste the original image onto  the background image, using the
                        # transparency mask
                        background.paste(image, mask=mask)
                        # Now we have a RGB image the alpha channel of which is replaced
                        # by the transparency color
                        image = background
                    if image.width!= width or image.height != height:
                        log.info(f"Resizing image to {width}x{height} pixels")
                        image = image.resize((width, height))
                    log.info(f"Creating BMP image of {width}x{height} pixels and {bpp} bpp")
                    bmp = MicroBMP(width, height, bpp)
                    # Images with a color depth of 1, 2, 4, or 8 bpp have a palette
                    if bpp in (1, 2, 4, 8):
                        if image.mode != 'P':
                            log.info("Converting image to palette mode (P)")
                            image = image.convert(mode='P')
                        # The palette must be the same as Garmin's
                        color_table = self.gps.image_transfer.get_color_table(image_id)
                        palette = color_table.get_palette()[:colors_used]
                        # The BMP palette is a list of bytearray, and the PIL palette is a byte object
                        if image.palette.palette != b''.join(palette):
                            log.info(f"Quantizing image to the received color palette")
                            image = image.convert(mode='RGB')
                            new_image = Image.new('P', (width, height))
                            new_palette = ImagePalette.ImagePalette(palette=b''.join(palette))
                            new_image.putpalette(new_palette)
                            image = image.quantize(colors=colors_used, palette=new_image)
                        bmp.palette = palette
                        pixel_data = image.tobytes()
                        if bpp != 8:
                            log.info(f"Converting 8 bpp image to {bpp} bpp color depth")
                            pixel_values = to_pixel_values(pixel_data, 8)
                            pixel_data = to_pixel_data(pixel_values, bpp)
                        bmp.parray = pixel_data
                    # Images with a color depth of 24 bpp
                    elif bpp == 24:
                        if image.mode != 'RGB':
                            log.info("Converting image to true color mode (RGB)")
                            image = image.convert(mode='RGB')
                        pixel_data = image.tobytes()
                        bmp.parray = pixel_data
                    else:
                        sys.exit(f"Images of {bpp} bpp are not supported")
                except UnidentifiedImageError as e:
                    log.info(e)
                    sys.exit(f"Unknown image file format")
            log.info(f"Uploading {basename}")
            if args.progress:
                with ProgressBar() as progress_bar:
                    self.gps.put_image(idx, bmp, callback=progress_bar.update_to)
            else:
                self.gps.put_image(idx, bmp)

parser = argparse.ArgumentParser(prog='pygarmin',
                                 description=
"""Command line application to communicate with a Garmin GPS device.

Pygarmin can retrieve information from the device, such as the product
description including the unit ID, the supported protocols, memory properties,
and information on the installed maps. supports bi-directional transfer of
waypoints, routes, track logs, proximity waypoints, maps and images such as
custom waypoint icons. It is able to receive laps, runs, satellite almanac,
current time, current position, and screenshots. It can continuously receive
real-time position, velocity, and time (PVT).

The port is specified with the -p PORT option. To communicate with a Garmin GPS
serially, use the name of that serial port such as /dev/ttyUSB0, /dev/cu.serial,
or COM1. To communicate via USB use usb: as the port on all OSes.
""")
parser.add_argument('-v',
                    '--verbosity',
                    action='count',
                    default=0,
                    help="Increase output verbosity")
parser.add_argument('-D',
                    '--debug',
                    action='store_const',
                    const=3,
                    default=0,
                    help="Enable debugging")
parser.add_argument('--version',
                    action='store_true',
                    help="Dump version and exit")
parser.add_argument('--progress',
                    action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Show progress bar")
parser.add_argument('-p',
                    '--port',
                    default='usb:',
                    help="Set the device name (default: usb:)")
subparsers = parser.add_subparsers(help="Command help")
info = subparsers.add_parser('info', help="Return product description")
info.set_defaults(command='info')
info.add_argument('filename',
                  nargs='?',
                  type=argparse.FileType(mode='w'),
                  default=sys.stdout,
                  # Write output to <file> instead of stdout.
                  help="Set output file")
protocols = subparsers.add_parser('protocols', help="Return protocol capabilities")
protocols.set_defaults(command='protocols')
protocols.add_argument('filename',
                       nargs='?',
                       type=argparse.FileType(mode='w'),
                       default=sys.stdout,
                       help="Set output file")
memory = subparsers.add_parser('memory', help="Return memory info")
memory.set_defaults(command='memory')
memory.add_argument('filename',
                    nargs='?',
                    type=argparse.FileType(mode='w'),
                    default=sys.stdout,
                    help="Set output file")
map_info = subparsers.add_parser('map', help="Return map info")
map_info.set_defaults(command='map_info')
map_info.add_argument('filename',
                 nargs='?',
                 type=argparse.FileType(mode='w'),
                 default=sys.stdout,
                 help="Set output file")
get_waypoints = subparsers.add_parser('get-waypoints', help="Download waypoints")
get_waypoints.set_defaults(command='get_waypoints')
get_waypoints.add_argument('-t',
                           '--format',
                           choices=['txt', 'garmin', 'gpx'],
                           default='garmin',
                           help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``gpx`` returns a string in GPS Exchange Format (GPX).")
get_waypoints.add_argument('filename',
                           nargs='?',
                           type=argparse.FileType(mode='w'),
                           default=sys.stdout,
                           help="Set output file")
put_waypoints = subparsers.add_parser('put-waypoints', help="Upload waypoints")
put_waypoints.set_defaults(command='put_waypoints')
put_waypoints.add_argument('-t',
                           '--format',
                           choices=['txt', 'garmin'],
                           default='garmin',
                           help="Set input format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
put_waypoints.add_argument('filename',
                           nargs='?',
                           type=argparse.FileType(mode='r'),
                           default=sys.stdin,
                           help="Set input file")
get_routes = subparsers.add_parser('get-routes', help="Download routes")
get_routes.set_defaults(command='get_routes')
get_routes.add_argument('-t',
                        '--format',
                        choices=['txt', 'garmin', 'gpx'],
                        default='garmin',
                        help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``gpx`` returns a string in GPS Exchange Format (GPX).")
get_routes.add_argument('filename',
                        nargs='?',
                        type=argparse.FileType(mode='w'),
                        default=sys.stdout,
                        help="Set output file")
put_routes = subparsers.add_parser('put-routes', help="Upload routes")
put_routes.set_defaults(command='put_routes')
put_routes.add_argument('-t',
                        '--format',
                        choices=['txt', 'garmin'],
                        default='garmin',
                        help="Set input format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
put_routes.add_argument('filename',
                        nargs='?',
                        type=argparse.FileType(mode='r'),
                        default=sys.stdin,
                        help="Set input file")
get_tracks = subparsers.add_parser('get-tracks', help="Download tracks")
get_tracks.set_defaults(command='get_tracks')
get_tracks.add_argument('-t',
                        '--format',
                        choices=['txt', 'garmin', 'gpx'],
                        default='garmin',
                        help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``gpx`` returns a string in GPS Exchange Format (GPX).")
get_tracks.add_argument('filename',
                        nargs='?',
                        type=argparse.FileType(mode='w'),
                        default=sys.stdout,
                        help="Set output file")
put_tracks = subparsers.add_parser('put-tracks', help="Upload tracks")
put_tracks.set_defaults(command='put_tracks')
put_tracks.add_argument('-t',
                        '--format',
                        choices=['txt', 'garmin'],
                        default='garmin',
                        help="Set input format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
put_tracks.add_argument('filename',
                        nargs='?',
                        type=argparse.FileType(mode='r'),
                        default=sys.stdin,
                        help="Set input file")
get_proximities = subparsers.add_parser('get-proximities', help="Download proximities")
get_proximities.set_defaults(command='get_proximities')
get_proximities.add_argument('-t',
                             '--format',
                             choices=['txt', 'garmin', 'gpx'],
                             default='garmin',
                             help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``gpx`` returns a string in GPS Exchange Format (GPX).")
get_proximities.add_argument('filename',
                             nargs='?',
                             type=argparse.FileType(mode='w'),
                             default=sys.stdout,
                             help="Set output file")
put_proximities = subparsers.add_parser('put-proximities', help="Upload proximities")
put_proximities.set_defaults(command='put_proximities')
put_proximities.add_argument('-t',
                             '--format',
                             choices=['txt', 'garmin'],
                             default='garmin',
                             help="Set input format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
put_proximities.add_argument('filename',
                             nargs='?',
                             type=argparse.FileType(mode='r'),
                             default=sys.stdin,
                             help="Set input file")
get_almanac = subparsers.add_parser('get-almanac', help="Download almanac")
get_almanac.set_defaults(command='get_almanac')
get_almanac.add_argument('-t',
                         '--format',
                         choices=['txt', 'garmin'],
                         default='garmin',
                         help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
get_almanac.add_argument('filename',
                         nargs='?',
                         type=argparse.FileType(mode='w'),
                         default=sys.stdout,
                         help="Set output file")
get_time = subparsers.add_parser('get-time', help="Download current date and time")
get_time.set_defaults(command='get_time')
get_time.add_argument('-t',
                      '--format',
                      choices=['txt', 'garmin'],
                      default='garmin',
                      help="Set output format. ``txt`` returns a human readable string of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``json`` returns a JSON string of the datatypes.")
get_time.add_argument('filename',
                      nargs='?',
                      type=argparse.FileType(mode='w'),
                      default=sys.stdout,
                      help="Set output file")
get_position = subparsers.add_parser('get-position', help="Download current position")
get_position.set_defaults(command='get_position')
get_position.add_argument('-t',
                          '--format',
                          choices=['txt', 'garmin'],
                          default='garmin',
                          help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
get_position.add_argument('filename',
                          nargs='?',
                          type=argparse.FileType(mode='w'),
                          default=sys.stdout,
                          help="Set output file")
pvt = subparsers.add_parser('pvt', help="Download pvt")
pvt.set_defaults(command='pvt')
pvt.add_argument('-t',
                 '--format',
                 choices=['txt', 'garmin', 'gpsd'],
                 default='garmin',
                 help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype. ``gpsd`` returns a GPSD JSON object.")
pvt.add_argument('filename',
                 nargs='?',
                 type=argparse.FileType(mode='w'),
                 default=sys.stdout,
                 help="Set output file")
get_laps = subparsers.add_parser('get-laps', help="Download laps")
get_laps.set_defaults(command='get_laps')
get_laps.add_argument('-t',
                      '--format',
                      choices=['txt', 'garmin'],
                      default='garmin',
                      help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
get_laps.add_argument('filename',
                      nargs='?',
                      type=argparse.FileType(mode='w'),
                      default=sys.stdout,
                      help="Set output file")
get_runs = subparsers.add_parser('get-runs', help="Download runs")
get_runs.set_defaults(command='get_runs')
get_runs.add_argument('-t',
                      '--format',
                      choices=['txt', 'garmin'],
                      default='garmin',
                      help="Set output format. ``txt`` returns a JSON string that consists of a dictionary with the datatypes attributes. ``garmin`` returns a string that can be executed and will yield the same value as the datatype.")
get_runs.add_argument('filename',
                      nargs='?',
                      type=argparse.FileType(mode='w'),
                      default=sys.stdout,
                      help="Set output file")
get_map = subparsers.add_parser('get-map', help="Download map")
get_map.set_defaults(command='get_map')
get_map.add_argument('filename',
                     nargs='?',
                     default='gmapsupp.img',
                     help="Set output file")
put_map = subparsers.add_parser('put-map', help="Upload map")
put_map.set_defaults(command='put_map')
put_map.add_argument('filename',
                     help="Set input file")
del_map = subparsers.add_parser('del-map', help="Delete map")
del_map.set_defaults(command='del_map')
get_screenshot = subparsers.add_parser('get-screenshot', help="Capture screenshot")
get_screenshot.set_defaults(command='get_screenshot')
get_screenshot.add_argument('-t',
                            '--format',
                            help="Set image file format")
get_screenshot.add_argument('filename',
                            nargs='?',
                            help="Set image filename or directory")
get_image_types = subparsers.add_parser('get-image-types', help="List image types")
get_image_types.add_argument('filename',
                             nargs='?',
                             type=argparse.FileType(mode='w'),
                             default=sys.stdout,
                             help="Set output file")
get_image_types.set_defaults(command='get_image_types')
get_image_list = subparsers.add_parser('get-image-list', help="List images")
get_image_list.add_argument('filename',
                             nargs='?',
                             type=argparse.FileType(mode='w'),
                             default=sys.stdout,
                             help="Set output file")
get_image_list.set_defaults(command='get_image_list')
get_image = subparsers.add_parser('get-image', help="Download image")
get_image.add_argument('-t',
                       '--format',
                       help="Set image file format")
get_image.add_argument('-i',
                       '--index',
                       type=int,
                       nargs='*',
                       help="Indices of the image list to get")
get_image.add_argument('filename',
                       nargs='?',
                       help="Filename or directory to save images. A filename pattern can contain %%d (or any formatting string using the %% operator), since %%d is replaced by the image index. Example: waypoint%%03d.png")
get_image.set_defaults(command='get_image')
put_image = subparsers.add_parser('put-image', help="Upload image")
put_image.add_argument('-i',
                       '--index',
                       type=int,
                       nargs='*',
                       help="Indices of the image list to put")
put_image.add_argument('filename',
                       nargs='+',
                       help="Set input file")
put_image.set_defaults(command='put_image')

def main():
    args = parser.parse_args()
    logging_level = logging_levels.get(max(args.verbosity, args.debug))
    log.setLevel(logging_level)
    log.info(f"Version {__version__}")
    if hasattr(args, 'command'):
        app = Pygarmin(args.port)
        command = getattr(app, args.command)
        command(args)
    elif args.version:
        print(f"pygarmin version {__version__}")
    else:
        parser.print_usage()

if __name__ == '__main__':
    main()
