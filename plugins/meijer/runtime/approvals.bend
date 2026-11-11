# Private owner records: Pair(Pair(sink, Blob(payload)), rest), where sink is
# unary: Empty is destination zero and Pair(Empty, n) is destination n + 1.
# Decode the whole list before considering a match; malformed tails fail closed.
type mj_ApprovalSink is Data:
  mj_BadApprovalSink{}
  mj_ApprovalSink{index: PGIndex}

type mj_Approvals is Data:
  mj_BadApprovals{}
  mj_NoApprovals{}
  mj_Approval{sink: PGIndex, bytes: PGBytes, rest: mj_Approvals}

def mj_next_sink(value: mj_ApprovalSink) -> mj_ApprovalSink:
  match value:
    case mj_BadApprovalSink{}:
      mj_BadApprovalSink{}
    case mj_ApprovalSink{index}:
      mj_ApprovalSink{PGNext{index}}

def mj_decode_sink(fuel: Count, value: PGPrivate) -> mj_ApprovalSink:
  match fuel:
    case ZeroCount{}:
      mj_BadApprovalSink{}
    case MoreCount{rest}:
      match value:
        case PGEmpty{}:
          mj_ApprovalSink{PGZero{}}
        case PGBlob{bytes}:
          mj_BadApprovalSink{}
        case PGPair{left, right}:
          match left:
            case PGEmpty{}:
              mj_next_sink(mj_decode_sink(rest, right))
            case PGBlob{bytes}:
              mj_BadApprovalSink{}
            case PGPair{first, second}:
              mj_BadApprovalSink{}

def mj_approval_payload(value: PGPrivate) -> mj_Value:
  match value:
    case PGEmpty{}:
      mj_Invalid{}
    case PGBlob{bytes}:
      mj_bound_value(bytes)
    case PGPair{left, right}:
      mj_Invalid{}

def mj_prepend_approval(rest: mj_Approvals, sink: PGIndex, bytes: PGBytes) -> mj_Approvals:
  match rest:
    case mj_BadApprovals{}:
      mj_BadApprovals{}
    case mj_NoApprovals{}:
      mj_Approval{sink, bytes, mj_NoApprovals{}}
    case mj_Approval{other, value, tail}:
      mj_Approval{sink, bytes, mj_Approval{other, value, tail}}

def mj_finish_approval(sink: mj_ApprovalSink, value: mj_Value, rest: mj_Approvals) -> mj_Approvals:
  match sink:
    case mj_BadApprovalSink{}:
      mj_BadApprovals{}
    case mj_ApprovalSink{index}:
      match value:
        case mj_Invalid{}:
          mj_BadApprovals{}
        case mj_Bytes{bytes}:
          mj_prepend_approval(rest, index, bytes)

def mj_decode_record(record: PGPrivate, rest: mj_Approvals) -> mj_Approvals:
  match record:
    case PGEmpty{}:
      mj_BadApprovals{}
    case PGBlob{bytes}:
      mj_BadApprovals{}
    case PGPair{key, payload}:
      mj_finish_approval(mj_decode_sink(maximum_writes(), key), mj_approval_payload(payload), rest)

def mj_decode_approvals(fuel: Count, records: PGPrivate) -> mj_Approvals:
  match fuel:
    case ZeroCount{}:
      match records:
        case PGEmpty{}:
          mj_NoApprovals{}
        case PGBlob{bytes}:
          mj_BadApprovals{}
        case PGPair{record, rest}:
          mj_BadApprovals{}
    case MoreCount{remaining}:
      match records:
        case PGEmpty{}:
          mj_NoApprovals{}
        case PGBlob{bytes}:
          mj_BadApprovals{}
        case PGPair{record, rest}:
          mj_decode_record(record, mj_decode_approvals(remaining, rest))

def sixteen_counts(tail: Count) -> Count:
  four_counts(four_counts(four_counts(four_counts(tail))))

# Enrollment accepts at most 64 approvals.
def mj_approval_limit() -> Count:
  sixteen_counts(sixteen_counts(sixteen_counts(sixteen_counts(ZeroCount{}))))

def mj_owner_approvals(owner: PGOwnerContext) -> mj_Approvals:
  match owner:
    case PGOwnerContext{records}:
      mj_decode_approvals(mj_approval_limit(), records)

def mj_approval_matches(records: mj_Approvals, +sink: PGIndex, +bytes: PGBytes) -> Decision:
  match records:
    case mj_BadApprovals{}:
      No{}
    case mj_NoApprovals{}:
      No{}
    case mj_Approval{expected_sink, expected_bytes, rest}:
      same_sink = index_equal(sink, expected_sink)
      same_payload = mj_bytes_equal(bytes, expected_bytes)
      matches_here = mj_both(same_sink, same_payload)
      matches_later = mj_approval_matches(rest, sink, bytes)
      mj_either(matches_here, matches_later)

def mj_check_required(required: Decision, records: mj_Approvals, sink: PGIndex, bytes: PGBytes) -> Decision:
  match required:
    case Yes{}:
      mj_approval_matches(records, sink, bytes)
    case No{}:
      Yes{}

def mj_approval_for(index: PGIndex, required: Decisions, records: mj_Approvals, sink: PGIndex, bytes: PGBytes) -> Decision:
  match index, required:
    case PGZero{}, EndDecisions{}:
      No{}
    case PGNext{pred}, EndDecisions{}:
      No{}
    case PGZero{}, DecisionItem{head, tail}:
      mj_check_required(head, records, sink, bytes)
    case PGNext{pred}, DecisionItem{head, tail}:
      mj_approval_for(pred, tail, records, sink, bytes)

# Rebuilds the batch, like mj_check_writes: an unapproved write or an unknown
# effect ends it with PGAbort.
def mj_approve_writes(batch: PGIO, +required: Decisions, +records: mj_Approvals) -> PGIO:
  match batch:
    case PGDone{}:
      PGDone{}
    case PGAbort{}:
      PGAbort{}
    case PGWrite{+sink, +bytes, rest}:
      mj_accept_write(mj_approval_for(sink, required, records, sink, bytes), sink, bytes,
        mj_approve_writes(rest, required, records))
    case other:
      PGAbort{}

def mj_validate_approvals(records: mj_Approvals, batch: PGIO, required: Decisions) -> PGIO:
  match records:
    case mj_BadApprovals{}:
      PGAbort{}
    case mj_NoApprovals{}:
      mj_approve_writes(batch, required, mj_NoApprovals{})
    case mj_Approval{sink, bytes, rest}:
      mj_approve_writes(batch, required, mj_Approval{sink, bytes, rest})
