# -*- coding: utf-8 -*-
"""Multi-Threaded Color Changer

Contains several basic "Color-Following" functions, as well as custom Stop/Start threads for these effects.
"""
import logging
import threading
from functools import lru_cache
from typing import List, Tuple

import mss
import numexpr as ne
import numpy as np

# from lib.color_functions import dominant_color
from PIL import Image
from lifxlan import utils

from .utils import str2list
from ..ui.settings import config


@lru_cache(maxsize=32)
def get_monitor_bounds(func):
    """ Returns the rectangular coordinates of the desired Avg. Screen area. Can pass a function to find the result
    procedurally """
    return func() or config["AverageColor"]["DefaultMonitor"]


def get_screen_as_image():
    """Grabs the entire primary screen as an image"""
    with mss.mss() as sct:
        monitor = sct.monitors[0]

        # Capture a bbox using percent values
        left = monitor["left"]  # + monitor["width"] * 5 // 100  # 5% from the left
        top = monitor["top"]  # + monitor["height"] * 5 // 100  # 5% from the top
        right = monitor["left"] + monitor["width"]  # left + 400  # 400px width
        lower = monitor["top"] + monitor["height"]  # top + 400  # 400px height
        bbox = (left, top, right, lower)
        sct_img = sct.grab(bbox)
        return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")


def get_rect_as_image(bounds: Tuple[int, int, int, int]):
    """ Grabs a rectangular area of the primary screen as an image """
    with mss.mss() as sct:
        monitor = {
            "left": bounds[0],
            "top": bounds[1],
            "width": bounds[2],
            "height": bounds[3],
        }
        sct_img = sct.grab(monitor)
        return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")


def normalize_rectangles(rects: List[Tuple[int, int, int, int]]):
    """ Normalize the rectangles to the monitor size """
    x_min = min(rect[0] for rect in rects)
    y_min = min(rect[1] for rect in rects)
    return [
        (-x_min + left, -y_min + top, -x_min + right, -y_min + bottom,)
        for left, top, right, bottom in rects
    ]


def avg_screen_color(initial_color, func_bounds=lambda: None):
    """ Capture an image of the monitor defined by func_bounds, then get the average color of the image in HSBK """
    monitor = get_monitor_bounds(func_bounds)
    if "full" in monitor:
        screenshot = get_screen_as_image()
    else:
        screenshot = get_rect_as_image(str2list(monitor, int))
    # Resizing the image to 1x1 pixel will give us the average for the whole image (via HAMMING interpolation)
    color = screenshot.resize((1, 1), Image.HAMMING).getpixel((0, 0))
    return list(utils.RGBtoHSBK(color, temperature=initial_color[3]))


def dominant_screen_color(initial_color, func_bounds=lambda: None):
    """
    Gets the dominant color of the screen defined by func_bounds
    https://stackoverflow.com/questions/50899692/most-dominant-color-in-rgb-image-opencv-numpy-python
    """
    monitor = get_monitor_bounds(func_bounds)
    if "full" in monitor:
        screenshot = get_screen_as_image()
    else:
        screenshot = get_rect_as_image(str2list(monitor, int))

    downscale_width, downscale_height = screenshot.width // 4, screenshot.height // 4
    screenshot = screenshot.resize((downscale_width, downscale_height), Image.HAMMING)

    a = np.array(screenshot)
    a2D = a.reshape(-1, a.shape[-1])
    col_range = (256, 256, 256)  # generically : a2D.max(0)+1
    eval_params = {
        "a0": a2D[:, 0],
        "a1": a2D[:, 1],
        "a2": a2D[:, 2],
        "s0": col_range[0],
        "s1": col_range[1],
    }
    a1D = ne.evaluate("a0*s0*s1+a1*s0+a2", eval_params)
    color = np.unravel_index(np.bincount(a1D).argmax(), col_range)

    return list(utils.RGBtoHSBK(color, temperature=initial_color[3]))


class ColorThread(threading.Thread):
    """ A Simple Thread which runs when the _stop event isn't set """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, daemon=True, **kwargs)
        self._stop = threading.Event()

    def stop(self):
        """ Stop thread by setting event """
        self._stop.set()

    def stopped(self):
        """ Check if thread has been stopped """
        return self._stop.isSet()


class ColorThreadRunner:
    """ Manages an asynchronous color-change with a Device. Can be run continuously, stopped and started. """

    def __init__(self, bulb, color_function, parent, continuous=True, **kwargs):
        self.bulb = bulb
        self.color_function = color_function
        self.kwargs = kwargs
        self.parent = parent  # couple to parent frame
        self.logger = logging.getLogger(
            parent.logger.name + f".Thread({color_function.__name__})"
        )
        self.prev_color = parent.get_color_values_hsbk()
        self.continuous = continuous
        self.thread = ColorThread(target=self.match_color, args=(self.bulb,))
        try:
            label = self.bulb.get_label()
        except:  # pylint: disable=bare-except
            # If anything goes wrong in getting the label just set it to ERR; we really don't care except for logging.
            label = "<LABEL-ERR>"
        self.logger.info(
            "Initialized Thread: Bulb: %s // Continuous: %s", label, self.continuous
        )

    def match_color(self, bulb):
        """ ColorThread target which calls the 'change_color' function on the bulb. """
        self.logger.debug("Starting color match.")
        self.prev_color = (
            self.parent.get_color_values_hsbk()
        )  # coupling to LightFrame from gui.py here
        while not self.thread.stopped():
            try:
                color = list(
                    self.color_function(initial_color=self.prev_color, **self.kwargs)
                )
                color[2] = min(color[2] + self.get_brightness_offset(), 65535)
                bulb.set_color(
                    color, duration=self.get_duration() * 1000, rapid=self.continuous
                )
                self.prev_color = color
            except OSError:
                # This is dirty, but we really don't care, just keep going
                self.logger.info("Hit an os error")
                continue
            if not self.continuous:
                self.stop()
        self.logger.debug("Color match finished.")

    def start(self):
        """ Start the match_color thread"""
        if self.thread.stopped():
            self.thread = ColorThread(target=self.match_color, args=(self.bulb,))
            self.thread.setDaemon(True)
        try:
            self.thread.start()
            self.logger.debug("Thread started.")
        except RuntimeError:
            self.logger.error("Tried to start ColorThread again.")

    def stop(self):
        """ Stop the match_color thread"""
        self.thread.stop()

    @staticmethod
    def get_duration():
        """ Read the transition duration from the config file. """
        return float(config["AverageColor"]["duration"])

    @staticmethod
    def get_brightness_offset():
        """ Read the brightness offset from the config file. """
        return int(config["AverageColor"]["brightnessoffset"])


def install_thread_excepthook():
    """
    Workaround for sys.excepthook thread bug
    (https://sourceforge.net/tracker/?func=detail&atid=105470&aid=1230540&group_label=5470).
    Call once from __main__ before creating any threads.
    If using psyco, call psycho.cannotcompile(threading.Thread.run)
    since this replaces a new-style class method.
    """
    import sys

    run_old = threading.Thread.run

    def run(*args, **kwargs):
        """ Monkey-patch for the run function that installs local excepthook """
        try:
            run_old(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:  # pylint: disable=bare-except
            sys.excepthook(*sys.exc_info())

    threading.Thread.run = run


install_thread_excepthook()
