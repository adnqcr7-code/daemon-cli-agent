"""Bubblewrap sandbox for shell execution (v0.5.0 §5).

Opt-in via config ``sandbox.shell`` (default OFF - it stays opt-in
until it has survived real use). When enabled on Linux with a working
bubblewrap, shell commands run inside bwrap:

- the root is a FRESH tmpfs: nothing from the host filesystem exists
  inside unless it is explicitly bound. The complete bound surface
  is enumerated in ``wrap()``: the OS runtime (``/usr`` plus the
  merged-usr symlinks), a minimal ``/etc`` (resolver / passwd /
  alternatives / CA bundle - see the comment there), ``/proc``,
  ``/dev``, a private tmpfs ``/tmp``, and the workspace bound
  read-write at its real path. ``$HOME``, ``/var``, sibling trees
  and every other host path are simply ABSENT - unreadable, not
  merely unwritable (round 1 bound ``/`` read-only and called that
  confinement; review round 2, V1, falsified the read half);
- the assembled root is remounted read-only, so outside the
  workspace and ``/tmp`` nothing can be written either;
- a network namespace (``--unshare-net``) when the host allows it:
  no external network at all. Bubblewrap brings the namespace's own
  loopback up, so servers started INSIDE the sandbox (the test-suite
  pattern) still work, while anything in the parent's namespace -
  including its loopback services - is unreachable;
- the environment stays secret-stripped (run_shell's env handling is
  unchanged) and ``HOME`` points at the workspace inside the box.

Two known limitations are inherent to the mechanism and are stated
plainly rather than engineered around (review round 3, R2-3/R2-4):

- Confinement is by PATH, not by inode — so a hardlink planted
  inside the workspace names the same inode as a file outside it.
  The box cannot tell a hardlink from the file it links to, so the
  setup refuses the run outright instead: ``plan_for`` (and the
  §2.6 backends) scan the workspace for regular files whose inode
  has a link outside it and raise ``SandboxError`` before anything
  starts (review round 1, R1-6). Planting one still requires a
  host-side act first (e.g. extracting a crafted tarball with a
  hardlink entry into the workspace) — the sandbox cannot create
  the link — but a planted one can no longer be read inside.
- The fresh ``/proc`` hides other processes (pid namespace) but not
  host-global kernel files: ``/proc/cmdline``, ``/proc/cpuinfo``,
  ``/proc/iomem`` and friends still describe the HOST - its boot
  parameters, CPU and memory map. bwrap cannot fabricate their
  contents.

Availability is PROBED, never assumed: this module runs bwrap for
real (once per process, cached). If the platform is not Linux,
bwrap is missing, or the probe fails, the sandbox reports itself
unavailable - commands then run unsandboxed with a one-time loud
warning, and doctor reports the same. If only the network namespace
fails (some container kernels reject it), the filesystem sandbox
still runs and the status says plainly that network isolation is
NOT in effect. macOS/Windows are detected and reported unavailable;
nothing here ever fakes availability.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxError
from .log import audit, get_logger

_logger = get_logger("sandbox")

_PROBE_TIMEOUT_S = 10
_probe_result: tuple[bool, bool] | None = None  # (filesystem, network)
_warned: set[str] = set()


def _bwrap_path() -> str | None:
    return shutil.which("bwrap")


def _probe() -> tuple[bool, bool]:
    """Actually run bwrap, once per process: (fs_ok, net_ok).

    The probe exercises the REAL wrap (review round 2, V7): it builds
    the actual :meth:`SandboxPlan.wrap` argv for a scratch workspace
    and runs ``/bin/true`` through it, once per level. A reduced-flag
    probe could report a working sandbox on a host where every real
    wrapped command then fails (e.g. a host that allows mount
    namespaces but forbids pid namespaces, which only the full wrap
    requests).
    """
    global _probe_result
    if _probe_result is not None:
        return _probe_result
    bwrap = _bwrap_path()
    if bwrap is None or not sys.platform.startswith("linux"):
        _probe_result = (False, False)
        return _probe_result
    with tempfile.TemporaryDirectory(
            prefix="daemon-sandbox-probe-") as scratch:
        root = Path(scratch)
        fs_ok = _probe_once(
            SandboxPlan(workspace=root, network_isolated=False)
            .wrap(["/bin/true"], cwd=root))
        net_ok = fs_ok and _probe_once(
            SandboxPlan(workspace=root, network_isolated=True)
            .wrap(["/bin/true"], cwd=root))
    _probe_result = (fs_ok, net_ok)
    return _probe_result


def _probe_once(argv: list[str]) -> bool:
    try:
        completed = subprocess.run(
            argv, capture_output=True,
            timeout=_PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@dataclass
class SandboxStatus:
    enabled: bool
    # off | unavailable | filesystem | full
    level: str
    reason: str = ""

    @property
    def active(self) -> bool:
        return self.level in ("filesystem", "full")

    @property
    def network_isolated(self) -> bool:
        return self.level == "full"


def sandbox_status(config=None) -> SandboxStatus:
    """The sandbox's real status for a config (probing if needed)."""
    enabled = bool(config.get("sandbox.shell", False)) \
        if config is not None else False
    if not enabled:
        return SandboxStatus(enabled=False, level="off",
                             reason="sandbox.shell is not enabled")
    if not sys.platform.startswith("linux"):
        return SandboxStatus(
            enabled=True, level="unavailable",
            reason=f"bubblewrap sandboxing is Linux-only; this is "
                   f"{sys.platform}")
    if _bwrap_path() is None:
        return SandboxStatus(enabled=True, level="unavailable",
                             reason="bwrap (bubblewrap) not on PATH")
    fs_ok, net_ok = _probe()
    if not fs_ok:
        return SandboxStatus(
            enabled=True, level="unavailable",
            reason="bwrap is installed but cannot create a sandbox "
                   "on this host (probe failed)")
    if not net_ok:
        return SandboxStatus(
            enabled=True, level="filesystem",
            reason="network namespace unavailable on this host; "
                   "filesystem isolation only - commands CAN still "
                   "reach the network")
    return SandboxStatus(enabled=True, level="full",
                         reason="bubblewrap filesystem + network "
                                "namespace isolation")


