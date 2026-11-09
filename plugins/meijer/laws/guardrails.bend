def reads_allowed(reads: PGDependencies) -> Decision:
  match reads:
    case PGNoDependencies{}:
      Yes{}
    case PGDependency{input, rest}:
      operation_allowed(OpInput{})

law tools_safe:
  {mj_both(reads_allowed(program_reads()), every_effect(Tools{}, program_effects())) == Yes{} : Decision}

law frame_safe:
  {every_effect(Frames{}, program_effects()) == Yes{} : Decision}

law scope_safe:
  {mj_both(dependencies_scoped(program_reads()), every_effect(Scopes{}, program_effects())) == Yes{} : Decision}

law preconditions_safe:
  {every_effect(Preconditions{}, program_effects()) == Yes{} : Decision}

law approval_safe:
  {every_effect(Approvals{}, program_effects()) == Yes{} : Decision}
