# Counts saturate at seventeen, one beyond the runtime's maximum batch.
def count_le(left: Count, right: Count) -> Decision:
  match left, right:
    case ZeroCount{}, ZeroCount{}:
      Yes{}
    case ZeroCount{}, MoreCount{right}:
      Yes{}
    case MoreCount{left}, ZeroCount{}:
      No{}
    case MoreCount{left}, MoreCount{right}:
      count_le(left, right)

def count_max(left: Count, right: Count) -> Count:
  match left, right:
    case ZeroCount{}, ZeroCount{}:
      ZeroCount{}
    case ZeroCount{}, MoreCount{right}:
      MoreCount{right}
    case MoreCount{left}, ZeroCount{}:
      MoreCount{left}
    case MoreCount{left}, MoreCount{right}:
      MoreCount{count_max(left, right)}

def limited_add(limit: Count, +left: Count, +right: Count) -> Count:
  match limit:
    case ZeroCount{}:
      ZeroCount{}
    case MoreCount{rest}:
      match left:
        case ZeroCount{}:
          match right:
            case ZeroCount{}:
              ZeroCount{}
            case MoreCount{tail}:
              MoreCount{limited_add(rest, ZeroCount{}, tail)}
        case MoreCount{tail}:
          MoreCount{limited_add(rest, tail, right)}

def four_counts(tail: Count) -> Count:
  MoreCount{MoreCount{MoreCount{MoreCount{tail}}}}

# The runtime accepts at most sixteen writes. Seventeen marks overflow.
def maximum_writes() -> Count:
  four_counts(four_counts(four_counts(four_counts(ZeroCount{}))))

def ceiling() -> Count:
  MoreCount{maximum_writes()}

def effect_cost(graph: PGEffectGraph) -> Count:
  match graph:
    case PGUnknownEffects{}:
      ceiling()
    case PGNoEffects{}:
      ZeroCount{}
    case PGAbortEffect{dependencies}:
      ZeroCount{}
    case PGWriteEffect{destination, dependencies, shape, next}:
      limited_add(ceiling(), MoreCount{ZeroCount{}}, effect_cost(next))
    case PGEffectChoice{yes, no}:
      count_max(effect_cost(yes), effect_cost(no))

law budget_safe:
  {count_le(effect_cost(program_effects()), write_budget()) == Yes{} : Decision}
