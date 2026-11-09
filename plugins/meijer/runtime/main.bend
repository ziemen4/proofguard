# Execute the admitted agent implementation, then validate its complete batch.
def dispatch(+inputs: PGInputs, owner: PGOwnerContext) -> PGIO:
  approvals = mj_owner_approvals(owner)
  computed = main(inputs)
  checked = mj_validate_batch(computed, inputs, preconditions(), writable_sinks(),
    per_write_budget(), total_byte_budget(), concrete_write_budget())
  mj_validate_approvals(approvals, checked, approval_required())
