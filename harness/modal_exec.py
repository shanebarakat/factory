"""Run one command on a Modal Sandbox against the current directory; return its exit code.

Invoked by ``modal_exec.sh``, which supplies the Modal credentials. The working tree (minus
``.git`` and every ``node_modules``) is uploaded to ``/work``, the command runs there with its
argv unchanged, and stdout/stderr stream back. Exit codes: the command's own, 124 when the
sandbox timeout fires, 143 on SIGTERM (the local ``timeout`` in mutate.sh/flake.sh), 70 when the
uploaded tree's digest differs on the remote side (nothing runs).

Provenance: the tree digest is sha256 over ``sha256sum``-format lines ("<hex>  ./<path>") of
every regular file, sorted by path bytes. It is computed locally before upload and remotely
after extraction (``find | sort | sha256sum``) and printed as ``modal-exec: tree sha256:...``,
so a receipt's log names exactly the tree that ran.

Environment (all optional):
  FACTORY_REMOTE_SETUP    shell run in /work before the command (e.g. ``pnpm install
                          --frozen-lockfile``); its node_modules are cached in a Modal Volume
                          keyed by the lockfiles, the setup text and the Node version
  FACTORY_REMOTE_NODE     Node major/version for the image; default ``.node-version``, else 22
  FACTORY_REMOTE_CPU      default 8; FACTORY_REMOTE_MEMORY_MB default 16384
  FACTORY_REMOTE_TIMEOUT  seconds, default 3600
  FACTORY_REMOTE_ENV      space-separated names of local variables to forward
  FACTORY_PACKAGES_TOKEN  private npm token, exposed remotely as NODE_AUTH_TOKEN and
                          GH_PACKAGES_TOKEN through a Modal secret, never in argv or logs
"""

from __future__ import annotations

import hashlib
import io
import os
import shlex
import signal
import sys
import tarfile
import threading
from pathlib import Path

import modal

APP = "factory-exec"
VOLUME = "factory-exec-cache"
REMOTE = "/work"
EXCLUDED = {".git", "node_modules", "target"}
LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb", "deno.lock", "Cargo.lock")


def tree(root: Path) -> list[Path]:
    paths = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in EXCLUDED)
        paths.extend(Path(directory, f) for f in files)
    return sorted(paths, key=lambda p: os.fsencode(p.relative_to(root)))


def digest(root: Path, paths: list[Path]) -> str:
    lines = sorted(
        (b"./" + os.fsencode(p.relative_to(root)),
         hashlib.sha256(p.read_bytes()).hexdigest().encode())
        for p in paths if p.is_file() and not p.is_symlink()
    )
    return hashlib.sha256(b"".join(h + b"  " + n + b"\n" for n, h in lines)).hexdigest()


def archive(root: Path, paths: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for p in paths:
            tar.add(p, arcname=str(p.relative_to(root)), recursive=False)
    return buffer.getvalue()


def remote_pythonpath(root: Path) -> str | None:
    """Entries under the local tree move to /work; any other entry has no remote twin."""
    entries = []
    for entry in filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep)):
        try:
            rel = Path(entry).resolve().relative_to(root)
        except ValueError:
            print(f"modal-exec: PYTHONPATH entry outside the tree dropped: {entry}", file=sys.stderr)
            continue
        entries.append(str(Path(REMOTE, rel)))
    return ":".join(entries) or None


def node_version(root: Path) -> str:
    pinned = root / ".node-version"
    if os.environ.get("FACTORY_REMOTE_NODE"):
        return os.environ["FACTORY_REMOTE_NODE"]
    return pinned.read_text().strip().lstrip("v") if pinned.is_file() else "22"


