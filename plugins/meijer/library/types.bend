type Label is Data:
  Public{}
  Private{}

type Labels is Data:
  EndLabels{}
  LabelItem{head: Label, tail: Labels}

type Decision is Data:
  Yes{}
  No{}

type Operation is Data:
  OpInput{}
  OpWrite{}

type CheckKind is Data:
  Tools{}
  Frames{}
  Preconditions{}
  Approvals{}
  Scopes{}
  Flows{}
  Outcomes{}

type Decisions is Data:
  EndDecisions{}
  DecisionItem{head: Decision, tail: Decisions}

type Condition is Data:
  AnyPayload{}
  NonEmpty{}
  EqualInput{index: PGIndex}
  AllOf{left: Condition, right: Condition}

type Conditions is Data:
  EndConditions{}
  ConditionItem{head: Condition, tail: Conditions}

type Count is Data:
  ZeroCount{}
  MoreCount{pred: Count}

type States is Data:
  EndStates{}
  StateItem{head: PGIndex, tail: States}

type Edge is Data:
  DeniedEdge{}
  NextEdge{state: PGIndex}

type Edges is Data:
  EndEdges{}
  EdgeItem{head: Edge, tail: Edges}

type Rows is Data:
  EndRows{}
  RowItem{head: Edges, tail: Rows}

type Machine is Data:
  Machine{initial: PGIndex, accepting: Decisions, rows: Rows}

type Machines is Data:
  EndMachines{}
  MachineItem{head: Machine, tail: Machines}

type Reach is Data:
  ForbiddenTrace{}
  Reachable{states: States}

type Relation is Data:
  EndRelation{}
  RelationItem{head: Reach, tail: Relation}
