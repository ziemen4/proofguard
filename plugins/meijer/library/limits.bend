# Binary counters keep byte and structural bounds shallow in the interpreter.
# The least significant bit is outermost; comparisons also accept zero padding.
type mj_Natural is Data:
  mj_Zero{}
  mj_Bit0{rest: mj_Natural}
  mj_Bit1{rest: mj_Natural}

type mj_Order is Data:
  mj_Less{}
  mj_Equal{}
  mj_Greater{}

def mj_increment(value: mj_Natural) -> mj_Natural:
  match value:
    case mj_Zero{}:
      mj_Bit1{mj_Zero{}}
    case mj_Bit0{rest}:
      mj_Bit1{rest}
    case mj_Bit1{rest}:
      mj_Bit0{mj_increment(rest)}

def mj_add(left: mj_Natural, right: mj_Natural) -> mj_Natural:
  match left:
    case mj_Zero{}:
      right
    case mj_Bit0{a}:
      match right:
        case mj_Zero{}:
          mj_Bit0{a}
        case mj_Bit0{b}:
          mj_Bit0{mj_add(a, b)}
        case mj_Bit1{b}:
          mj_Bit1{mj_add(a, b)}
    case mj_Bit1{a}:
      match right:
        case mj_Zero{}:
          mj_Bit1{a}
        case mj_Bit0{b}:
          mj_Bit1{mj_add(a, b)}
        case mj_Bit1{b}:
          mj_Bit0{mj_increment(mj_add(a, b))}

def mj_zero_order(value: mj_Natural) -> mj_Order:
  match value:
    case mj_Zero{}:
      mj_Equal{}
    case mj_Bit0{rest}:
      mj_zero_order(rest)
    case mj_Bit1{rest}:
      mj_Less{}

def mj_reverse_order(order: mj_Order) -> mj_Order:
  match order:
    case mj_Less{}:
      mj_Greater{}
    case mj_Equal{}:
      mj_Equal{}
    case mj_Greater{}:
      mj_Less{}

def mj_low_order(higher: mj_Order, lower: mj_Order) -> mj_Order:
  match higher:
    case mj_Less{}:
      mj_Less{}
    case mj_Equal{}:
      lower
    case mj_Greater{}:
      mj_Greater{}

def mj_compare(left: mj_Natural, right: mj_Natural) -> mj_Order:
  match left:
    case mj_Zero{}:
      mj_zero_order(right)
    case mj_Bit0{a}:
      match right:
        case mj_Zero{}:
          mj_reverse_order(mj_zero_order(mj_Bit0{a}))
        case mj_Bit0{b}:
          mj_compare(a, b)
        case mj_Bit1{b}:
          mj_low_order(mj_compare(a, b), mj_Less{})
    case mj_Bit1{a}:
      match right:
        case mj_Zero{}:
          mj_Greater{}
        case mj_Bit0{b}:
          mj_low_order(mj_compare(a, b), mj_Greater{})
        case mj_Bit1{b}:
          mj_compare(a, b)

def mj_order_within(order: mj_Order) -> Decision:
  match order:
    case mj_Less{}:
      Yes{}
    case mj_Equal{}:
      Yes{}
    case mj_Greater{}:
      No{}

def mj_within(value: mj_Natural, bound: mj_Natural) -> Decision:
  mj_order_within(mj_compare(value, bound))

def mj_measure(bytes: PGBytes) -> mj_Natural:
  match bytes:
    case PGEndBytes{}:
      mj_Zero{}
    case PGByte{high, low, tail}:
      mj_increment(mj_measure(tail))
    case PGChunks{left, right}:
      mj_add(mj_measure(left), mj_measure(right))

def mj_bounded_bytes(bytes: PGBytes, bound: mj_Natural) -> Decision:
  mj_within(mj_measure(bytes), bound)
