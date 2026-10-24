"""Navigation marks drawn over the camera's pictures: red rectangles a little below the middle,
labelled with their angle from where the camera points: 0° in the centre (where the boat is
heading) and ±25° either side (+ to the right, clockwise like a compass).

Where an angle lands on the picture: x = centre + FOCAL_WIDTHS * width * tan(angle). The
camera was calibrated against the minute ticks of a wall clock, placed roughly at the dial's
centre and pointing at the 6, so each tick was 6° from the camera (QXGA picture): the ticks
from -42° to +30° fit this within 1-2°. That is a horizontal field of view of about 86°, and
±25° lands a quarter of the width either side of the centre. FHD is assumed to cover the same
width as QXGA (not yet checked against the clock).

The stored picture stays as the camera took it; marked() draws on a copy each time it is
served, so changing the marks changes them on every picture, old ones included. Sizes are
fractions of the picture, so the marks look the same at every resolution.
"""

import io
import math

from PIL import Image, ImageDraw, ImageFont

COLOR = (230, 20, 20)
FOCAL_WIDTHS = 0.536  # the lens's focal length in picture widths (from the clock calibration)
MARK_ANGLES = (-25, 0, 25)  # degrees from the camera's axis, + to the right
CENTER_Y = 0.7  # the rectangles' middle, as a fraction of the height from the top (0.5 = middle)
BOX_W = 0.03  # rectangle size, fractions of the picture's width and height
BOX_H = 0.10
LINE_W = 0.004  # line thickness, fraction of the width
LABEL_H = 0.045  # text height, fraction of the height
JPEG_QUALITY = 90


def draw_mark(draw, x, y, width, height, label):
    """A rectangle centred on (x, y) with its label above it."""
    w, h = BOX_W * width, BOX_H * height
    line = max(2, round(LINE_W * width))
    draw.rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), outline=COLOR, width=line)
    font = ImageFont.load_default(size=max(10, round(LABEL_H * height)))
    draw.text((x, y - h / 2 - line), label, fill=COLOR, font=font, anchor="mb")


def x_of(angle, width):
    """Where a direction `angle` degrees right of the camera's axis appears, in pixels from the left."""
    return width / 2 + FOCAL_WIDTHS * width * math.tan(math.radians(angle))


def marked(jpeg):
    """The JPEG with the navigation marks drawn on it, as a new JPEG."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    for angle in MARK_ANGLES:
        draw_mark(draw, x_of(angle, width), CENTER_Y * height, width, height, "%+d°" % angle if angle else "0°")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()
