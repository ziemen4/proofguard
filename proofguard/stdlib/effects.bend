# Primitive IO values are the only application results the host can dispatch.
# Computation, branching, recursion and payload construction remain in Bend.
# Bend string literals desugar to these declarations.
type Bool is Data:
  False{}
  True{}

type Nat is Data:
  Zero{}
  Succ{pred: Nat}

type Word.Nil is Data:
  WNil{}

type Word.Con<-p: Nat> is Data:
  WCon{head: Bool, tail: Word(p)}

def Word(n: Nat) -> Data:
  match n:
    case 0n:
      Word.Nil
    case 1n+p:
      Word.Con<p>

type U32 is Data:
  U32{data: Word(32n)}

type Char is Data:
  Chr{code: U32}

type String is Data:
  SNil{}
  SCon{head: Char, tail: String}

type PGIndex is Data:
  PGZero{}
  PGNext{pred: PGIndex}

type PGNibble is Data:
  PGHex0{}
  PGHex1{}
  PGHex2{}
  PGHex3{}
  PGHex4{}
  PGHex5{}
  PGHex6{}
  PGHex7{}
  PGHex8{}
  PGHex9{}
  PGHexA{}
  PGHexB{}
  PGHexC{}
  PGHexD{}
  PGHexE{}
  PGHexF{}

type PGBytes is Data:
  PGEndBytes{}
  PGByte{high: PGNibble, low: PGNibble, tail: PGBytes}
  PGChunks{left: PGBytes, right: PGBytes}

# Bytes in order with no chunks, so one recursion reaches every byte.
type PGFlat is Data:
  PGFlatEnd{}
  PGFlatByte{high: PGNibble, low: PGNibble, tail: PGFlat}

type PGInputs is Data:
  PGNoInputs{}
  PGInput{bytes: PGBytes, rest: PGInputs}

type PGPrivate is Data:
  PGEmpty{}
  PGBlob{bytes: PGBytes}
  PGPair{left: PGPrivate, right: PGPrivate}

type PGOwnerContext is Data:
  PGOwnerContext{value: PGPrivate}

type PGIO is Data:
  PGDone{}
  PGAbort{}
  PGWrite{destination: PGIndex, bytes: PGBytes, next: PGIO}

def pg_append(left: PGBytes, right: PGBytes) -> PGBytes:
  match left:
    case PGEndBytes{}:
      right
    case PGByte{high, low, tail}:
      PGByte{high, low, pg_append(tail, right)}
    case PGChunks{first, rest}:
      PGChunks{first, pg_append(rest, right)}

def pg_flatten_onto(bytes: PGBytes, rest: PGFlat) -> PGFlat:
  match bytes:
    case PGEndBytes{}:
      rest
    case PGByte{high, low, tail}:
      PGFlatByte{high, low, pg_flatten_onto(tail, rest)}
    case PGChunks{left, right}:
      pg_flatten_onto(left, pg_flatten_onto(right, rest))

def pg_flatten(bytes: PGBytes) -> PGFlat:
  pg_flatten_onto(bytes, PGFlatEnd{})

def pg_unflatten(flat: PGFlat) -> PGBytes:
  match flat:
    case PGFlatEnd{}:
      PGEndBytes{}
    case PGFlatByte{high, low, tail}:
      PGByte{high, low, pg_unflatten(tail)}

def pg_input(index: PGIndex, inputs: PGInputs) -> PGBytes:
  match index, inputs:
    case PGZero{}, PGNoInputs{}:
      PGEndBytes{}
    case PGNext{pred}, PGNoInputs{}:
      PGEndBytes{}
    case PGZero{}, PGInput{bytes, rest}:
      bytes
    case PGNext{pred}, PGInput{bytes, rest}:
      pg_input(pred, rest)

def pg_empty() -> PGBytes:
  PGEndBytes{}

def pg_byte(high: PGNibble, low: PGNibble, tail: PGBytes) -> PGBytes:
  PGByte{high, low, tail}

def pg_write(destination: PGIndex, bytes: PGBytes) -> PGIO:
  PGWrite{destination, bytes, PGDone{}}

