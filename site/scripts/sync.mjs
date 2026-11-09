// Copies the repository's Markdown into Starlight's content folder.
//
// Each page's first "# Heading" becomes its title. Links between documented
// files become site links; links to other repository files point at the
// repository on GitHub, because the site only carries the documentation.
// Images are copied into public/docs-assets/ under their repository path.
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pages } from "../pages.mjs";

const site = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const root = path.dirname(site);
const output = path.join(site, "src/content/docs");
const assets = path.join(site, "public/docs-assets");
const base = (process.env.SITE_BASE ?? "/").replace(/\/?$/, "/");
const repository = process.env.SITE_REPOSITORY ?? "https://github.com/ziemen4/proofguard";
// Release builds link source files at their tag; other builds link main.
const ref = process.env.SITE_REF ?? "main";
const bySource = new Map(pages.map((page) => [page.source, page]));

function resolve(file, from) {
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(from), file));
  if (resolved.startsWith("..")) throw new Error(`${from}: link leaves the repository: ${file}`);
  // The site's link checker only sees site pages, so check repository links here.
  if (!existsSync(path.join(root, resolved))) throw new Error(`${from}: broken link: ${file}`);
  return resolved;
}

const images = new Set();

function image(href, from) {
  const resolved = resolve(href, from);
  images.add(resolved);
  return `${base}docs-assets/${resolved}`;
}

function target(href, from) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("#") || href.startsWith("/")) return href;
  const [file, anchor] = href.split("#");
  const resolved = resolve(file, from);
  const suffix = anchor ? `#${anchor}` : "";
  const page = bySource.get(resolved);
  if (page) return `${base}${page.slug === "index" ? "" : `${page.slug}/`}${suffix}`;
  return `${repository}/blob/${ref}/${resolved}${suffix}`;
}

function convert(page, text) {
  const heading = text.match(/^# (.+)\n+/);
  if (!heading) throw new Error(`${page.source}: missing a leading "# " title`);
  let body = text.slice(heading[0].length);
  // Rewrite inline Markdown links outside fenced code.
  body = body
    .split(/(^```[\s\S]*?^```)/m)
    .map((part, index) => index % 2 ? part : part
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, href) => `![${alt}](${image(href, page.source)})`)
      .replace(/\]\(([^)\s]+)\)/g, (_, href) => `](${target(href, page.source)})`))
    .join("");
  const title = page.title ?? heading[1].trim();
  // GitHub edits only branches, so every version's edit link opens main.
  const edit = `${repository}/edit/main/${page.source}`;
  return `---\ntitle: ${JSON.stringify(title)}\neditUrl: ${JSON.stringify(edit)}\n---\n\n${body}`;
}

// Every page and asset here is generated and ignored by git.
await rm(output, { recursive: true, force: true });
await rm(assets, { recursive: true, force: true });
for (const page of pages) {
  const destination = path.join(output, `${page.slug}.md`);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, convert(page, await readFile(path.join(root, page.source), "utf8")));
}
for (const file of images) {
  const destination = path.join(assets, file);
  await mkdir(path.dirname(destination), { recursive: true });
  await copyFile(path.join(root, file), destination);
}
console.log(`synced ${pages.length} pages and ${images.size} images`);
