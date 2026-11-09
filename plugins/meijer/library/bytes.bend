# Byte predicates for concrete checks and conversion of analysis literals.
# Comparisons treat PGChunks as concatenated byte sequences.

def mj_either(left: Decision, right: Decision) -> Decision:
  match left:
    case Yes{}:
      Yes{}
    case No{}:
      right

def mj_bytes_nonempty(value: PGBytes) -> Decision:
  match value:
    case PGEndBytes{}:
      No{}
    case PGByte{high, low, tail}:
      Yes{}
    case PGChunks{left, right}:
      mj_either(mj_bytes_nonempty(left), mj_bytes_nonempty(right))

type mj_Quad is Data:
  mj_Q0{}
  mj_Q1{}
  mj_Q2{}
  mj_Q3{}

def mj_top2(value: PGNibble) -> mj_Quad:
  match value:
    case PGHex0{}:
      mj_Q0{}
    case PGHex1{}:
      mj_Q0{}
    case PGHex2{}:
      mj_Q0{}
    case PGHex3{}:
      mj_Q0{}
    case PGHex4{}:
      mj_Q1{}
    case PGHex5{}:
      mj_Q1{}
    case PGHex6{}:
      mj_Q1{}
    case PGHex7{}:
      mj_Q1{}
    case PGHex8{}:
      mj_Q2{}
    case PGHex9{}:
      mj_Q2{}
    case PGHexA{}:
      mj_Q2{}
    case PGHexB{}:
      mj_Q2{}
    case PGHexC{}:
      mj_Q3{}
    case PGHexD{}:
      mj_Q3{}
    case PGHexE{}:
      mj_Q3{}
    case PGHexF{}:
      mj_Q3{}

def mj_low2(value: PGNibble) -> mj_Quad:
  match value:
    case PGHex0{}:
      mj_Q0{}
    case PGHex1{}:
      mj_Q1{}
    case PGHex2{}:
      mj_Q2{}
    case PGHex3{}:
      mj_Q3{}
    case PGHex4{}:
      mj_Q0{}
    case PGHex5{}:
      mj_Q1{}
    case PGHex6{}:
      mj_Q2{}
    case PGHex7{}:
      mj_Q3{}
    case PGHex8{}:
      mj_Q0{}
    case PGHex9{}:
      mj_Q1{}
    case PGHexA{}:
      mj_Q2{}
    case PGHexB{}:
      mj_Q3{}
    case PGHexC{}:
      mj_Q0{}
    case PGHexD{}:
      mj_Q1{}
    case PGHexE{}:
      mj_Q2{}
    case PGHexF{}:
      mj_Q3{}

def mj_quad_equal(left: mj_Quad, right: mj_Quad) -> Decision:
  match left, right:
    case mj_Q0{}, mj_Q0{}:
      Yes{}
    case mj_Q0{}, mj_Q1{}:
      No{}
    case mj_Q0{}, mj_Q2{}:
      No{}
    case mj_Q0{}, mj_Q3{}:
      No{}
    case mj_Q1{}, mj_Q0{}:
      No{}
    case mj_Q1{}, mj_Q1{}:
      Yes{}
    case mj_Q1{}, mj_Q2{}:
      No{}
    case mj_Q1{}, mj_Q3{}:
      No{}
    case mj_Q2{}, mj_Q0{}:
      No{}
    case mj_Q2{}, mj_Q1{}:
      No{}
    case mj_Q2{}, mj_Q2{}:
      Yes{}
    case mj_Q2{}, mj_Q3{}:
      No{}
    case mj_Q3{}, mj_Q0{}:
      No{}
    case mj_Q3{}, mj_Q1{}:
      No{}
    case mj_Q3{}, mj_Q2{}:
      No{}
    case mj_Q3{}, mj_Q3{}:
      Yes{}

def mj_both(left: Decision, right: Decision) -> Decision:
  match left:
    case Yes{}:
      right
    case No{}:
      No{}

def mj_digit_equal(+left: PGNibble, +right: PGNibble) -> Decision:
  mj_both(mj_quad_equal(mj_top2(left), mj_top2(right)), mj_quad_equal(mj_low2(left), mj_low2(right)))

type mj_View is Data:
  mj_ViewEnd{}
  mj_ViewByte{high: PGNibble, low: PGNibble, rest: PGBytes}

type mj_ViewUnit is Data:
  mj_ViewUnit{}

def mj_view_attach(left: mj_View, right: PGBytes, empty: mj_ViewUnit -> mj_View) -> mj_View:
  match left:
    case mj_ViewEnd{}:
      empty(mj_ViewUnit{})
    case mj_ViewByte{high, low, tail}:
      mj_ViewByte{high, low, PGChunks{tail, right}}

# Structural descent finds the first byte, retaining the unvisited subtrees.
def mj_view(value: PGBytes) -> mj_View:
  match value:
    case PGEndBytes{}:
      mj_ViewEnd{}
    case PGByte{high, low, tail}:
      mj_ViewByte{high, low, tail}
    case PGChunks{left, +right}:
      mj_view_attach(mj_view(left), right, tick => mj_view(right))

type mj_Match is Data:
  mj_Different{}
  mj_Matched{remaining: PGBytes}

def mj_accept(equal: Decision, rest: PGBytes) -> mj_Match:
  match equal:
    case Yes{}:
      mj_Matched{rest}
    case No{}:
      mj_Different{}

def mj_compare_view(high: PGNibble, low: PGNibble, view: mj_View) -> mj_Match:
  match view:
    case mj_ViewEnd{}:
      mj_Different{}
    case mj_ViewByte{other_high, other_low, rest}:
      mj_accept(mj_both(mj_digit_equal(high, other_high), mj_digit_equal(low, other_low)), rest)

def mj_take(high: PGNibble, low: PGNibble, state: mj_Match) -> mj_Match:
  match state:
    case mj_Different{}:
      mj_Different{}
    case mj_Matched{remaining}:
      mj_compare_view(high, low, mj_view(remaining))

def mj_continue_match(state: mj_Match, next: mj_Match -> mj_Match) -> mj_Match:
  match state:
    case mj_Different{}:
      mj_Different{}
    case mj_Matched{remaining}:
      next(mj_Matched{remaining})

# Recursion decreases the original left tree, never the reconstructed cursor.
# Delay the remaining traversal once a differing byte has settled the result.
def mj_match_tree(left: PGBytes, state: mj_Match) -> mj_Match:
  match left:
    case PGEndBytes{}:
      state
    case PGByte{high, low, tail}:
      mj_continue_match(mj_take(high, low, state), state => mj_match_tree(tail, state))
    case PGChunks{first, second}:
      mj_continue_match(mj_match_tree(first, state), state => mj_match_tree(second, state))

def mj_not(value: Decision) -> Decision:
  match value:
    case Yes{}:
      No{}
    case No{}:
      Yes{}

def mj_match_complete(state: mj_Match) -> Decision:
  match state:
    case mj_Different{}:
      No{}
    case mj_Matched{remaining}:
      mj_not(mj_bytes_nonempty(remaining))

def mj_bytes_equal(left: PGBytes, right: PGBytes) -> Decision:
  mj_match_complete(mj_match_tree(left, mj_Matched{right}))