# Agent text: pg_text("...") encodes a Bend string as UTF-8 bytes.
def pg_nibble(a: Bool, b: Bool, c: Bool, d: Bool) -> PGNibble:
  match a, b, c, d:
    case False{}, False{}, False{}, False{}:
      PGHex0{}
    case False{}, False{}, False{}, True{}:
      PGHex1{}
    case False{}, False{}, True{}, False{}:
      PGHex2{}
    case False{}, False{}, True{}, True{}:
      PGHex3{}
    case False{}, True{}, False{}, False{}:
      PGHex4{}
    case False{}, True{}, False{}, True{}:
      PGHex5{}
    case False{}, True{}, True{}, False{}:
      PGHex6{}
    case False{}, True{}, True{}, True{}:
      PGHex7{}
    case True{}, False{}, False{}, False{}:
      PGHex8{}
    case True{}, False{}, False{}, True{}:
      PGHex9{}
    case True{}, False{}, True{}, False{}:
      PGHexA{}
    case True{}, False{}, True{}, True{}:
      PGHexB{}
    case True{}, True{}, False{}, False{}:
      PGHexC{}
    case True{}, True{}, False{}, True{}:
      PGHexD{}
    case True{}, True{}, True{}, False{}:
      PGHexE{}
    case True{}, True{}, True{}, True{}:
      PGHexF{}

def pg_octet(a: Bool, b: Bool, c: Bool, d: Bool, e: Bool, f: Bool, g: Bool, h: Bool, tail: PGBytes) -> PGBytes:
  PGByte{pg_nibble(a, b, c, d), pg_nibble(e, f, g, h), tail}

def pg_any(a: Bool, +b: Bool) -> Bool:
  match a:
    case True{}:
      True{}
    case False{}:
      b

def pg_encode(top: Bool, middle: Bool, high: Bool, +b0: Bool, +b1: Bool, +b2: Bool, +b3: Bool, +b4: Bool, +b5: Bool, +b6: Bool, +b7: Bool, +b8: Bool, +b9: Bool, +b10: Bool, +b11: Bool, +b12: Bool, +b13: Bool, +b14: Bool, +b15: Bool, +b16: Bool, +b17: Bool, +b18: Bool, +b19: Bool, +b20: Bool, tail: PGBytes) -> PGBytes:
  match top, middle, high:
    case False{}, False{}, False{}:
      pg_octet(False{}, b6, b5, b4, b3, b2, b1, b0, tail)
    case False{}, False{}, True{}:
      pg_octet(True{}, True{}, False{}, b10, b9, b8, b7, b6,
        pg_octet(True{}, False{}, b5, b4, b3, b2, b1, b0, tail))
    case False{}, True{}, other:
      pg_octet(True{}, True{}, True{}, False{}, b15, b14, b13, b12,
        pg_octet(True{}, False{}, b11, b10, b9, b8, b7, b6,
          pg_octet(True{}, False{}, b5, b4, b3, b2, b1, b0, tail)))
    case True{}, other_middle, other:
      pg_octet(True{}, True{}, True{}, True{}, False{}, b20, b19, b18,
        pg_octet(True{}, False{}, b17, b16, b15, b14, b13, b12,
          pg_octet(True{}, False{}, b11, b10, b9, b8, b7, b6,
            pg_octet(True{}, False{}, b5, b4, b3, b2, b1, b0, tail))))

def pg_any_of(+a: Bool, +b: Bool, +c: Bool, +d: Bool, +e: Bool) -> Bool:
  pg_any(a, pg_any(b, pg_any(c, pg_any(d, e))))

def pg_char(value: Char, tail: PGBytes) -> PGBytes:
  match value:
    case Chr{U32{WCon{+b0, WCon{+b1, WCon{+b2, WCon{+b3, WCon{+b4, WCon{+b5, WCon{+b6, WCon{+b7, WCon{+b8, WCon{+b9, WCon{+b10, WCon{+b11, WCon{+b12, WCon{+b13, WCon{+b14, WCon{+b15, WCon{+b16, WCon{+b17, WCon{+b18, WCon{+b19, WCon{+b20, WCon{b21, WCon{b22, WCon{b23, WCon{b24, WCon{b25, WCon{b26, WCon{b27, WCon{b28, WCon{b29, WCon{b30, WCon{b31, WNil{}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}}:
      top = pg_any_of(b16, b17, b18, b19, b20)
      middle = pg_any_of(b11, b12, b13, b14, b15)
      high = pg_any_of(b7, b8, b9, b10, False{})
      pg_encode(top, middle, high, b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15, b16, b17, b18, b19, b20, tail)

def pg_text(value: String) -> PGBytes:
  match value:
    case SNil{}:
      PGEndBytes{}
    case SCon{head, tail}:
      pg_char(head, pg_text(tail))
