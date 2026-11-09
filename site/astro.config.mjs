// Docs site configuration. SITE_ORIGIN and SITE_BASE come from GitHub Pages in
// CI; local builds serve from "/".
import { readFileSync } from "node:fs";
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import starlightLinksValidator from "starlight-links-validator";
import { sections } from "./pages.mjs";

const repository = process.env.SITE_REPOSITORY ?? "https://github.com/ziemen4/proofguard";
// VERSION is the repository's single version number. Release builds also pass
// every released tag (newest first) and the Pages root, for the version switcher.
const version = `v${readFileSync(new URL("../VERSION", import.meta.url), "utf8").trim()}`;
const releases = (process.env.SITE_RELEASES ?? "").split(",").filter(Boolean);
const root = process.env.SITE_ROOT ?? process.env.SITE_BASE ?? "/";

export default defineConfig({
  site: process.env.SITE_ORIGIN || undefined,
  base: process.env.SITE_BASE ?? "/",
  vite: {
    define: {
      __DOCS_VERSION__: JSON.stringify(version),
      __DOCS_RELEASES__: JSON.stringify(releases),
      __DOCS_ROOT__: JSON.stringify(root.endsWith("/") ? root : `${root}/`),
    },
  },
  integrations: [
    starlight({
      title: "ProofGuard",
      description: "ProofGuard separates planning from execution for AI agents: the agent writes a Bend2 program, and the owner's side checks it before executing it.",
      logo: { src: "./src/assets/logo.svg" },
      favicon: "/favicon.svg",
      social: [{ icon: "github", label: "GitHub", href: repository }],
      customCss: ["./src/styles/custom.css"],
      components: { SiteTitle: "./src/components/SiteTitle.astro" },
      sidebar: sections.map((section) => ({
        label: section.label,
        items: section.pages.map((page) => page.slug === "index" ? { label: "Introduction", link: "/" } : page.slug),
      })),
      // Bend2 has no Shiki grammar; its syntax is close enough to Python.
      expressiveCode: { shiki: { langAlias: { bend: "python" } } },
      plugins: [starlightLinksValidator({ errorOnRelativeLinks: false })],
    }),
  ],
});
