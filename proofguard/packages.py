"""Owner enrollment for frozen Bend packages and private runtime inputs.

Packages contain Bend source and optional trusted checking code. Application
computation stays in Bend; native checkers never receive private runtime bytes.
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets

from bendspec.backend import PluginCheck, Toolchain
from bendspec.model import Bundle, ContractError, Project, Source, canonical, contract_id, decode, fields, names, text
from bendspec.workspace import Authority, read_regular

from .admission import BendAdmission, ProgramAdmission
from .audit import Audit, durable_directory, publish
from .context import ExecutionContext, decode_owner_data, decode_snapshot
from .encoding import commitment, encode
from .gateway import Gateway, Session
from .http import parse_endpoints

LIBRARY = Path(__file__).parent / "stdlib"
LIBRARY_PATH = "proofguard/effects.bend"


def runtime_package() -> Bundle:
    """Supply the fixed execution library without private plugin declarations."""
    sources = tuple(Source(f"proofguard/{name}", read_regular(LIBRARY, name).decode())
                    for name in ("effects.bend", "program.bend"))
    return Bundle(Project("proofguard-runtime", "Execute accepted main through primitive IO",
                          tuple(source.path for source in sources), ("main",), ()), sources)


def get_core_identity() -> str:
    """Pin the host boundary, primitive ABI and owner launcher."""
    root = Path(__file__).parent
    paths = [*root.glob("*.py"), *LIBRARY.glob("*.bend")]
    return commitment("foundation-code-v1", *(str(path.relative_to(root.parent)).encode()
                      + hashlib.sha256(path.read_bytes()).digest() for path in sorted(paths)))


def decode_inputs(value: object) -> tuple[bytes, ...]:
    """Decode bounded private snapshots from the owner-only configuration."""
    if not isinstance(value, list) or len(value) > 16:
        raise ContractError("expected at most sixteen private input snapshots")
    result = []
    for item in value:
        result.append(decode_snapshot(item, 65536))
    return tuple(result)


def parse_session(value: object) -> Session:
    """Resolve the owner's fixed destination table and session deadline."""
    optional = {"endpoints"} & set(value) if type(value) is dict else set()
    row = fields(value, {"identifier", "expires", "destinations"} | optional, "owner session")
    identifier = text(row["identifier"], "session identifier")
    if type(row["expires"]) is not int or not 0 <= row["expires"] < 2**63:
        raise ContractError("expiry must be a positive i64 epoch")
    if not isinstance(row["destinations"], list) or not 1 <= len(row["destinations"]) <= 16:
        raise ContractError("owner must enroll 1..16 destinations")
    destinations = []
    for value in row["destinations"]:
        path = Path(text(value, "destination"))
        if not path.is_absolute() or path.is_symlink():
            raise ContractError("absolute non-symlink destination required")
        destinations.append(path.parent.resolve(strict=True) / path.name)
    if len(set(destinations)) != len(destinations):
        raise ContractError("write destinations must identify distinct resources")
    return Session(identifier, row["expires"], "", tuple(destinations), parse_endpoints(row.get("endpoints", [])))


