// Builds the docs of every release tag into one GitHub Pages tree: the newest
// release at the root and each release under /vX.Y.Z/. Run from the repository
// root with full tag history: node site/scripts/build-releases.mjs BASE [ORIGIN]
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const [basePath = "/", origin = ""] = process.argv.slice(2);
const root = basePath.endsWith("/") ? basePath : `${basePath}/`;
const output = path.resolve("site/release-dist");
const run = (command, args, options = {}) =>
  execFileSync(command, args, { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], ...options });
const hasSite = (tag) => {
  try {
    run("git", ["cat-file", "-e", `${tag}:site/package.json`]);
    return true;
  } catch {
    return false;
  }
};

// Releases are vMAJOR.MINOR.PATCH tags that contain the site, newest first.
const releases = run("git", ["tag", "--list", "v*", "--sort=-v:refname"])
  .split("\n").filter((tag) => /^v\d+\.\d+\.\d+$/.test(tag) && hasSite(tag));
if (releases.length === 0) throw new Error("no release tag contains site/");

rmSync(output, { recursive: true, force: true });
for (const [index, tag] of releases.entries()) {
  const checkout = mkdtempSync(path.join(tmpdir(), "proofguard-docs-"));
  run("git", ["worktree", "add", "--detach", checkout, tag]);
  try {
    const site = path.join(checkout, "site");
    run("npm", ["ci", "--ignore-scripts", "--no-audit", "--no-fund"], { cwd: site });
    // The newest release is built twice: once for the root, once for its own path.
    for (const prefix of index === 0 ? ["", `${tag}/`] : [`${tag}/`]) {
      run("npm", ["run", "build"], {
        cwd: site,
        env: {
          ...process.env, SITE_BASE: `${root}${prefix}`, SITE_ORIGIN: origin, SITE_ROOT: root,
          SITE_RELEASES: releases.join(","), SITE_REF: tag,
        },
      });
      cpSync(path.join(site, "dist"), path.join(output, prefix), { recursive: true });
    }
  } finally {
    run("git", ["worktree", "remove", "--force", checkout]);
  }
}
console.log(`built ${releases.length} releases into ${output}`);
