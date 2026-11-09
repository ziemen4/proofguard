"""Provision pinned Bend sources and build its kernel with an existing Lean 4.34.

No global installation, project hooks, package manager, or candidate source runs.
Linux x86-64 is the currently qualified platform. Lean's compiler and shared
runtime are trusted tools; their identities are recorded with the kernel build.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bendspec.model import canonical
from bendspec.workspace import atomic_write
from bendspec.backend import sha


def run(command: list[str], *, cwd: Path) -> str:
    """Run an owner-selected build tool with a clean environment and deadline."""
    result = subprocess.run(command, cwd=cwd, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                            "TMPDIR": str(ROOT / ".cache")},
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"toolchain build failed: {command[0]}\n{result.stdout}{result.stderr}")
    return result.stdout + result.stderr


def prepare_sources(source: Path, revision: str, files: dict[str, str], offline: bool = False):
    """Reuse hash-pinned cached sources; download missing files unless offline."""
    source.mkdir(parents=True, exist_ok=True)
    for name, expected in files.items():
        path = source / name
        if path.is_file() and not path.is_symlink() and sha(path) == expected:
            continue
        if offline:
            raise RuntimeError(f"pinned source unavailable in offline cache: {name}")
        url = f"https://raw.githubusercontent.com/bendlang/bend/{revision}/bend2/{name}"
        with urllib.request.urlopen(url, timeout=30) as incoming:
            data = incoming.read(4 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"pinned source digest mismatch: {name}")
        atomic_write(path, data)


def main():
    """Provision a verified source cache and build the local BendTT executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lean-home", required=True, type=Path,
                        help="Lean 4.34.0 distribution root containing bin/ and lib/")
    parser.add_argument("--node", type=Path, default=Path("/usr/bin/node"))
    parser.add_argument("--clang", type=Path, default=Path("/usr/bin/clang"))
    parser.add_argument("--offline", action="store_true",
                        help="require all pinned sources in the local cache; never download")
    args = parser.parse_args()
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    lean_home = args.lean_home.resolve()
    node = args.node.resolve()
    clang = args.clang.resolve()
    lean = lean_home / "bin/lean"
    for name, path in (("--node", node), ("--clang", clang), ("--lean-home", lean)):
        if not path.is_file():
            raise SystemExit(f"{name}: {path} not found. Install the pinned version there "
                             "or pass its path; see bend-spec/README.md.")
    node_version = run([str(node), "--version"], cwd=ROOT).strip()
    lean_version = run([str(lean), "--version"], cwd=ROOT).strip()
    if node_version != "v" + lock["node"] or f"version {lock['lean']}," not in lean_version:
        raise RuntimeError(f"version mismatch: {node_version}; {lean_version}")
    if shutil.disk_usage(ROOT).free < 1100 * 1024**2:
        raise RuntimeError("toolchain build requires at least 1100 MiB free (1 GiB reserve)")
    source = ROOT / ".cache" / ("bend-" + lock["revision"][:7])
    prepare_sources(source, lock["revision"], lock["files"], args.offline)
    work = ROOT / ".cache"
    c_source = work / "bendtt.c"
    binary = work / "bendtt.next"
    print("Checking the upstream kernel and compiling its CLI...", flush=True)
    lean_output = run([str(lean), "-c", str(c_source), str(source / "bendtt.lean")], cwd=ROOT)
    library = lean_home / "lib/lean"
    command = [str(clang), "-O2", "-DNDEBUG", "-I", str(lean_home / "include"),
               str(c_source), "-L", str(library), "-Wl,-rpath," + str(library),
               "-lleanshared", "-o", str(binary)]
    compile_output = run(command, cwd=ROOT)
    # Bind the shared Lean runtime as well as the executable. System libc/loader
    # remain part of the host trust boundary, like Node's system dependencies.
    shared = {str(path.resolve()): sha(path) for path in library.glob("*.so")}
    binary.replace(work / "bendtt")
    configuration = {"source": str(source.relative_to(ROOT)), "kernel": ".cache/bendtt",
                     "kernel_sha256": sha(work / "bendtt"), "node": str(node),
                     "libraries": shared}
    atomic_write(work / "runtime.json", canonical(configuration) + b"\n")
    report = {"schema": 1, "bend_revision": lock["revision"], "sources": lock["files"],
              "lean_version": lean_version, "lean_sha256": sha(lean),
              "node_version": node_version, "node_sha256": sha(node),
              "clang_sha256": sha(clang), "kernel_sha256": configuration["kernel_sha256"],
              "shared_runtime": shared, "c_sha256": sha(c_source),
              "lean_output": lean_output, "compile_output": compile_output}
    atomic_write(ROOT / "artifacts/toolchain-build.json", canonical(report) + b"\n")
    print("Pinned BendTT kernel ready.")


if __name__ == "__main__":
    main()
