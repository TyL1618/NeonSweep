"""產生 NeonSweep 的 icon.ico(透明底 + 粉藍漸層圓環,呼應 App 的 cyberpunk 主題)。
只在需要重新產生圖示時手動執行:python scripts/gen_icon.py
"""

import os

from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]
BASE_SIZE = 256

NEON_PINK = (255, 46, 136)
NEON_BLUE = (0, 229, 255)
BG = (0, 0, 0, 0)  # 全透明,去掉黑底


def make_base_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)

    margin = size * 0.10
    ring_width = max(size * 0.11, 2)
    bbox = [margin, margin, size - margin, size - margin]

    steps = 90
    for i in range(steps):
        t = i / steps
        r = int(NEON_PINK[0] * (1 - t) + NEON_BLUE[0] * t)
        g = int(NEON_PINK[1] * (1 - t) + NEON_BLUE[1] * t)
        b = int(NEON_PINK[2] * (1 - t) + NEON_BLUE[2] * t)
        start_angle = 360 * i / steps
        end_angle = 360 * (i + 1) / steps + 1
        draw.arc(bbox, start=start_angle, end=end_angle, fill=(r, g, b, 255), width=int(ring_width))

    return img


def main() -> None:
    base = make_base_icon(BASE_SIZE)
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.ico")
    base.save(out_path, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"寫入 {out_path}")


if __name__ == "__main__":
    main()
