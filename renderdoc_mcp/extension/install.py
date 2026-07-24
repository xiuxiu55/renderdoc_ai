"""Install this extension into RenderDoc's user extensions folder.

Copies the extension package to the per-user qrenderdoc extensions directory so
RenderDoc can discover it under Tools -> Manage Extensions.

Usage:
    python install.py            # copy into the default location
    python install.py --link     # (best effort) create a symlink/junction instead
"""

import argparse
import os
import shutil
import sys

EXT_NAME = "renderdoc_mcp"
FILES = [
    "__init__.py", "extension.json",
    # Chat panel -> CodeBuddy (direct, via ctypes HTTP + ACP):
    "http_ctypes.py", "codebuddy_client.py", "acp_client.py",
    # Live-frame context used to augment prompts:
    "live_frame.py",
]

# Shared hot-question playbook (also used by the MCP server).
PLAYBOOK_DIRNAME = "playbook"
PLAYBOOK_FILES = [
    "__init__.py", "registry.py", "runtime.py", "analyzers.py", "questions.json",
]

# Shared Intent→Plan→Execute orchestrator (Panel + MCP).
ORCHESTRATOR_DIRNAME = "orchestrator"
ORCHESTRATOR_FILES = [
    "__init__.py", "registry.py", "router.py", "planner.py", "executor.py", "pipeline.py",
]


def default_extensions_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "qrenderdoc", "extensions")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "qrenderdoc", "extensions")


def _copy_playbook(src_playbook, dest_playbook):
    os.makedirs(dest_playbook, exist_ok=True)
    for name in PLAYBOOK_FILES:
        src = os.path.join(src_playbook, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest_playbook, name))


def _copy_orchestrator(src_orch, dest_orch):
    os.makedirs(dest_orch, exist_ok=True)
    for name in ORCHESTRATOR_FILES:
        src = os.path.join(src_orch, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest_orch, name))


def main():
    parser = argparse.ArgumentParser(description="Install the RenderDoc MCP extension")
    parser.add_argument("--dest", default=None, help="Override the extensions directory")
    parser.add_argument("--link", action="store_true", help="Symlink/junction instead of copy")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    pkg_root = os.path.dirname(here)
    src_playbook = os.path.join(pkg_root, PLAYBOOK_DIRNAME)
    src_orch = os.path.join(pkg_root, ORCHESTRATOR_DIRNAME)
    ext_root = args.dest or default_extensions_dir()
    target = os.path.join(ext_root, EXT_NAME)

    os.makedirs(ext_root, exist_ok=True)

    if os.path.exists(target) or os.path.islink(target):
        if os.path.islink(target):
            os.unlink(target)
        else:
            shutil.rmtree(target)

    if args.link:
        try:
            os.symlink(here, target, target_is_directory=True)
            # Playbook / orchestrator live next to extension/ in the package.
            for dirname, src_dir, copier in (
                (PLAYBOOK_DIRNAME, src_playbook, _copy_playbook),
                (ORCHESTRATOR_DIRNAME, src_orch, _copy_orchestrator),
            ):
                link_path = os.path.join(target, dirname)
                if os.path.isdir(src_dir) and not os.path.exists(link_path):
                    try:
                        os.symlink(src_dir, link_path, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        copier(src_dir, link_path)
            print("Linked %s -> %s" % (target, here))
            return
        except (OSError, NotImplementedError) as exc:
            print("Symlink failed (%s), falling back to copy." % exc)

    os.makedirs(target, exist_ok=True)
    for name in FILES:
        src = os.path.join(here, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target, name))
    if os.path.isdir(src_playbook):
        _copy_playbook(src_playbook, os.path.join(target, PLAYBOOK_DIRNAME))
    else:
        print("WARNING: playbook not found at %s" % src_playbook)
    if os.path.isdir(src_orch):
        _copy_orchestrator(src_orch, os.path.join(target, ORCHESTRATOR_DIRNAME))
    else:
        print("WARNING: orchestrator not found at %s" % src_orch)
    print("Installed extension to: %s" % target)
    print("Now open RenderDoc -> Tools -> Manage Extensions, and Load 'CodeBuddy MCP (live frame)'.")


if __name__ == "__main__":
    main()