# The COMPLETE host surface bound into the sandbox, enumerated.
# Anything not named here does not exist inside the box. If a future
# need genuinely requires another bind, it must be added to these
# lists explicitly - never a blanket bind of / or of a parent tree.
_RUNTIME_DIRS = ("usr",)
# Merged-usr layout: /bin, /sbin, /lib, /lib64 (and the 32-bit
# variants) are symlinks into /usr on modern distros; recreate the
# symlink where the host has one, bind the real directory where it
# does not.
_RUNTIME_LINKS = ("bin", "sbin", "lib", "lib64", "lib32", "libx32")
# Minimal /etc. Justification, file by file - the sandbox has no DNS
# of its own at the full level (--unshare-net), and at the filesystem
# level name resolution goes through these host files:
# - resolv.conf / hosts / nsswitch.conf: glibc name resolution; keeps
#   getaddrinfo from hard-erroring where the network IS reachable.
# - passwd / group: uid/gid -> name lookups (whoami, ls -l, tar).
# - alternatives (dir): /usr/bin/python3 and friends are symlinks
#   into /etc/alternatives on Debian-likes; without it the
#   interpreter itself fails to resolve.
# - ssl (dir): the system CA bundle; TLS verification at the
#   filesystem level (network reachable, as the status discloses)
#   needs the trust store.
_ETC_FILES = ("resolv.conf", "hosts", "nsswitch.conf", "passwd",
              "group")
_ETC_DIRS = ("alternatives", "ssl")


@dataclass
class SandboxPlan:
    workspace: Path
    network_isolated: bool

    def wrap(self, argv: list[str], cwd: Path) -> list[str]:
        """Prefix *argv* with the bwrap invocation for this plan."""
        bwrap = _bwrap_path()
        assert bwrap is not None  # plans are built only when probed ok
        workspace = str(self.workspace)
        parts = [bwrap, "--die-with-parent", "--unshare-pid",
                 "--unshare-uts", "--unshare-ipc"]
        if self.network_isolated:
            parts.append("--unshare-net")
        # Fresh tmpfs root (review round 2, V1): the host filesystem
        # is NOT bound in - there is deliberately no "--ro-bind / /".
        # Only the enumerated runtime surface below exists inside,
        # so no outside PATH can be read or written. (The claim is
        # scoped to paths: a hardlink planted in the workspace is
        # the same inode under another name - see the module
        # docstring's known limitations, R2-3.) $HOME and /tmp need
        # no hiding tmpfs of their own:
        # they are absent unless bound (only /tmp is, as a private
        # tmpfs; $HOME never is - HOME points at the workspace).
        parts += ["--tmpfs", "/"]
        for name in _RUNTIME_DIRS:
            host_path = Path("/") / name
            if host_path.is_dir():
                parts += ["--ro-bind", str(host_path), f"/{name}"]
        for name in _RUNTIME_LINKS:
            host_path = Path("/") / name
            if host_path.is_symlink():
                parts += ["--symlink", os.readlink(host_path),
                          f"/{name}"]
            elif host_path.is_dir():
                parts += ["--ro-bind", str(host_path), f"/{name}"]
        parts += ["--dir", "/etc"]
        for name in _ETC_FILES:
            host_path = Path("/etc") / name
            if host_path.exists():
                parts += ["--ro-bind", str(host_path), f"/etc/{name}"]
        for name in _ETC_DIRS:
            host_path = Path("/etc") / name
            if host_path.is_dir():
                parts += ["--ro-bind", str(host_path), f"/etc/{name}"]
        parts += ["--proc", "/proc", "--dev", "/dev",
                  "--tmpfs", "/tmp",
                  "--bind", workspace, workspace,
                  "--setenv", "HOME", workspace,
                  # The assembled root itself is read-only; the
                  # remount is non-recursive, so the workspace bind
                  # and the /tmp tmpfs keep their own (writable)
                  # flags. Writing anywhere else fails.
                  "--remount-ro", "/",
                  "--chdir", str(cwd), "--"]
        return [*parts, *argv]


