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

def mj_accept_write(check: Decision, sink: PGIndex, bytes: PGBytes, rest: PGIO) -> PGIO:
  match check:
    case Yes{}:
      PGWrite{sink, bytes, rest}
    case No{}:
      PGAbort{}

# A PGIO is used once, so the batch is checked while it is rebuilt. spent and
# written include the current write. A failed check ends the batch with PGAbort;
# the host reads the whole batch before any write, so earlier writes are cancelled
# too. Effects this plugin does not know are refused.
def mj_check_writes(batch: PGIO, +inputs: PGInputs, +conditions: Conditions, +writable: Decisions, +per_write: mj_Natural, +total: mj_Natural, +writes: mj_Natural, spent: mj_Natural, written: mj_Natural) -> PGIO:
  match batch:
    case PGDone{}:
      PGDone{}
    case PGAbort{}:
      PGAbort{}
    case PGWrite{+sink, +bytes, rest}:
      +now_spent = {mj_add(mj_measure(bytes), spent) : mj_Natural}
      +now_written = {mj_increment(written) : mj_Natural}
      destination_safe = mj_frame_for(sink, writable)
      size_safe = mj_bounded_bytes(bytes, per_write)
      condition_safe = mj_condition_for(sink, conditions, bytes, inputs)
      total_bytes_safe = mj_within(now_spent, total)
      write_count_safe = mj_within(now_written, writes)
      write_safe = mj_both(destination_safe, mj_both(size_safe, mj_both(condition_safe, mj_both(total_bytes_safe, write_count_safe))))
      mj_accept_write(write_safe, sink, bytes,
        mj_check_writes(rest, inputs, conditions, writable, per_write, total, writes, now_spent, now_written))
    case other:
      PGAbort{}

def mj_validate_batch(batch: PGIO, inputs: PGInputs, conditions: Conditions, writable: Decisions, per_write: mj_Natural, total: mj_Natural, writes: mj_Natural) -> PGIO:
  mj_check_writes(batch, inputs, conditions, writable, per_write, total, writes, mj_Zero{}, mj_Zero{})