@dataclass(frozen=True)
class Enrollment:
    snapshot: bytes

    @property
    def record(self) -> dict:
        return decode(self.snapshot)

    @property
    def identity(self) -> str:
        return commitment("owner-enrollment-v1", encode(self.record))

    @classmethod
    def create(cls, configuration: object, authority: Path) -> "Enrollment":
        """Freeze package sources, optional checking code and owner authority."""
        row = fields(configuration, {"schema", "plugins", "inputs_base64", "session"}, "enrollment")
        if type(row["schema"]) is not int or row["schema"] != 1:
            raise ContractError("invalid enrollment schema")
        plugins = row["plugins"]
        if type(plugins) is not list or not 1 <= len(plugins) <= 16:
            raise ContractError("enroll 1..16 independent plugin packages")
        captured = []
        bundle = runtime_package()
        for value in plugins:
            optional = {"checker", "owner_data"} & set(value) if type(value) is dict else set()
            selected = fields(value, {"contract", "entrypoint"} | optional, "enrolled plugin")
            package = Bundle.parse(selected["contract"])
            names([selected["entrypoint"]], "plugin guard entrypoint")
            if not package.project.proofs or package.project.implementations != ("main",):
                raise ContractError("plugin must declare main and mandatory Bend laws")
            if package.sources[:2] != bundle.sources:
                raise ContractError("plugin must use the pinned foundation interface")
            if "checker" in selected:
                PluginCheck(selected["checker"], 0)
            captured.append((package, selected))
        if len({canonical(selected) for _, selected in captured}) != len(captured):
            raise ContractError("duplicate plugin package")
        inputs = decode_inputs(row["inputs_base64"])
        context = ExecutionContext(inputs, tuple(decode_owner_data(selected["owner_data"]) for _, selected in captured
                                                 if "owner_data" in selected))
        session = parse_session(row["session"])
        authority = authority.resolve(strict=True)
        if any(path.is_relative_to(authority) for path in session.destinations):
            raise ContractError("write destinations cannot overlap owner authority")
        durable_directory(authority)
        Authority(authority).approve(bundle)
        for package, _ in captured:
            Authority(authority).approve(package)
        record = {
            "schema": 1, "commitment_nonce": secrets.token_hex(32),
            "contract_id": bundle.identity,
            "inputs_base64": row["inputs_base64"],
            "context_id": context.identity,
            "core_id": get_core_identity(), "toolchain": Toolchain.installed().validate(),
            "session": {"identifier": session.identifier, "expires": session.expires,
                        "destinations": [str(path) for path in session.destinations]},
        }
        if session.endpoints:
            record["session"]["endpoints"] = [endpoint.data() for endpoint in session.endpoints]
        record["plugins"] = [{"contract_id": package.identity, "entrypoint": selected["entrypoint"],
                              **{key: selected[key] for key in ("checker", "owner_data") if key in selected}}
                             for package, selected in captured]
        result = cls(canonical(record))
        if len(result.snapshot) > 8 * 1024 * 1024:
            raise ContractError("enrollment archive exceeds its read bound")
        record_id = Audit(authority).store(result.record)
        durable_directory(authority / "enrollments")
        publish(authority / "enrollments" / f"{result.identity}.json", canonical({"record_id": record_id}))
        return result

    @classmethod
    def load(cls, authority: Path, expected_id: str) -> "Enrollment":
        """Check the content-addressed owner record selected at worker startup."""
        contract_id(expected_id)
        index = fields(decode(read_regular(authority, f"enrollments/{expected_id}.json", 65536)),
                       {"record_id"}, "enrollment index")
        record_id = contract_id(index["record_id"])
        raw = read_regular(authority, f"{record_id}.json", 8 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != record_id:
            raise ContractError("enrollment archive digest mismatch")
        decoded = decode(raw)
        row = fields(decoded, {"schema", "commitment_nonce", "contract_id", "plugins",
                                  "inputs_base64", "context_id", "core_id", "toolchain", "session"},
                     "approved enrollment")
        if type(row["schema"]) is not int or row["schema"] != 1:
            raise ContractError("invalid enrollment schema")
        contract_id(row["commitment_nonce"])
        result = cls(canonical(row))
        if result.identity != expected_id:
            raise ContractError("enrollment does not match owner-selected identity")
        return result

    def build_gateway(self, authority: Path) -> Gateway:
        """Revalidate the installed boundary and reconstruct private execution state."""
        row = self.record
        if get_core_identity() != row["core_id"]:
            raise ContractError("foundation code changed since owner enrollment")
        bundle = Authority(authority).load(row["contract_id"])
        if bundle != runtime_package():
            raise ContractError("enrollment must use the fixed foundation runtime")
        toolchain = Toolchain.installed()
        if toolchain.validate() != row["toolchain"]:
            raise ContractError("toolchain changed since owner enrollment")
        parsed = parse_session(row["session"])
        if [str(path) for path in parsed.destinations] != row["session"]["destinations"]:
            raise ContractError("destination parents changed since owner enrollment")
        inputs = decode_inputs(row["inputs_base64"])
        owners = [decode_owner_data(plugin["owner_data"]) if "owner_data" in plugin else None for plugin in row["plugins"]]
        context = ExecutionContext(inputs, tuple(owner for owner, plugin in zip(owners, row["plugins"])
                                                 if "owner_data" in plugin))
        session = Session(parsed.identifier, parsed.expires, row["context_id"], parsed.destinations, parsed.endpoints)
        task_root = authority / "sessions" / commitment("session", session.identifier.encode())
        state, audit = task_root / "state", task_root / "audit"
        durable_directory(state)
        durable_directory(audit)
        gates = tuple(BendAdmission(Authority(authority).load(plugin["contract_id"]), plugin["entrypoint"],
                                    toolchain, owner_argument="owner_data" in plugin, owner=owner,
                                    checker=plugin.get("checker"), input_count=len(inputs))
                      for plugin, owner in zip(row["plugins"], owners))
        admission = ProgramAdmission(BendAdmission(bundle, "main", toolchain, owner_argument=False), gates)
        return Gateway(admission, session, context, state, Audit(audit), authority=authority)

    def get_task(self, authority: Path) -> dict:
        """Expose the fixed foundation interface; package sources stay owner-side."""
        Authority(authority).load(self.record["contract_id"])
        interface = [Source(f"proofguard/{name}", read_regular(LIBRARY, name).decode()).data()
                     for name in ("effects.bend", "program.bend")]
        return {"schema": 1, "enrollment_id": self.identity,
                "interface": interface, "entrypoint": "main",
                "instructions": "Write Bend2 source implementing main(inputs) with your own helpers and datatypes as needed. "
                                "Use the supplied interface and follow its function signatures. "
                                "Do not redefine supplied declarations or provide verification proofs. "
                                "Nullary definitions require parentheses. "
                                "main uses inputs once; pass them to a helper declared with +inputs to read several. "
                                "pg_flatten(bytes) gives a PGFlat with no PGChunks, for recursing byte by byte; pg_unflatten turns it back into PGBytes. "
                                "The program computes application behavior; the owner dispatches only fixed primitive IO. "
                                "The gateway checks the program before executing its controlled IO. "
                                "PGHttp{request, response => ...} calls an owner-enrolled endpoint while the program "
                                "runs and gives the response to the program. You read only your submission's status, "
                                "never a response. Effects run in program order and a sent call cannot be undone: "
                                "PGAbort cancels only the writes since the last call."}
