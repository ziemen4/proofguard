# Owner reference used only for checking. The agent supplies the actual main.
def expected_report(inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}

law owner_report:
  {main == expected_report : PGInputs -> PGIO}
