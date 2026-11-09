# Checks shared by the laws. Every law reads the extractor's summary directly.
def lookup(index: PGIndex, labels: Labels) -> Label:
  match index, labels:
    case PGZero{}, EndLabels{}:
      Private{}
    case PGNext{pred}, EndLabels{}:
      Private{}
    case PGZero{}, LabelItem{head, tail}:
      head
    case PGNext{pred}, LabelItem{head, tail}:
      lookup(pred, tail)

def join(left: Label, right: Label) -> Label:
  match left:
    case Private{}:
      Private{}
    case Public{}:
      right

def meet(left: Label, right: Label) -> Label:
  match left:
    case Public{}:
      Public{}
    case Private{}:
      right

def release(value: Label, clearance: Label) -> Decision:
  match value, clearance:
    case Public{}, Public{}:
      Yes{}
    case Public{}, Private{}:
      Yes{}
    case Private{}, Private{}:
      Yes{}
    case Private{}, Public{}:
      No{}

def decision_at(index: PGIndex, items: Decisions) -> Decision:
  match index, items:
    case PGZero{}, EndDecisions{}:
      No{}
    case PGNext{pred}, EndDecisions{}:
      No{}
    case PGZero{}, DecisionItem{head, tail}:
      head
    case PGNext{pred}, DecisionItem{head, tail}:
      decision_at(pred, tail)

def has_label(index: PGIndex, items: Labels) -> Decision:
  match index, items:
    case PGZero{}, EndLabels{}:
      No{}
    case PGNext{pred}, EndLabels{}:
      No{}
    case PGZero{}, LabelItem{head, tail}:
      Yes{}
    case PGNext{pred}, LabelItem{head, tail}:
      has_label(pred, tail)

def dependency_label(dependencies: PGDependencies) -> Label:
  match dependencies:
    case PGNoDependencies{}:
      Public{}
    case PGDependency{input, rest}:
      join(lookup(input, input_labels()), dependency_label(rest))

def dependencies_scoped(dependencies: PGDependencies) -> Decision:
  match dependencies:
    case PGNoDependencies{}:
      Yes{}
    case PGDependency{input, rest}:
      mj_both(has_label(input, input_labels()), dependencies_scoped(rest))

def condition_at(index: PGIndex, items: Conditions) -> Condition:
  match index, items:
    case PGZero{}, EndConditions{}:
      AnyPayload{}
    case PGNext{pred}, EndConditions{}:
      AnyPayload{}
    case PGZero{}, ConditionItem{head, tail}:
      head
    case PGNext{pred}, ConditionItem{head, tail}:
      condition_at(pred, tail)

# Inputs a concrete condition compares against, independent of the payload.
def condition_label(condition: Condition) -> Label:
  match condition:
    case AnyPayload{}:
      Public{}
    case NonEmpty{}:
      Public{}
    case EqualInput{index}:
      lookup(index, input_labels())
    case AllOf{left, right}:
      join(condition_label(left), condition_label(right))

# A payload already known to be nonempty cannot fail NonEmpty at runtime.
def condition_inspects(condition: Condition, +shape: PGPayloadShape) -> Decision:
  match condition:
    case AnyPayload{}:
      No{}
    case NonEmpty{}:
      match shape:
        case PGEmptyPayload{}:
          Yes{}
        case PGPresentPayload{}:
          No{}
        case PGUnknownPayload{}:
          Yes{}
    case EqualInput{index}:
      Yes{}
    case AllOf{left, right}:
      mj_either(condition_inspects(left, shape), condition_inspects(right, shape))

def condition_possible(condition: Condition, +shape: PGPayloadShape) -> Decision:
  match condition:
    case AnyPayload{}:
      Yes{}
    case NonEmpty{}:
      match shape:
        case PGEmptyPayload{}:
          No{}
        case PGPresentPayload{}:
          Yes{}
        case PGUnknownPayload{}:
          Yes{}
    case EqualInput{index}:
      has_label(index, input_labels())
    case AllOf{left, right}:
      mj_both(condition_possible(left, shape), condition_possible(right, shape))

def veto_label(check: Decision, payload: Label) -> Label:
  match check:
    case Yes{}:
      payload
    case No{}:
      Public{}

# The agent reads every outcome, so a batch's success may depend only on data
# the planner is cleared to see.
def outcome_rule(+sink: PGIndex, +payload: Label, shape: PGPayloadShape) -> Decision:
  +condition = {condition_at(sink, preconditions()) : Condition}
  approval = decision_at(sink, approval_required())
  inspected = mj_either(condition_inspects(condition, shape), mj_either(approval, byte_budget_restricted()))
  release(join(condition_label(condition), veto_label(inspected, payload)), context_label())

def write_rule(kind: CheckKind, +sink: PGIndex, +dependencies: PGDependencies, +shape: PGPayloadShape) -> Decision:
  match kind:
    case Tools{}:
      operation_allowed(OpWrite{})
    case Frames{}:
      decision_at(sink, writable_sinks())
    case Preconditions{}:
      condition_possible(condition_at(sink, preconditions()), shape)
    case Approvals{}:
      decision_at(sink, approval_available())
    case Scopes{}:
      mj_both(has_label(sink, sink_labels()), dependencies_scoped(dependencies))
    case Flows{}:
      release(join(context_label(), dependency_label(dependencies)), lookup(sink, sink_labels()))
    case Outcomes{}:
      outcome_rule(sink, dependency_label(dependencies), shape)

def abort_rule(kind: CheckKind, +dependencies: PGDependencies) -> Decision:
  match kind:
    case Tools{}:
      Yes{}
    case Frames{}:
      Yes{}
    case Preconditions{}:
      Yes{}
    case Approvals{}:
      Yes{}
    case Scopes{}:
      dependencies_scoped(dependencies)
    case Flows{}:
      Yes{}
    case Outcomes{}:
      release(dependency_label(dependencies), context_label())

# Unselected branches are checked too. A missing summary rejects.
def every_effect(+kind: CheckKind, graph: PGEffectGraph) -> Decision:
  match graph:
    case PGUnknownEffects{}:
      No{}
    case PGNoEffects{}:
      Yes{}
    case PGAbortEffect{dependencies}:
      abort_rule(kind, dependencies)
    case PGWriteEffect{+destination, +dependencies, +shape, next}:
      mj_both(write_rule(kind, destination, dependencies, shape), every_effect(kind, next))
    case PGEffectChoice{yes, no}:
      mj_both(every_effect(kind, yes), every_effect(kind, no))

def summary_effects(summary: PGProgramSummary) -> PGEffectGraph:
  match summary:
    case PGProgramSummary{effects, reads}:
      effects

def summary_reads(summary: PGProgramSummary) -> PGDependencies:
  match summary:
    case PGProgramSummary{effects, reads}:
      reads

def program_effects() -> PGEffectGraph:
  summary_effects(pg_program_summary())

def program_reads() -> PGDependencies:
  summary_reads(pg_program_summary())
