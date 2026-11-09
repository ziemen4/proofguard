// The site's page table. Each entry maps a Markdown file in the repository to a
// page slug, so the repository files stay the only copy of the documentation.
// The sync step and the sidebar both read this table. The "index" slug is the home page.
export const sections = [
  {
    label: "Getting started",
    pages: [
      { source: "docs/introduction.md", slug: "index" },
      { source: "docs/quickstart.md", slug: "quickstart" },
    ],
  },
  {
    label: "Concepts",
    pages: [
      { source: "docs/concepts/foundation.md", slug: "concepts/foundation" },
      { source: "docs/concepts/plugins.md", slug: "concepts/plugins" },
      { source: "docs/concepts/harnesses.md", slug: "concepts/harnesses" },
      { source: "docs/concepts/private-data.md", slug: "concepts/private-data" },
      { source: "docs/concepts/security-model.md", slug: "concepts/security-model" },
    ],
  },
  {
    label: "Guides",
    pages: [
      { source: "docs/guides/write-a-plugin.md", slug: "guides/write-a-plugin" },
      { source: "docs/guides/add-a-harness.md", slug: "guides/add-a-harness" },
    ],
  },
  {
    label: "Plugins",
    pages: [
      { source: "plugins/meijer/README.md", slug: "plugins/meijer" },
      { source: "examples/foundation/README.md", slug: "plugins/report-example" },
    ],
  },
  {
    label: "Harnesses",
    pages: [
      { source: "integrations/pi/README.md", slug: "harnesses/pi" },
    ],
  },
  {
    label: "Reference",
    pages: [
      { source: "docs/reference/configuration.md", slug: "reference/configuration" },
      { source: "docs/reference/limits.md", slug: "reference/limits" },
      { source: "bend-spec/README.md", slug: "reference/toolchain" },
      { source: "docs/reference/ci.md", slug: "reference/ci", title: "CI and releases" },
    ],
  },
];

export const pages = sections.flatMap((section) => section.pages);
