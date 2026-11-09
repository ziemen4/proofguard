# A finite state set is carried across BOTH branch successors. This avoids
# accepting a later operation under only the convenient branch's state.
def index_equal(left: PGIndex, right: PGIndex) -> Decision:
  match left, right:
    case PGZero{}, PGZero{}:
      Yes{}
    case PGZero{}, PGNext{right}:
      No{}
    case PGNext{left}, PGZero{}:
      No{}
    case PGNext{left}, PGNext{right}:
      index_equal(left, right)

def state_member(+state: PGIndex, states: States) -> Decision:
  match states:
    case EndStates{}:
      No{}
    case StateItem{head, tail}:
      mj_either(index_equal(state, head), state_member(state, tail))

def insert_state(state: PGIndex, states: States, exists: Decision) -> States:
  match exists:
    case Yes{}:
      states
    case No{}:
      StateItem{state, states}

def union_states(left: States, +right: States) -> States:
  match left:
    case EndStates{}:
      right
    case StateItem{+head, tail}:
      union_states(tail, insert_state(head, right, state_member(head, right)))

def merge_reach(left: Reach, right: Reach) -> Reach:
  match left, right:
    case ForbiddenTrace{}, ForbiddenTrace{}:
      ForbiddenTrace{}
    case ForbiddenTrace{}, Reachable{states}:
      ForbiddenTrace{}
    case Reachable{states}, ForbiddenTrace{}:
      ForbiddenTrace{}
    case Reachable{left}, Reachable{right}:
      Reachable{union_states(left, right)}

def edge_at(sink: PGIndex, edges: Edges) -> Reach:
  match sink, edges:
    case PGZero{}, EndEdges{}:
      ForbiddenTrace{}
    case PGNext{pred}, EndEdges{}:
      ForbiddenTrace{}
    case PGNext{pred}, EdgeItem{head, tail}:
      edge_at(pred, tail)
    case PGZero{}, EdgeItem{head, tail}:
      match head:
        case DeniedEdge{}:
          ForbiddenTrace{}
        case NextEdge{state}:
          Reachable{StateItem{state, EndStates{}}}

def relation_at(state: PGIndex, relation: Relation) -> Reach:
  match state, relation:
    case PGZero{}, EndRelation{}:
      ForbiddenTrace{}
    case PGNext{pred}, EndRelation{}:
      ForbiddenTrace{}
    case PGZero{}, RelationItem{head, tail}:
      head
    case PGNext{pred}, RelationItem{head, tail}:
      relation_at(pred, tail)

def follow_states(states: States, +relation: Relation) -> Reach:
  match states:
    case EndStates{}:
      Reachable{EndStates{}}
    case StateItem{head, tail}:
      merge_reach(relation_at(head, relation), follow_states(tail, relation))

def follow_reach(reach: Reach, relation: Relation) -> Reach:
  match reach:
    case ForbiddenTrace{}:
      ForbiddenTrace{}
    case Reachable{states}:
      follow_states(states, relation)

def compose_relations(first: Relation, +second: Relation) -> Relation:
  match first:
    case EndRelation{}:
      EndRelation{}
    case RelationItem{head, tail}:
      RelationItem{follow_reach(head, second), compose_relations(tail, second)}

def union_relations(left: Relation, right: Relation) -> Relation:
  match left, right:
    case EndRelation{}, EndRelation{}:
      EndRelation{}
    case EndRelation{}, RelationItem{head, tail}:
      EndRelation{}
    case RelationItem{head, tail}, EndRelation{}:
      RelationItem{ForbiddenTrace{}, union_relations(tail, EndRelation{})}
    case RelationItem{left, ls}, RelationItem{right, rs}:
      RelationItem{merge_reach(left, right), union_relations(ls, rs)}

def identity_relation(rows: Rows, +index: PGIndex) -> Relation:
  match rows:
    case EndRows{}:
      EndRelation{}
    case RowItem{head, tail}:
      RelationItem{Reachable{StateItem{index, EndStates{}}}, identity_relation(tail, PGNext{index})}

def write_relation(rows: Rows, +sink: PGIndex) -> Relation:
  match rows:
    case EndRows{}:
      EndRelation{}
    case RowItem{head, tail}:
      RelationItem{edge_at(sink, head), write_relation(tail, sink)}

def denied_relation(rows: Rows) -> Relation:
  match rows:
    case EndRows{}:
      EndRelation{}
    case RowItem{head, tail}:
      RelationItem{ForbiddenTrace{}, denied_relation(tail)}

def effect_relation(graph: PGEffectGraph, +rows: Rows) -> Relation:
  match graph:
    case PGUnknownEffects{}:
      denied_relation(rows)
    case PGNoEffects{}:
      identity_relation(rows, PGZero{})
    case PGAbortEffect{dependencies}:
      identity_relation(rows, PGZero{})
    case PGWriteEffect{destination, dependencies, shape, next}:
      compose_relations(write_relation(rows, destination), effect_relation(next, rows))
    case PGEffectChoice{yes, no}:
      union_relations(effect_relation(yes, rows), effect_relation(no, rows))

def states_accept(states: States, +accepting: Decisions) -> Decision:
  match states:
    case EndStates{}:
      Yes{}
    case StateItem{head, tail}:
      mj_both(decision_at(head, accepting), states_accept(tail, accepting))

def trace_accept(reach: Reach, accepting: Decisions) -> Decision:
  match reach:
    case ForbiddenTrace{}:
      No{}
    case Reachable{states}:
      states_accept(states, accepting)

def machine_accept(graph: PGEffectGraph, machine: Machine) -> Decision:
  match machine:
    case Machine{initial, accepting, rows}:
      trace_accept(relation_at(initial, effect_relation(graph, rows)), accepting)

def machines_accept(+graph: PGEffectGraph, machines: Machines) -> Decision:
  match machines:
    case EndMachines{}:
      Yes{}
    case MachineItem{head, tail}:
      mj_both(machine_accept(graph, head), machines_accept(graph, tail))

def bounded_sequence(graph: PGEffectGraph, budget_ok: Decision) -> Decision:
  match budget_ok:
    case No{}:
      No{}
    case Yes{}:
      machines_accept(graph, security_machines())

law sequence_safe:
  {bounded_sequence(program_effects(), count_le(effect_cost(program_effects()), write_budget())) == Yes{} : Decision}
