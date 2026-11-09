"""Capture owner-side Bend checks for the fixed foundation program interface."""

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from bendspec.model import Bundle, ContractError, Project, Source, canonical, decode, fields, names, paths, text
from bendspec.workspace import MAX_SOURCE_BYTES, read_regular

from .packages import Enrollment, LIBRARY, LIBRARY_PATH

ENTRYPOINT_PATH = "proofguard/program.bend"
_OMITTED = object()
DECLARATION = re.compile(r"(def|law|type)\s+([A-Za-z_][A-Za-z0-9_.]*)")
CONSTRUCTOR = re.compile(r"\s+([A-Za-z_][A-Za-z0-9_]*)\{")
SHARED_NAMES = ("pg_", "PG")


def get_declared_names(sources: tuple[Source, ...]) -> set[str]:
    """List plugin declarations; names a native checker uses keep the pg_/PG prefix."""
    declared = set()
    for source in sources:
        in_type = False
        for line in source.content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if found := DECLARATION.match(line):
                declared.add(found[2])
                in_type = found[1] == "type"
            elif not line[0].isspace():
                in_type = False
            elif in_type and (found := CONSTRUCTOR.match(line)):
                declared.add(found[1])
    return {name for name in declared if not name.startswith(SHARED_NAMES)}


def isolate_names(sources: tuple[Source, ...], laws: list, entrypoint: object) -> tuple:
    """Rename plugin declarations so agent helpers cannot collide with hidden names.

    Only owner source changes; the agent's program is checked and executed as
    written. Missing a declaration restores the duplicate-name rejection.
    """
    declared = get_declared_names(sources)
    if not declared:
        return sources, laws, entrypoint
    prefix = "p" + hashlib.sha256(canonical([source.data() for source in sources])).hexdigest()[:12] + "_"
    pattern = re.compile(r"(?<![A-Za-z0-9_.])(" + "|".join(map(re.escape, sorted(declared, key=len, reverse=True)))
                         + r")(?![A-Za-z0-9_])")
    rename = lambda value: pattern.sub(lambda match: prefix + match[1], value)
    return (tuple(Source(source.path, rename(source.content)) for source in sources),
            [rename(law) for law in laws] if type(laws) is list else laws,
            rename(entrypoint) if type(entrypoint) is str else entrypoint)


