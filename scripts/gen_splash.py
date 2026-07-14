"""產生 onefile 打包用的啟動畫面 splash.png(見 NeonSweep.spec 的 Splash())。

PyInstaller 的 Splash 機制會在 bootloader 解壓縮完成、Python 都還沒開始跑之前就先顯示
這張圖,用來告訴使用者「程式正在啟動,不是當掉」——這是刻意保留 onefile 單一檔案散布
方式(見 DEVDOC.md §13.1:smartctl.exe 打包進 onefile archive 會讓每次啟動都重新解壓,
容易被防毒軟體重新掃描,啟動可能要等到 1 分鐘)的配套措施。

只在需要重新產生圖片時手動執行:python scripts/gen_splash.py
"""

import os

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 480, 280
NEON_PINK = (255, 46, 136)
NEON_BLUE = (0, 229, 255)
BG = (5, 5, 8)
TEXT_MAIN = (232, 232, 240)
TEXT_DIM = (138, 138, 154)


def make_splash() -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    ring_size = 96
    cx, cy = WIDTH // 2, HEIGHT // 2 - 34
    bbox = [cx - ring_size / 2, cy - ring_size / 2, cx + ring_size / 2, cy + ring_size / 2]
    steps = 90
    ring_width = 10
    for i in range(steps):
        t = i / steps
        r = int(NEON_PINK[0] * (1 - t) + NEON_BLUE[0] * t)
        g = int(NEON_PINK[1] * (1 - t) + NEON_BLUE[1] * t)
        b = int(NEON_PINK[2] * (1 - t) + NEON_BLUE[2] * t)
        start_angle = 360 * i / steps
        end_angle = 360 * (i + 1) / steps + 1
        draw.arc(bbox, start=start_angle, end=end_angle, fill=(r, g, b), width=ring_width)

    try:
        title_font = ImageFont.truetype("segoeuib.ttf", 30)
    except OSError:
        title_font = ImageFont.load_default()
    try:
        sub_font = ImageFont.truetype("msjh.ttc", 13)  # 微軟正黑體,含繁中字型
    except OSError:
        sub_font = ImageFont.load_default()

    title = "NeonSweep"
    tw = draw.textlength(title, font=title_font)
    draw.text((WIDTH / 2 - tw / 2, cy + ring_size / 2 + 20), title, font=title_font, fill=TEXT_MAIN)

    sub = "硬碟多合一任務工具"
    sw = draw.textlength(sub, font=sub_font)
    draw.text((WIDTH / 2 - sw / 2, cy + ring_size / 2 + 60), sub, font=sub_font, fill=TEXT_DIM)

    return img


def main() -> None:
    img = make_splash()
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "splash.png")
    img.save(out_path, format="PNG")
    print(f"寫入 {out_path}")


if __name__ == "__main__":
    main()
