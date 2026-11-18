"""Synthetic illustrated page and transcription, containing no personal material."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

TRANSCRIPT = "SYNTHETIC LAYOUT TEST CLEAR WORDS AND NUMBERS 12345"
PICTURE_BOXES = [(70, 380, 430, 690), (550, 890, 930, 1240)]


def illustrated_page() -> Image.Image:
    image = Image.new("RGB", (1000, 1400), (247, 246, 242))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=26)
    for left, top in ((70, 70), (550, 450)):
        for index, text in enumerate(("SYNTHETIC LAYOUT TEST", "CLEAR WORDS AND", "NUMBERS 12345")):
            draw.text((left, top + index * 55), text, font=font, fill=(20, 20, 20))
    for index, box in enumerate(PICTURE_BOXES):
        width, height = box[2] - box[0], box[3] - box[1]
        y, x = np.mgrid[:height, :width]
        rgb = np.stack(
            (
                40 + x * 180 // width,
                50 + y * 160 // height,
                40 + ((x // 7 + y // 9 + index) % 12) * 14,
            ),
            axis=2,
        ).astype(np.uint8)
        image.paste(Image.fromarray(rgb), box)
    return image