def run(sandbox: modal.Sandbox, script: str, stdin: bytes | None = None) -> int:
    process = sandbox.exec("bash", "-c", script, workdir=REMOTE)
    if stdin is not None:
        process.stdin.write(stdin)
        process.stdin.write_eof()
        process.stdin.drain()

    def pump(source, sink) -> None:
        for chunk in source:
            sink.write(chunk)
            sink.flush()

    threads = [threading.Thread(target=pump, args=(process.stdout, sys.stdout)),
               threading.Thread(target=pump, args=(process.stderr, sys.stderr))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return process.wait()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: modal_exec.sh <command> [args...]", file=sys.stderr)
        return 64
    root = Path.cwd().resolve()
    paths = tree(root)
    local_digest = digest(root, paths)
    payload = archive(root, paths)
    node = node_version(root)
    setup = os.environ.get("FACTORY_REMOTE_SETUP", "")
    key = hashlib.sha256(
        b"".join((root / f).read_bytes() for f in LOCKFILES if (root / f).is_file())
        + setup.encode() + node.encode()
    ).hexdigest()[:32]

    env = {name: os.environ[name] for name in os.environ.get("FACTORY_REMOTE_ENV", "").split()
           if name in os.environ}
    pythonpath = remote_pythonpath(root)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    env["CI"] = env.get("CI", "1")
    secrets = []
    if os.environ.get("FACTORY_PACKAGES_TOKEN"):
        token = os.environ["FACTORY_PACKAGES_TOKEN"]
        secrets.append(modal.Secret.from_dict({"NODE_AUTH_TOKEN": token, "GH_PACKAGES_TOKEN": token}))

    # FACTORY_REMOTE_IMAGE swaps the base for non-Node targets, e.g. rust:1.98-bookworm.
    # Cargo's registry and build output live on the cache volume, keyed like node_modules,
    # so a mutant rebuilds only what it touched.
    base = os.environ.get("FACTORY_REMOTE_IMAGE")
    rust = False
    if base:
        image = modal.Image.from_registry(base, add_python="3.12").apt_install("git", "ca-certificates")
        if base.startswith("rust:"):
            env.setdefault("CARGO_HOME", "/cache/cargo")
            env.setdefault("CARGO_TARGET_DIR", f"/cache/target/{key}")
            rust = True
    else:
        image = (modal.Image.from_registry(f"node:{node}-bookworm-slim", add_python="3.12")
                 .apt_install("git", "ca-certificates")
                 .run_commands("corepack enable"))
    app = modal.App.lookup(APP, create_if_missing=True)
    sandbox = modal.Sandbox.create(
        app=app, image=image, env=env, secrets=secrets,
        cpu=float(os.environ.get("FACTORY_REMOTE_CPU", "8")),
        memory=int(os.environ.get("FACTORY_REMOTE_MEMORY_MB", "16384")),
        timeout=int(os.environ.get("FACTORY_REMOTE_TIMEOUT", "3600")),
        volumes={"/cache": modal.Volume.from_name(VOLUME, create_if_missing=True)},
        workdir="/",
    )

    def stop(signum, _frame):
        sandbox.terminate()
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        sandbox.exec("mkdir", "-p", REMOTE).wait()
        verify = (
            f"tar -xzf - -C {REMOTE} && cd {REMOTE} && "
            f"[ \"$(find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum | sha256sum"
            f" | cut -d' ' -f1)\" = {local_digest} ]"
            # Install hooks and tests that shell out to git expect a repository.
            " && git init -q"
            # The shared target dir is keyed only by Cargo.lock, and the archive keeps local
            # mtimes, so an older tree could look up to date and run a stale binary (seen on
            # kinbase-oidc-20261020b). Fresh mtimes make cargo rebuild the workspace every time
            # while dependencies stay cached.
            + (" && find . -type f -exec touch {} +" if rust else "")
        )
        if run(sandbox, verify, stdin=payload) != 0:
            print(f"modal-exec: remote tree digest differs from local sha256:{local_digest}",
                  file=sys.stderr)
            return 70
        print(f"modal-exec: tree sha256:{local_digest} verified remote "
              f"(node {node}, sandbox {sandbox.object_id})", file=sys.stderr)
        if setup:
            cache = f"/cache/node_modules/{key}.tar.gz"
            restore = (
                f"if [ -f {cache} ]; then echo 'modal-exec: node_modules cache hit' >&2; "
                f"tar -xzf {cache}; else echo 'modal-exec: node_modules cache miss' >&2; "
                f"( {setup} ) >&2 && mkdir -p /cache/node_modules && "
                f"find . -name node_modules -type d -prune -print0 | "
                f"tar -czf {cache}.$$ --null -T - && mv {cache}.$$ {cache} && sync /cache; fi"
            )
            if (rc := run(sandbox, restore)) != 0:
                print(f"modal-exec: setup failed ({rc})", file=sys.stderr)
                return rc
        rc = run(sandbox, shlex.join(argv))
        return 124 if rc == -1 else rc
    finally:
        sandbox.terminate()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
