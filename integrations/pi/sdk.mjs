import { findPackageJSON } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

let sdk;
let ai;

export function loadSdk() {
  return sdk ??= import('@earendil-works/pi-coding-agent');
}

export function loadPiAi() {
  // Resolve the SDK's own locked pi-ai, not a second/global installation.
  const metadata = findPackageJSON('@earendil-works/pi-ai', import.meta.resolve('@earendil-works/pi-coding-agent'));
  const pkg = JSON.parse(readFileSync(metadata, 'utf8'));
  if (pkg.name !== '@earendil-works/pi-ai' || pkg.version !== '0.85.1') throw new Error('unexpected SDK dependency');
  return ai ??= import(pathToFileURL(path.resolve(path.dirname(metadata), pkg.exports['.'].import)).href);
}
