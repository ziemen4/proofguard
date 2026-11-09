# Concrete byte checks shared by private preconditions and approval decoding.
type mj_Value is Data:
  mj_Invalid{}
  mj_Bytes{bytes: PGBytes}

def mj_input(index: PGIndex, inputs: PGInputs) -> mj_Value:
  match index, inputs:
    case PGZero{}, PGNoInputs{}: mj_Invalid{}
    case PGNext{pred}, PGNoInputs{}: mj_Invalid{}
    case PGZero{}, PGInput{bytes, rest}: mj_Bytes{bytes}
    case PGNext{pred}, PGInput{bytes, rest}: mj_input(pred, rest)

def mj_accept_bytes(check: Decision, bytes: PGBytes) -> mj_Value:
  match check:
    case Yes{}: mj_Bytes{bytes}
    case No{}: mj_Invalid{}

def mj_bound_value(+bytes: PGBytes) -> mj_Value:
  mj_accept_bytes(mj_bounded_bytes(bytes, payload_limit()), bytes)
