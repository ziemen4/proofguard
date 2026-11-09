import path from 'node:path';
import { constants } from 'node:fs';
import { lstat, open, realpath } from 'node:fs/promises';
import { LIMITS } from './config.mjs';

export async function noLinks(filename) {
  const absolute = path.resolve(filename);
  let current = path.parse(absolute).root;
  for (const part of absolute.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if ((await lstat(current)).isSymbolicLink()) throw new Error('symlink is not readable');
  }
  return absolute;
}

export async function readRegular(filename, { roots, limit = LIMITS.file, published = false } = {}) {
  const absolute = path.resolve(filename);
  if (roots && !roots.some(root => absolute === root || absolute.startsWith(root + path.sep))) {
    throw new Error('outside readable workspace');
  }
  await noLinks(absolute);
  const file = await open(absolute, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = await file.stat({ bigint: true });
    if (!before.isFile() || !published && before.nlink !== 1n || before.size > BigInt(limit)) {
      throw new Error('readable file limit');
    }
    const data = Buffer.alloc(limit + 1);
    let size = 0;
    while (size < data.length) {
      const { bytesRead } = await file.read(data, size, data.length - size, null);
      if (!bytesRead) break;
      size += bytesRead;
    }
    const after = await file.stat({ bigint: true });
    // Owner results are immutable hardlink publications. Removing the temporary
    // link changes ctime/nlink but not the captured final inode's content.
    const stamps = published ? ['dev', 'ino', 'size', 'mtimeNs'] : ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs', 'nlink'];
    if (size > limit || stamps.some(key => before[key] !== after[key])) {
      throw new Error('file changed during capture');
    }
    return data.subarray(0, size);
  } finally { await file.close(); }
}

export async function readableRoots(roots) {
  return Promise.all(roots.map(async root => {
    const absolute = await noLinks(root);
    if (await realpath(absolute) !== absolute || !(await lstat(absolute)).isDirectory()) {
      throw new Error('readable directory required');
    }
    return absolute;
  }));
}

export function utf8(data) {
  return new TextDecoder('utf-8', { fatal: true }).decode(data);
}
