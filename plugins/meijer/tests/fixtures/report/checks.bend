type ReportDecision is Data:
  ReportYes{}
  ReportNo{}

def report_both(left: ReportDecision, right: ReportDecision) -> ReportDecision:
  match left:
    case ReportNo{}: ReportNo{}
    case ReportYes{}: right

def report_dependencies(value: PGDependencies) -> ReportDecision:
  match value:
    case PGNoDependencies{}: ReportYes{}
    case PGDependency{input, rest}:
      match input:
        case PGZero{}: report_dependencies(rest)
        case PGNext{pred}: ReportNo{}

def report_effects(value: PGEffectGraph) -> ReportDecision:
  match value:
    case PGUnknownEffects{}: ReportNo{}
    case PGNoEffects{}: ReportYes{}
    case PGAbortEffect{dependencies}: report_dependencies(dependencies)
    case PGWriteEffect{destination, dependencies, shape, next}:
      match destination:
        case PGZero{}: report_both(report_dependencies(dependencies), report_effects(next))
        case PGNext{pred}: ReportNo{}
    case PGEffectChoice{yes, no}:
      report_both(report_effects(yes), report_effects(no))

def report_accept(summary: PGProgramSummary) -> ReportDecision:
  match summary:
    case PGProgramSummary{effects, reads}: report_effects(effects)

law report_safe:
  {report_accept(pg_program_summary()) == ReportYes{} : ReportDecision}
