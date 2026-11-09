# Owner-side reference implementation for this example's law.
def write_report(inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}

# This example requires function equality for every input, not a sampled run.
# The checker closes the obligation after parsing the agent's main definition.
law owner_report:
  {main == write_report : PGInputs -> PGIO}