def find_outside_hardlinks(workspace: Path) -> list[Path]:
    """Regular files in *workspace* with a hardlink outside it.

    A file with ``st_nlink > 1`` is safe only when every one of its
    links is inside the scanned tree, so the scan groups regular
    files by (device, inode) and flags any file whose link count
    exceeds the number of in-tree entries sharing its inode.
    Entries that cannot be statted are skipped — not counted and
    not flagged: the scan can only judge files it can actually
    stat, so an unstattable entry (gone mid-scan, or unreadable)
    is simply outside what the scan can prove. Symlinks are never
    followed (path confinement already blocks symlink escapes),
    and directories are never hardlinks on Linux, so only regular
    files are examined. Returns the offending workspace-relative
    paths, sorted.
    """
    root = Path(workspace).resolve()
    by_inode: dict[tuple[int, int], list[Path]] = {}
    nlinks: dict[tuple[int, int], int] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            key = (st.st_dev, st.st_ino)
            by_inode.setdefault(key, []).append(path)
            nlinks[key] = st.st_nlink
    offenders: list[Path] = []
    for key, paths in by_inode.items():
        if len(paths) < nlinks[key]:
            offenders.extend(paths)
    return sorted(path.relative_to(root) for path in offenders)


def _hardlink_refusal(offenders: list[Path]) -> str:
    shown = ", ".join(str(path) for path in offenders[:5])
    more = (f" (+{len(offenders) - 5} more)"
            if len(offenders) > 5 else "")
    return (
        f"refusing to run in the sandbox: {len(offenders)} file(s) in "
        f"the workspace are hardlinks to files outside it ({shown}"
        f"{more}). A hardlink names the same inode as the outside "
        f"file, so the sandbox could read (and write) it. Remove or "
        f"replace the link(s), or run outside the sandbox.")


def plan_for(config, workspace: Path) -> SandboxPlan | None:
    """A wrap plan when the sandbox is enabled AND actually works.

    Returns None (run unsandboxed) when the sandbox is off, or when
    it is on but unavailable - in that case a loud warning is emitted
    once per process and an audit record is written, so the gap is
    never silent. A filesystem-only level still yields a plan, with
    its own one-time warning that network isolation is not in effect.
    """
    if config is None:
        return None
    status = sandbox_status(config)
    if status.level == "off":
        return None
    if status.level == "unavailable":
        _warn_once("unavailable",
                   f"shell sandbox requested (sandbox.shell=true) but "
                   f"UNAVAILABLE: {status.reason}. Commands will run "
                   f"UNSANDBOXED.")
        return None
    if status.level == "filesystem":
        _warn_once("filesystem-only",
                   f"shell sandbox is filesystem-only on this host: "
                   f"{status.reason}.")
    # A hardlink to outside content is a read AND write escape (the
    # same inode), so the run is refused outright — returning None
    # here would run the command UNSANDBOXED, which is the opposite
    # of fail-closed (review round 1, R1-6).
    offenders = find_outside_hardlinks(workspace)
    if offenders:
        raise SandboxError(_hardlink_refusal(offenders))
    return SandboxPlan(workspace=Path(workspace).resolve(),
                       network_isolated=status.network_isolated)


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning("%s", message)
    audit("sandbox-unavailable", detail=message)
