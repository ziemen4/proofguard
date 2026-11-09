"""Generic private transport preserves shape, identity and allocation bounds."""

import base64
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import unittest

from bendspec.model import ContractError
from proofguard.context import (
    ExecutionContext, MAX_OWNER_CONTEXT, PrivateBlob, PrivatePair,
    decode_owner_data, encode_owner_data, private_identity,
)
from proofguard.encoding import commitment

CODEC = (Path(__file__).resolve().parents[2] / "bend-spec/backend/io.mjs").as_uri()


def nested(depth):
    value = None
    for _ in range(depth):
        value = {"pair": [None, value]}
    return value


def balanced(depth):
    if depth == 0:
        return None
    return {"pair": [balanced(depth - 1), balanced(depth - 1)]}


class PrivateDataTests(unittest.TestCase):
    def test_roundtrip_is_immutable_and_binds_structure(self):
        wire = {"pair": [{"bytes_base64": "AP8="}, {"pair": [None, {"bytes_base64": ""}]}]}
        value = decode_owner_data(wire)
        self.assertEqual(encode_owner_data(value), wire)
        wire["pair"][0]["bytes_base64"] = "dGFtcGVy"
        self.assertEqual(value.left, PrivateBlob(b"\x00\xff"))
        with self.assertRaises(FrozenInstanceError):
            value.left = None
        expected = b"PGV1\x02\x01\x00\x00\x00\x02\x00\xff\x02\x00\x01\x00\x00\x00\x00"
        self.assertEqual(private_identity(value), commitment("private-data-v1", expected))
        variants = (None, PrivateBlob(b""), PrivatePair(None, None),
                    PrivatePair(PrivateBlob(b"a"), PrivateBlob(b"bc")),
                    PrivatePair(PrivateBlob(b"ab"), PrivateBlob(b"c")),
                    PrivatePair(PrivateBlob(b"bc"), PrivateBlob(b"a")))
        self.assertEqual(len({ExecutionContext((), (item,)).identity for item in variants}), len(variants))

    def test_host_and_evaluator_enforce_identical_shapes(self):
        valid = (None, {"bytes_base64": "AP8="}, nested(128), balanced(9))
        invalid = (True, "", [], {}, {"pair": [None]}, {"pair": [None, None, None]},
                   {"bytes_base64": "AB=="}, {"bytes_base64": "", "pair": [None, None]},
                   nested(129), balanced(10))
        for item in valid:
            self.assertEqual(encode_owner_data(decode_owner_data(item)), item)
        for item in invalid:
            with self.assertRaises(ContractError):
                decode_owner_data(item)
        script = """import fs from 'node:fs';
import {runtimeOwnerTerm} from CODEC;
const [valid,invalid]=JSON.parse(fs.readFileSync(0,'utf8'));
const B={Ctr:(k,x)=>({$:'Ctr',k,x})};
for(const value of valid)runtimeOwnerTerm(B,{schema:1,data:value});
for(const value of invalid){
  let rejected=false;
  try{runtimeOwnerTerm(B,{schema:1,data:value});}catch{rejected=true;}
  if(!rejected)throw Error('invalid data accepted');
}
""".replace("CODEC", json.dumps(CODEC))
        result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                                input=json.dumps((valid, invalid)), capture_output=True,
                                text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cumulative_bytes_and_typed_construction_cannot_bypass_limits(self):
        blob = PrivateBlob(b"x" * MAX_OWNER_CONTEXT)
        ExecutionContext((), (PrivatePair(blob, PrivateBlob(b"")),))
        for item in (PrivatePair(blob, PrivateBlob(b"x")), PrivateBlob(bytearray(b"x")),
                     PrivatePair(None, b"untyped")):
            with self.assertRaises(ContractError):
                ExecutionContext((), (item,))
            with self.assertRaises(ContractError):
                encode_owner_data(item)
        tree = None
        for _ in range(129):
            tree = PrivatePair(None, tree)
        with self.assertRaises(ContractError):
            ExecutionContext((), (tree,))
        wire = {"pair": [{"bytes_base64": base64.b64encode(blob.data).decode()},
                         {"bytes_base64": "eA=="}]}
        with self.assertRaises(ContractError):
            decode_owner_data(wire)
        script = """import fs from 'node:fs';
import {runtimeOwnerTerm} from CODEC;
const data=JSON.parse(fs.readFileSync(0,'utf8'));
let rejected=false;
try{runtimeOwnerTerm({Ctr:(k,x)=>({k,x})},{schema:1,data});}catch{rejected=true;}
if(!rejected)throw Error('aggregate byte overflow accepted');
""".replace("CODEC", json.dumps(CODEC))
        result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                                input=json.dumps(wire), capture_output=True, text=True,
                                timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
