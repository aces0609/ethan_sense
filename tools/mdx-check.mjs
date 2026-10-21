#!/usr/bin/env node
// Compile an .mdx file with @mdx-js/mdx (GFM tables + footnotes, YAML frontmatter).
// Exit 0 on success and print a JSON summary; exit 1 with the error on failure.
// Usage: node tools/mdx-check.mjs path/to/primer.mdx

import { readFile } from "node:fs/promises";
import { compile } from "@mdx-js/mdx";
import remarkFrontmatter from "remark-frontmatter";
import remarkGfm from "remark-gfm";

const path = process.argv[2];
if (!path) {
  console.error("usage: mdx-check.mjs <file.mdx>");
  process.exit(2);
}

try {
  const source = await readFile(path, "utf8");
  const result = await compile(source, {
    remarkPlugins: [remarkFrontmatter, remarkGfm],
    format: "mdx",
  });
  const code = String(result);
  // JSX in prose would show up as imported components; the contract forbids imports/JSX.
  const hasImport = /^import\s/m.test(source);
  const summary = {
    ok: true,
    bytes: source.length,
    compiledBytes: code.length,
    hasImport,
  };
  console.log(JSON.stringify(summary));
  process.exit(hasImport ? 1 : 0);
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) }));
  process.exit(1);
}
