def output_clearance(graph: PGEffectGraph) -> Label:
  match graph:
    case PGUnknownEffects{}:
      Private{}
    case PGNoEffects{}:
      Private{}
    case PGAbortEffect{dependencies}:
      Private{}
    case PGWriteEffect{destination, dependencies, shape, next}:
      meet(lookup(destination, sink_labels()), output_clearance(next))
    case PGEffectChoice{yes, no}:
      meet(output_clearance(yes), output_clearance(no))

law flow_safe:
  {release(context_label(), output_clearance(program_effects())) == Yes{} : Decision}

law data_safe:
  {every_effect(Flows{}, program_effects()) == Yes{} : Decision}
