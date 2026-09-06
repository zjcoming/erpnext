"""Raster sizes of public/images/process-simplification.svg, using Frappe's Pillow dependency."""

from pathlib import Path

from PIL import Image, ImageDraw


def icon(size: int, maskable: bool = False) -> Image.Image:
	# Supersample the same 64-unit paths as the app's existing SVG logo.
	scale = 32
	image = Image.new("RGBA", (64 * scale, 64 * scale), "#2563eb" if maskable else (0, 0, 0, 0))
	draw = ImageDraw.Draw(image)
	box = lambda values: tuple(value * scale for value in values)
	draw.rounded_rectangle(box((0, 0, 64, 64)), radius=12 * scale, fill="#2563eb")
	for rectangle in ((15, 20, 33, 28), (31, 36, 49, 44)):
		draw.rectangle(box(rectangle), fill="white")
	for line in ((33, 24, 41, 24), (49, 32, 49, 36), (31, 40, 23, 40), (15, 32, 15, 28)):
		draw.line(box(line), fill="#bfdbfe", width=4 * scale)
	draw.arc(box((33, 24, 49, 40)), 270, 360, fill="#bfdbfe", width=4 * scale)
	draw.arc(box((15, 24, 31, 40)), 90, 180, fill="#bfdbfe", width=4 * scale)
	for x, y in ((33, 24), (49, 36), (31, 40), (15, 28)):
		draw.ellipse(box((x - 2, y - 2, x + 2, y + 2)), fill="#bfdbfe")
	return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
	destination = Path(__file__).resolve().parents[1] / "process_simplification/public/images/pwa"
	destination.mkdir(parents=True, exist_ok=True)
	for size in (192, 512):
		icon(size).save(destination / f"icon-{size}.png", optimize=True)
	icon(512, maskable=True).save(destination / "maskable-512.png", optimize=True)
