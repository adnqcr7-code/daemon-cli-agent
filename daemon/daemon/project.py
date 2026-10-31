"""Persistent project index: structure, languages, dependencies,
entrypoints, build/test commands - incrementally maintained.

The index caches per-file (mtime, size) signatures under
``.daemon/index.json`` so re-scans only stat unchanged files and re-read the
ones that changed. The summary is what the agent's system prompt sees; the
full JSON backs the project_map tool.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import Config, project_dir
from .log import get_logger
from .tools.fs import IGNORE_DIRS

_logger = get_logger("project")

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
    ".sql": "sql", ".sh": "shell", ".ps1": "powershell", ".html": "html",
    ".css": "css", ".scss": "css", ".vue": "javascript", ".svelte": "javascript",
    ".md": "markdown", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
}

_IGNORE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock",
}


class ProjectIndex:
    def __init__(self, workspace: Path, *, config: Config | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config
        self.cache_path = project_dir(self.workspace) / "index.json"
        self.data: dict[str, Any] = {"version": 1, "built": 0, "files": {},
                                     "languages": {}, "manifests": {},
                                     "entrypoints": [], "build": {},
                                     "git": {}}

    # ------------------------------------------------------------------ scan

    def build(self, refresh: bool = False) -> dict[str, Any]:
        """Scan the workspace (incrementally). Returns the index data."""
        if not refresh and self.load_cache():
            stale = time.time() - self.data.get("built", 0) > 600
            if not stale:
                return self.data
        extra_ignore = set()
        if self.config is not None:
            extra_ignore = set(self.config.get("index.extra_ignore") or [])
        max_files = int(self.config.get("index.max_files") or 20000) \
            if self.config else 20000

        files: dict[str, list] = {}
        languages: dict[str, int] = {}
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.workspace):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in IGNORE_DIRS and d not in extra_ignore
                and not d.startswith(".")
            )
            for filename in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, filename),
                                      self.workspace).replace(os.sep, "/")
                suffix = os.path.splitext(filename)[1].lower()
                if filename in _IGNORE_FILES:
                    continue
                if suffix and suffix not in _LANGUAGE_BY_SUFFIX:
                    continue
                full = Path(dirpath) / filename
                try:
                    stat = full.stat()
                except OSError:
                    continue
                cached = self.data["files"].get(rel)
                files[rel] = [stat.st_mtime, stat.st_size, suffix]
                if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                    pass  # unchanged
                language = _LANGUAGE_BY_SUFFIX.get(suffix, "other")
                languages[language] = languages.get(language, 0) + 1
                count += 1
                if count >= max_files:
                    _logger.warning("index truncated at %d files", max_files)
                    break
            if count >= max_files:
                break

        self.data.update({
            "version": 1,
            "built": time.time(),
            "files": files,
            "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        })
        self.data["manifests"] = self._read_manifests()
        self.data["entrypoints"] = self._detect_entrypoints(files)
        self.data["build"] = self._detect_commands()
        self.data["git"] = self._git_info()
        self.save_cache()
        return self.data

    def load_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if payload.get("version") != 1:
            return False
        self.data = payload
        return True

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.data), encoding="utf-8")

    # -------------------------------------------------------------- manifests

    def _read_manifests(self) -> dict[str, Any]:
        manifests: dict[str, Any] = {}
        pyproject = self.workspace / "pyproject.toml"
        if pyproject.exists():
            manifests["python"] = self._parse_pyproject(pyproject)
        for name in ("requirements.txt", "requirements-dev.txt"):
            path = self.workspace / name
            if path.exists():
                manifests.setdefault("python", {})["deps"] = \
                    [line.split("#")[0].strip()
                     for line in path.read_text("utf-8").splitlines()
                     if line.strip() and not line.startswith("-")]
        package = self.workspace / "package.json"
        if package.exists():
            manifests["node"] = self._parse_package_json(package)
        go_mod = self.workspace / "go.mod"
        if go_mod.exists():
            manifests["go"] = {"deps": [
                line.split()[1] for line in go_mod.read_text("utf-8").splitlines()
                if line.startswith("\t") and " " in line]}
        cargo = self.workspace / "Cargo.toml"
        if cargo.exists():
            manifests["rust"] = {"raw": cargo.read_text("utf-8")[:4000]}
        for extra in ("composer.json", "Gemfile", "pom.xml", "build.gradle",
                      "build.gradle.kts", "CMakeLists.txt", "Makefile",
                      "Dockerfile", "docker-compose.yml", "go.mod"):
            if (self.workspace / extra).exists():
                manifests.setdefault("other_files", []).append(extra)
        return manifests

    @staticmethod
    def _parse_pyproject(path: Path) -> dict[str, Any]:
        import tomllib

        try:
            data = tomllib.loads(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        project = data.get("project") or {}
        return {
            "name": project.get("name") or data.get("tool", {}).get("poetry", {}).get("name"),
            "deps": list((project.get("dependencies") or [])[:80]),
            "scripts": project.get("scripts") or {},
            "python": project.get("requires-python"),
        }

    @staticmethod
    def _parse_package_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text("utf-8"))
        except ValueError as exc:
            return {"error": str(exc)}
        return {
            "name": data.get("name"),
            "scripts": data.get("scripts") or {},
            "deps": list((data.get("dependencies") or {}).keys())[:80],
            "dev_deps": list((data.get("devDependencies") or {}).keys())[:80],
        }

    # ------------------------------------------------------------- entrypoints

    def _detect_entrypoints(self, files: dict[str, list]) -> list[str]:
        candidates: list[str] = []
        for rel in files:
            base = os.path.basename(rel).lower()
            if base in {"main.py", "app.py", "manage.py", "cli.py", "server.py",
                        "index.js", "index.ts", "main.go", "main.rs",
                        "main.java", "main.cpp", "main.c"}:
                candidates.append(rel)
        if "package.json" in files:
            try:
                data = json.loads((self.workspace / "package.json").read_text("utf-8"))
                if data.get("main"):
                    candidates.append(str(data["main"]))
            except (OSError, ValueError):
                pass
        # cmd/<name>/main.go layout
        for rel in files:
            parts = rel.split("/")
            if len(parts) == 2 and parts[0] == "cmd" and parts[1].endswith("main.go"):
                candidates.append(rel)
        return sorted(set(candidates))[:20]

    # --------------------------------------------------------------- commands

    def _detect_commands(self) -> dict[str, Any]:
        build: dict[str, Any] = {}
        has = lambda name: (self.workspace / name).exists()  # noqa: E731
        if has("package.json"):
            try:
                data = json.loads((self.workspace / "package.json").read_text("utf-8"))
                scripts = data.get("scripts") or {}
                if "test" in scripts:
                    build["test"] = "npm test"
                if "build" in scripts:
                    build["build"] = "npm run build"
                if "lint" in scripts:
                    build["lint"] = "npm run lint"
            except (OSError, ValueError):
                pass
        if has("Makefile"):
            targets = self._makefile_targets()
            for target in ("test", "build", "lint"):
                if target in targets:
                    build.setdefault(target, f"make {target}")
        if has("go.mod"):
            build.setdefault("test", "go test ./...")
            build.setdefault("build", "go build ./...")
        if has("Cargo.toml"):
            build.setdefault("test", "cargo test")
            build.setdefault("build", "cargo build")
        if has("pyproject.toml") or has("pytest.ini") or has("tests") \
                or any(os.path.basename(rel).startswith("test_")
                       for rel in self.data.get("files", {})):
            build.setdefault("test", "python -m pytest -q")
        return build

    def _makefile_targets(self) -> set[str]:
        try:
            text = (self.workspace / "Makefile").read_text("utf-8")
        except OSError:
            return set()
        return {match.group(1) for match in
                re.finditer(r"^([A-Za-z0-9_.-]+):", text, re.MULTILINE)}

    def _git_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        git_dir = self.workspace / ".git"
        if not git_dir.exists():
            return info
        info["repository"] = True
        try:
            head = (git_dir / "HEAD").read_text("utf-8").strip()
            if head.startswith("ref:"):
                info["branch"] = head.split("/")[-1]
        except OSError:
            pass
        try:
            config = (git_dir / "config").read_text("utf-8")
            remotes = re.findall(r'\[remote "(\w+)"\][^\[]*?url = (\S+)',
                                 config, re.DOTALL)
            info["remotes"] = [f"{name} {url}" for name, url in remotes][:5]
        except OSError:
            pass
        return info

    # --------------------------------------------------------------- reporting

    def summary(self) -> str:
        """Compact text summary injected into the agent system prompt."""
        if not self.data.get("built"):
            self.build()
        files = self.data.get("files") or {}
        languages = self.data.get("languages") or {}
        parts: list[str] = [f"Files indexed: {len(files)}"]
        if languages:
            top = ", ".join(f"{name} ({count})" for name, count in
                            list(languages.items())[:5])
            parts.append(f"Languages: {top}")
        manifests = self.data.get("manifests") or {}
        for kind in ("python", "node", "go", "rust"):
            manifest = manifests.get(kind)
            if isinstance(manifest, dict):
                if manifest.get("name"):
                    parts.append(f"Project: {manifest['name']} ({kind})")
                if manifest.get("deps"):
                    parts.append(f"{kind} deps: {', '.join(map(str, manifest['deps'][:12]))}"
                                 + (" ..." if len(manifest["deps"]) > 12 else ""))
        entrypoints = self.data.get("entrypoints") or []
        if entrypoints:
            parts.append(f"Entrypoints: {', '.join(entrypoints[:8])}")
        build = self.data.get("build") or {}
        if build:
            parts.append("Commands: " + "; ".join(
                f"{kind}={command}" for kind, command in build.items()))
        git = self.data.get("git") or {}
        if git.get("repository"):
            parts.append(f"Git: branch {git.get('branch', '?')}"
                         + (f", {len(git.get('remotes', []))} remote(s)"
                            if git.get("remotes") else ""))
        parts.append("Ignored dirs: dependency/build/VCS directories are "
                     "excluded from listing and search by default.")
        return "\n".join(parts)

    def files_listing(self, limit: int = 400) -> str:
        files = sorted((self.data.get("files") or {}).keys())
        if not files:
            return "No indexed files."
        shown = files[:limit]
        by_dir: dict[str, list[str]] = {}
        for rel in shown:
            parts = rel.split("/")
            directory = "/".join(parts[:-1]) or "."
            by_dir.setdefault(directory, []).append(parts[-1])
        lines = [f"{directory}/: {', '.join(names)}"
                 for directory, names in sorted(by_dir.items())]
        if len(files) > limit:
            lines.append(f"[{len(files) - limit} more files...]")
        return "\n".join(lines)
