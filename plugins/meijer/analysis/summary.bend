# Meijer-derived facts. Source inputs are positions, never owner classifications.
type PGDependencies is Data:
  PGNoDependencies{}
  PGDependency{input: PGIndex, rest: PGDependencies}

type PGPayloadShape is Data:
  PGEmptyPayload{}
  PGPresentPayload{}
  PGUnknownPayload{}

type PGEffectGraph is Data:
  PGUnknownEffects{}
  PGNoEffects{}
  PGAbortEffect{dependencies: PGDependencies}
  PGWriteEffect{destination: PGIndex, dependencies: PGDependencies, shape: PGPayloadShape, next: PGEffectGraph}
  PGEffectChoice{yes: PGEffectGraph, no: PGEffectGraph}

type PGProgramSummary is Data:
  PGProgramSummary{effects: PGEffectGraph, reads: PGDependencies}

# The source checker replaces this body with facts bound to the selected source.
# Without source analysis, policies see an unsupported graph and must reject it.
def pg_program_summary() -> PGProgramSummary:
  PGProgramSummary{PGUnknownEffects{}, PGNoDependencies{}}
