"""The host codec accepts concrete primitive IO, not application semantics."""

import base64
import json
from pathlib import Path
import subprocess
import unittest

from bendspec.model import ContractError
from proofguard.effects import ProgramAborted, decode_writes

CODEC = (Path(__file__).resolve().parents[2] / "bend-spec/backend/io.mjs").as_uri()


def ctr(name, *args):
    return {"$": "Ctr", "k": name, "x": list(args)}


def encode_frame(value):
    """Exercise the actual evaluator-side codec, with no application interpreter."""
    script = ("import fs from 'node:fs'; import {encodePrimitiveIO} from " + json.dumps(CODEC)
              + "; process.stdout.write(JSON.stringify(encodePrimitiveIO(JSON.parse(fs.readFileSync(0,'utf8'))))); ")
    result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                            input=json.dumps(value), capture_output=True, text=True, timeout=10,
                            env={"PATH": "/usr/bin:/bin"})
    if result.returncode != 0:
        raise ContractError("primitive encoder rejected input")
    return json.loads(result.stdout)


class PrimitiveTests(unittest.TestCase):
    def test_abort_discards_a_computed_prefix_instead_of_returning_writes(self):
        prefix = ctr("PGWrite", ctr("PGZero"), ctr("PGEndBytes"), ctr("PGAbort"))
        for value in (ctr("PGAbort"), prefix):
            frame = encode_frame(value)
            self.assertEqual(frame, {"schema": 1, "abort": True})
            with self.assertRaises(ProgramAborted):
                decode_writes(frame)
        for value in ({"schema": 1, "abort": False}, {"schema": True, "abort": True},
                      {"schema": 1, "abort": True, "writes": []}):
            with self.assertRaises(ContractError):
                decode_writes(value)

    def test_all_bytes_decode_without_text_conversion(self):
        value = ctr("PGEndBytes")
        digits = "0123456789ABCDEF"
        for byte in reversed(range(256)):
            value = ctr("PGByte", ctr("PGHex" + digits[byte >> 4]), ctr("PGHex" + digits[byte & 15]), value)
        frame = encode_frame(ctr("PGWrite", ctr("PGZero"), value, ctr("PGDone")))
        self.assertEqual(decode_writes(frame)[0].data, bytes(range(256)))

    def test_application_nodes_and_functions_are_not_executable_primitives(self):
        for value in (ctr("Branch", ctr("PGDone")), ctr("Write", ctr("Input")),
                      {"$": "Ref", "k": "main"}, ctr("PGDone", ctr("PGDone"))):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    encode_frame(value)

    def test_complete_batch_rejects_an_invalid_later_write(self):
        valid = ctr("PGWrite", ctr("PGZero"), ctr("PGEndBytes"), ctr("PGDone"))
        self.assertEqual(len(decode_writes(encode_frame(valid))), 1)
        malformed = ctr("PGWrite", ctr("PGZero"), ctr("PGEndBytes"), ctr("NativeCallback"))
        with self.assertRaises(ContractError):
            encode_frame(malformed)
        batch = ctr("PGDone")
        for _ in range(17):
            batch = ctr("PGWrite", ctr("PGZero"), ctr("PGEndBytes"), batch)
        with self.assertRaises(ContractError):
            encode_frame(batch)

    def test_indexes_and_digits_are_bounded(self):
        index = ctr("PGZero")
        for _ in range(15):
            index = ctr("PGNext", index)
        frame = encode_frame(ctr("PGWrite", index, ctr("PGEndBytes"), ctr("PGDone")))
        self.assertEqual(decode_writes(frame)[0].capability, 15)
        with self.assertRaises(ContractError):
            encode_frame(ctr("PGWrite", ctr("PGNext", index), ctr("PGEndBytes"), ctr("PGDone")))
        with self.assertRaises(ContractError):
            encode_frame(ctr("PGWrite", ctr("PGZero"),
                             ctr("PGByte", ctr("PGHexG"), ctr("PGHex0"), ctr("PGEndBytes")), ctr("PGDone")))

    def test_chunked_bytes_preserve_order(self):
        left = ctr("PGByte", ctr("PGHex0"), ctr("PGHex0"), ctr("PGEndBytes"))
        right = ctr("PGByte", ctr("PGHexF"), ctr("PGHexF"), ctr("PGEndBytes"))
        value = ctr("PGWrite", ctr("PGZero"), ctr("PGChunks", left, right), ctr("PGDone"))
        self.assertEqual(decode_writes(encode_frame(value))[0].data, b"\x00\xff")

    def test_private_input_codec_constructs_data_without_parsing_source(self):
        script = ("import {runtimeInputTerm,encodePrimitiveIO} from " + json.dumps(CODEC)
                  + "; const Ctr=(k,x)=>({$:'Ctr',k,x}); const t=runtimeInputTerm({Ctr},['AP8=']);"
                    "process.stdout.write(JSON.stringify(encodePrimitiveIO(Ctr('PGWrite',"
                    "[Ctr('PGZero',[]),t.x[0],Ctr('PGDone',[])]))));")
        result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decode_writes(json.loads(result.stdout))[0].data, b"\x00\xff")

    def test_owner_context_codec_is_distinct_bounded_closed_data(self):
        script = ("import {runtimeOwnerTerm,encodePrimitiveIO} from " + json.dumps(CODEC)
                  + "; const Ctr=(k,x)=>({$:'Ctr',k,x}); const t=runtimeOwnerTerm({Ctr},"
                    "{schema:1,data:{pair:[{bytes_base64:'AP8='},null]}});"
                    "if(t.k!=='PGOwnerContext')throw Error('wrong constructor');"
                    "process.stdout.write(JSON.stringify(encodePrimitiveIO(Ctr('PGWrite',"
                    "[Ctr('PGZero',[]),t.x[0].x[0].x[0],Ctr('PGDone',[])]))));")
        result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decode_writes(json.loads(result.stdout))[0].data, b"\x00\xff")
        script = ("import {runtimeOwnerTerm} from " + json.dumps(CODEC)
                  + "; const Ctr=(k,x)=>({$:'Ctr',k,x}); for(const v of [null,[],{},"
                    "{schema:1,bytes_base64:''},{schema:1,data:{bytes_base64:'AB=='}},"
                    "{schema:1,data:null,extra:1},{schema:true,data:null},"
                    "{schema:1,data:{pair:[null]}},{schema:1,data:{pair:[null,null],extra:1}},"
                    "{schema:1,data:{bytes_base64:Buffer.alloc(4198401).toString('base64')}}]) {"
                    "let rejected=false;try{runtimeOwnerTerm({Ctr},v)}catch{rejected=true}"
                    "if(!rejected)throw Error('malformed context accepted');}")
        result = subprocess.run(["/usr/bin/node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_rejects_invalid_flat_frames(self):
        for value in ({"schema": 1, "writes": [], "callback": "execute"},
                      {"schema": True, "writes": []},
                      {"schema": 1, "writes": [{"destination": True, "bytes_base64": ""}]},
                      {"schema": 1, "writes": [{"destination": 0, "bytes_base64": "AB=="}]},
                      {"schema": 1, "writes": [{"destination": 0,
                                               "bytes_base64": base64.b64encode(b"x" * 65537).decode()}]}):
            with self.subTest(value_type=type(value)):
                with self.assertRaises(ContractError):
                    decode_writes(value)
