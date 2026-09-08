// Rasterize the company's original https://www.hengsuankeji.com/heng.svg.
// Requires sharp; HENGSUAN_SHARP_PATH may point at an existing installation.
const fs = require("node:fs");
const path = require("node:path");
const sharp = require(process.env.HENGSUAN_SHARP_PATH || "sharp");

async function main() {
	const images = path.resolve(__dirname, "../process_simplification/public/images");
	const source = fs.readFileSync(path.join(images, "hengsuan.svg"));
	const destination = path.join(images, "pwa/hengsuan");
	fs.mkdirSync(destination, { recursive: true });
	for (const size of [192, 512]) {
		await sharp(source, { density: 1152 }).resize(size, size)
			.png().toFile(path.join(destination, `icon-${size}.png`));
	}
	await sharp(source, { density: 1152 }).resize(512, 512)
		.flatten({ background: "#0f172a" }).png()
		.toFile(path.join(destination, "maskable-512.png"));
}

main().catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
