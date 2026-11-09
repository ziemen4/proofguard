/** Check the owner runner after implementation fills, before managed proofs.
 * Bend checks definitions in event order. Moving this single frozen definition
 * lets its body call a filled agent slot; all bodies still undergo normal checks.
 */
export function deferOwnerEntrypoint(book, inputs) {
  const name = inputs.execution_entrypoint;
  if (name === undefined) return () => {};
  if (typeof name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_.]*$/.test(name)
      || inputs.contract.project.proofs.includes(name)) {
    throw new Error('Invalid owner execution entrypoint');
  }
  const definition = book.tlds[name];
  if (definition?.$ !== 'Def') throw new Error('Execution entrypoint must be declared by owner');
  if (definition.v === null) {
    if (!inputs.contract.project.implementations.includes(name)) {
      throw new Error('Open execution entrypoint is not an implementation slot');
    }
    return () => {};
  }
  book.order = book.order.filter(item => item !== name);
  return () => { book.order.push(name); };
}
