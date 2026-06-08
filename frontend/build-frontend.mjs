import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const source = resolve(root, "frontend");
const output = resolve(source, "dist");
const apiBase = (process.env.SKETCH_API_BASE || "").trim().replace(/\/+$/, "");
const deploymentProfile = (process.env.DEPLOYMENT_PROFILE || "local").trim().toLowerCase();
const staticFiles = ["index.html", "style.css", "sketch.js", "demo.js"];

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
for (const file of staticFiles) {
  await cp(resolve(source, file), resolve(output, file), { force: true });
}
await writeFile(
  resolve(output, "config.js"),
  `window.SKETCH_API_BASE = ${JSON.stringify(apiBase)};\n` +
  `window.DEPLOYMENT_PROFILE = ${JSON.stringify(deploymentProfile)};\n`,
);