@dataclass(frozen=True)
class Plugin:
    contract: Bundle
    entrypoint: str = "main"
    checker: str | None = None
    owner_data: object = _OMITTED

    @classmethod
    def load(cls, root: Path, *, bindings: tuple[Source, ...] = ()) -> "Plugin":
        """Freeze private package sources; the foundation supplies the agent ABI."""
        manifest = decode(read_regular(root, "plugin.json"))
        optional = {"checker"} if type(manifest) is dict and "checker" in manifest else set()
        row = fields(manifest, {"schema", "name", "intent", "sources", "bindings", "laws", "entrypoint"} | optional,
                     "plugin")
        if type(row["schema"]) is not int or row["schema"] != 1:
            raise ContractError("invalid plugin schema")
        selected = paths(row["sources"])
        expected = () if row["bindings"] == [] else paths(row["bindings"])
        if ({LIBRARY_PATH, ENTRYPOINT_PATH} & set(selected)
                or not set(expected) <= set(selected)):
            raise ContractError("invalid private or reserved plugin source paths")
        if type(bindings) is not tuple or any(type(source) is not Source for source in bindings):
            raise ContractError("owner source bindings must be a tuple of Sources")
        supplied = tuple(Source.parse(source.data()) for source in bindings)
        if tuple(source.path for source in supplied) != expected:
            raise ContractError("owner bindings must match the manifest's ordered private paths")
        remaining = MAX_SOURCE_BYTES - sum(len(source.content.encode()) for source in supplied)
        if remaining < 0:
            raise ContractError("plugin source size limit exceeded")
        captured = {source.path: source for source in supplied}
        for path in selected:
            if path not in captured:
                raw = read_regular(root, path, remaining)
                remaining -= len(raw)
                try:
                    captured[path] = Source.parse({"path": path, "content": raw.decode("utf-8")})
                except UnicodeError as error:
                    raise ContractError("non-UTF-8 plugin source") from error
        checker = None
        if optional:
            path = row["checker"]
            if not isinstance(path, str) or not path.endswith(".mjs"):
                raise ContractError("checker must select an owner-side .mjs file")
            try:
                checker = read_regular(root, path, remaining).decode("utf-8")
            except UnicodeError as error:
                raise ContractError("non-UTF-8 checker source") from error
            if not checker or "\x00" in checker:
                raise ContractError("checker source must be nonempty text without NUL")
        fixed = [Source(LIBRARY_PATH, read_regular(LIBRARY, "effects.bend").decode()),
                 Source(ENTRYPOINT_PATH, read_regular(LIBRARY, "program.bend").decode())]
        owned, laws, selected_entrypoint = isolate_names(tuple(captured[path] for path in selected),
                                                         row["laws"], row["entrypoint"])
        sources = (*fixed, *owned)
        project = Project.parse({"schema": 1, "name": row["name"], "intent": row["intent"],
                                 "sources": [source.path for source in sources],
                                 "implementations": ["main"], "proofs": laws})
        entrypoint = names([selected_entrypoint], "entrypoint")[0]
        return cls(Bundle(project, sources), entrypoint, checker)

    def with_owner_data(self, owner_data: object) -> "Plugin":
        """Give this plugin's guard private records and a two-argument signature."""
        return replace(self, owner_data=owner_data)

    def get_selection(self) -> dict:
        """Describe this plugin's enrollment entry, preserving omitted owner data."""
        selected = {"contract": self.contract.data(), "entrypoint": self.entrypoint}
        if self.checker is not None:
            selected["checker"] = self.checker
        if self.owner_data is not _OMITTED:
            selected["owner_data"] = self.owner_data
        return selected

    def enrollment_config(self, *, inputs_base64: list[str], session: dict) -> dict:
        """Build the common owner enrollment record."""
        return {"schema": 1, "plugins": [self.get_selection()],
                "inputs_base64": inputs_base64, "session": session}

    @staticmethod
    def enroll_all(plugins: tuple["Plugin", ...], authority: Path, *, inputs_base64: list[str],
                   session: dict) -> Enrollment:
        """Freeze independent law packages around one foundation program/runtime."""
        if type(plugins) is not tuple or not plugins or any(type(plugin) is not Plugin for plugin in plugins):
            raise ContractError("an immutable nonempty plugin selection is required")
        return Enrollment.create({"schema": 1, "plugins": [plugin.get_selection() for plugin in plugins],
                                  "inputs_base64": inputs_base64, "session": session}, authority)

    def enroll(self, authority: Path, *, inputs_base64: list[str], session: dict) -> Enrollment:
        """Activate this captured plugin with owner-held data and destinations."""
        return Enrollment.create(self.enrollment_config(inputs_base64=inputs_base64, session=session), authority)


def enroll_configuration(path: Path, authority: Path) -> Enrollment:
    """Enroll the plugins an owner lists in a JSON file; plugin paths are relative to it."""
    configuration = decode(read_regular(path.parent, path.name, 16 * 1024 * 1024))
    row = fields(configuration, {"schema", "plugins", "inputs_base64", "session"}, "plugin configuration")
    if type(row["schema"]) is not int or row["schema"] != 1:
        raise ContractError("unsupported plugin configuration schema")
    if type(row["plugins"]) is not list or not 1 <= len(row["plugins"]) <= 16:
        raise ContractError("select 1..16 plugin paths")
    plugins = []
    for selection in row["plugins"]:
        extra = {"bindings", "owner_data"} & set(selection) if type(selection) is dict else set()
        item = fields(selection, {"path"} | extra, "plugin selection")
        package = path.parent / text(item["path"], "plugin path")
        raw_bindings = item.get("bindings", [])
        if type(raw_bindings) is not list:
            raise ContractError("owner source bindings must be a list")
        plugin = Plugin.load(package, bindings=tuple(Source.parse(source) for source in raw_bindings))
        plugins.append(plugin.with_owner_data(item["owner_data"]) if "owner_data" in item else plugin)
    return Plugin.enroll_all(tuple(plugins), authority, inputs_base64=row["inputs_base64"],
                             session=row["session"])
