"""Caobunga — local registry and CLI for Flaitron orchestration."""

from pathlib import Path

__version__ = "0.1.0"


def install_provenance() -> tuple[str, Path]:
    """Where this running package resolves from.

    ``("editable", path)`` when the package directory sits under a git
    checkout at its expected ``backend/caobunga`` layout (an editable install
    imports straight from the repo), else ``("snapshot", path)`` — a copy in
    an isolated venv with no matching ``.git`` ancestor, as `uv tool install`
    (without `--editable`) produces. The layout check (CBN-173) guards
    against a ``.git``-rooted ``$HOME`` (dotfiles-as-repo) making an
    unrelated snapshot install look editable. This is the only pin-free
    signal available at runtime for CBN-169: a stale snapshot silently
    shadows local fixes until an operator asks why they aren't taking
    effect.
    """
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (pkg_dir, *pkg_dir.parents):
        if (candidate / ".git").exists() and candidate / "backend" / "caobunga" == pkg_dir:
            return "editable", pkg_dir
    return "snapshot", pkg_dir
