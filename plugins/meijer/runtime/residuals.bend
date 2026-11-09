# Check concrete payloads after Bend evaluation and before any host write.
# Scope: conditions, destination frames and inclusive per-write/whole-batch
# resource budgets. Approval checks are outside this module.
def mj_equal_input(value: mj_Value, bytes: PGBytes) -> Decision:
  match value:
    case mj_Invalid{}:
      No{}
    case mj_Bytes{expected}:
      mj_bytes_equal(expected, bytes)

def mj_condition_holds(condition: Condition, +bytes: PGBytes, +inputs: PGInputs) -> Decision:
  match condition:
    case AnyPayload{}:
      Yes{}
    case NonEmpty{}:
      mj_bytes_nonempty(bytes)
    case EqualInput{index}:
      mj_equal_input(mj_input(index, inputs), bytes)
    case AllOf{left, right}:
      mj_both(mj_condition_holds(left, bytes, inputs), mj_condition_holds(right, bytes, inputs))

def mj_condition_for(sink: PGIndex, conditions: Conditions, +bytes: PGBytes, +inputs: PGInputs) -> Decision:
  match sink, conditions:
    case PGZero{}, EndConditions{}:
      No{}
    case PGNext{pred}, EndConditions{}:
      No{}
    case PGZero{}, ConditionItem{head, tail}:
      mj_condition_holds(head, bytes, inputs)
    case PGNext{pred}, ConditionItem{head, tail}:
      mj_condition_for(pred, tail, bytes, inputs)

def mj_frame_for(sink: PGIndex, writable: Decisions) -> Decision:
  match sink, writable:
    case PGZero{}, EndDecisions{}:
      No{}
    case PGNext{pred}, EndDecisions{}:
      No{}
    case PGZero{}, DecisionItem{head, tail}:
      head
    case PGNext{pred}, DecisionItem{head, tail}:
      mj_frame_for(pred, tail)

def mj_batch_bytes(batch: PGIO) -> mj_Natural:
  match batch:
    case PGDone{}:
      mj_Zero{}
    case PGAbort{}:
      mj_Zero{}
    case PGWrite{sink, bytes, rest}:
      mj_add(mj_measure(bytes), mj_batch_bytes(rest))

def mj_batch_writes(batch: PGIO) -> mj_Natural:
  match batch:
    case PGDone{}:
      mj_Zero{}
    case PGAbort{}:
      mj_Zero{}
    case PGWrite{sink, bytes, rest}:
      mj_increment(mj_batch_writes(rest))

def mj_payload_rules(batch: PGIO, +inputs: PGInputs, +conditions: Conditions, +writable: Decisions, +per_write: mj_Natural) -> Decision:
  match batch:
    case PGDone{}:
      Yes{}
    case PGAbort{}:
      No{}
    case PGWrite{+sink, +bytes, rest}:
      destination_safe = mj_frame_for(sink, writable)
      size_safe = mj_bounded_bytes(bytes, per_write)
      condition_safe = mj_condition_for(sink, conditions, bytes, inputs)
      rest_safe = mj_payload_rules(rest, inputs, conditions, writable, per_write)
      mj_both(destination_safe, mj_both(size_safe, mj_both(condition_safe, rest_safe)))

def mj_accept_batch(check: Decision, batch: PGIO) -> PGIO:
  match check:
    case Yes{}:
      batch
    case No{}:
      PGAbort{}

def mj_validate_batch(+batch: PGIO, inputs: PGInputs, conditions: Conditions, writable: Decisions, per_write: mj_Natural, total: mj_Natural, writes: mj_Natural) -> PGIO:
  payloads_safe = mj_payload_rules(batch, inputs, conditions, writable, per_write)
  total_bytes_safe = mj_within(mj_batch_bytes(batch), total)
  write_count_safe = mj_within(mj_batch_writes(batch), writes)
  all_safe = mj_both(payloads_safe, mj_both(total_bytes_safe, write_count_safe))
  mj_accept_batch(all_safe, batch)
