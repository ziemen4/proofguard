import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { open, rename, unlink } from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { LIMITS, TIMEOUTS, boundedText, keys } from './config.mjs';
import { noLinks, readRegular, utf8 } from './files.mjs';

export function sourceCommitment(source) {
  const hash = createHash('sha256').update('PGW1');
  for (const field of [Buffer.from('source'), Buffer.from(source)]) {
    const length = Buffer.alloc(8);
    length.writeBigUInt64BE(BigInt(field.length));
    hash.update(length).update(field);
  }
  return hash.digest('hex');
}

async function syncDirectory(directory) {
  const fd = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await fd.sync(); } finally { await fd.close(); }
}

export function validateResult(result, proposal, sourceId) {
  const fields = ['schema', 'proposal', 'status', 'reason', 'source_id', 'check_id', 'diagnostics', 'archive_durable'];
  if (!keys(result, fields) || result.schema !== 1 || result.proposal !== proposal || result.source_id !== sourceId ||
      !['completed', 'rejected', 'uncertain'].includes(result.status) ||
      ![null, true, false].includes(result.archive_durable) ||
      !(result.check_id === null || /^[0-9a-f]{64}$/.test(result.check_id)) ||
      !(result.reason === null || boundedText(result.reason, 1024)) ||
      !(result.diagnostics === null || typeof result.diagnostics === 'string' &&
        Buffer.byteLength(result.diagnostics) <= 48000 && [...result.diagnostics].length <= 12000)) {
    throw new Error('unbound owner result');
  }
  return result;
}

export function createOutboxClient({ outbox = '/outbox', results = '/results',
  timeoutMs = TIMEOUTS.result, pollMs = TIMEOUTS.poll, onEvent = () => {} } = {}) {
  let terminal = false;
  let busy = false;
  let attempts = 0;
  const closed = new AbortController();
  const client = {
    get terminal() { return terminal; },
    get busy() { return busy; },
    close() { terminal = true; closed.abort(); },
    async submit(source, { signal } = {}) {
      if (terminal || busy || attempts >= LIMITS.proposals) throw new Error('submission unavailable');
      if (!boundedText(source, LIMITS.program)) throw new Error('invalid Bend program');
      busy = true;
      attempts++;
      const abort = signal ? AbortSignal.any([signal, closed.signal]) : closed.signal;
      let temporary;
      try {
        abort.throwIfAborted();
        await noLinks(outbox);
        await noLinks(results);
        const proposal = randomUUID().replaceAll('-', '');
        const sourceId = sourceCommitment(source);
        temporary = path.join(outbox, `.${proposal}.upload`);
        const file = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
        try { await file.writeFile(source, 'utf8'); await file.sync(); } finally { await file.close(); }
        abort.throwIfAborted();
        await rename(temporary, path.join(outbox, `${proposal}.bend`));
        temporary = undefined;
        await syncDirectory(outbox);
        onEvent({ type: 'proposal', proposal, source_id: sourceId, program: source });
        const deadline = performance.now() + timeoutMs;
        while (performance.now() < deadline) {
          abort.throwIfAborted();
          let data;
          try { data = await readRegular(path.join(results, `${proposal}.json`), { limit: LIMITS.result, published: true }); }
          catch (error) { if (error.code !== 'ENOENT') throw error; }
          if (data) {
            const result = validateResult(JSON.parse(utf8(data)), proposal, sourceId);
            abort.throwIfAborted();
            // This fences the conversation, not the owner's enrollment. A
            // completed request leaves later explicit submissions available.
            if (result.status === 'uncertain') terminal = true;
            onEvent({ type: 'proposal_result', proposal, result });
            return result;
          }
          await pause(pollMs, undefined, { signal: abort });
        }
        throw new Error('owner result timeout; reconciliation required');
      } catch {
        terminal = true;
        throw new Error('submission outcome unobserved; owner reconciliation required');
      } finally {
        busy = false;
        if (temporary) await unlink(temporary).catch(() => {});
      }
    },
  };
  return client;
}
