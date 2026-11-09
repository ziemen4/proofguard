/** Fill owner-selected nullary equality laws with checker-validated reflexivity terms. */
export function fillObligations(B, book, inputs, candidateStart) {
  if (!Object.hasOwn(inputs, 'automatic_obligations')) return;
  if (inputs.automatic_obligations !== true) throw new Error('Invalid automatic obligation mode');
  const names = inputs.contract.project.proofs;
  const proposed = new Set(book.order.slice(candidateStart));
  for (const name of names) {
    if (!/^[A-Za-z_][A-Za-z0-9_.]*$/.test(name)) throw new Error('Invalid managed obligation name');
    if (proposed.has(name)) throw new Error(`Gate-managed obligation cannot be authored by candidate: ${name}`);
    const declaration = book.tlds[name];
    if (declaration?.$ !== 'Def' || declaration.v !== null || declaration.n !== 0 || declaration.x !== 0
        || B.term_lower(declaration.T).$ !== 'Eql') {
      throw new Error(`Automatic obligations require nullary equality laws: ${name}`);
    }
  }
  const source = names.map(name => `def ${name}():\n  {==}\n`).join('\n');
  B.parse_book(book, '', source, '', Object.create(null));
}
