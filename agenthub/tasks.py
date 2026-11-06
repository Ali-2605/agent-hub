"""Task manifests: one small YAML file per script.

A task file lives in <project>/.hub/tasks/<name>.yaml. The manifest is the
single source of truth: the phone menu, the MCP tools agents see, and the
generated TASKS.md are all built from it. See docs/writing-tasks.md.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_NOTIFY = {"always", "failure", "never"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class TaskArg:
    name: str
    description: str = ""
    default: str | None = None
    required: bool = False
    choices: list[str] | None = None


@dataclass
class Task:
    name: str
    project: str
    description: str
    command: str
    cwd: Path
    file: Path
    args: list[TaskArg] = field(default_factory=list)
    confirm: bool = False
    timeout: int = 0  # seconds, 0 = none
    notify: str = "always"
    schedule: str | None = None  # "every 30m" | "every 6h" | "daily 07:30"
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.project}/{self.name}"

    def build_argv(self, values: dict[str, str]) -> list[str]:
        """Fill {arg} placeholders. No shell is involved, so values can't inject commands."""
        merged: dict[str, str] = {}
        for a in self.args:
            if a.name in values:
                v = str(values[a.name])
            elif a.default is not None:
                v = str(a.default)
            elif a.required:
                raise ValueError(f"missing required argument '{a.name}' ({a.description})")
            else:
                v = ""
            if a.choices and v and v not in a.choices:
                raise ValueError(f"'{a.name}' must be one of {a.choices}")
            merged[a.name] = v
        unknown = set(values) - {a.name for a in self.args}
        if unknown:
            raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        argv = []
        for token in shlex.split(self.command):
            filled = token.format(**merged)
            if filled != "" or token == filled:
                argv.append(filled)
        return argv

    def usage(self) -> str:
        parts = [self.name] + [f"{a.name}={'<' + a.name + '>' if a.required else a.default or ''}" for a in self.args]
        return " ".join(parts)


def parse_task(file: Path, project: str, root: Path) -> Task:
    data = yaml.safe_load(file.read_text()) or {}
    name = str(data.get("name") or file.stem)
    if not NAME_RE.match(name):
        raise ValueError(f"{file}: task name '{name}' must be lowercase letters, digits, - or _")
    for key in ("description", "command"):
        if not data.get(key):
            raise ValueError(f"{file}: '{key}' is required")
    args = []
    for arg_name, spec in (data.get("args") or {}).items():
        spec = spec if isinstance(spec, dict) else {"description": str(spec)}
        args.append(TaskArg(
            name=str(arg_name),
            description=str(spec.get("description", "")),
            default=None if spec.get("default") is None else str(spec["default"]),
            required=bool(spec.get("required", False)),
            choices=[str(c) for c in spec["choices"]] if spec.get("choices") else None,
        ))
    notify = str(data.get("notify", "always"))
    if notify not in VALID_NOTIFY:
        raise ValueError(f"{file}: notify must be one of {sorted(VALID_NOTIFY)}")
    cwd = (root / str(data.get("cwd", "."))).resolve()
    return Task(
        name=name, project=project, description=str(data["description"]).strip(),
        command=str(data["command"]).strip(), cwd=cwd, file=file, args=args,
        confirm=bool(data.get("confirm", False)), timeout=int(data.get("timeout", 0)),
        notify=notify, schedule=data.get("schedule"), tags=[str(t) for t in data.get("tags") or []],
        examples=[str(e) for e in data.get("examples") or []],
    )


def load_tasks(projects: dict[str, Path]) -> tuple[dict[str, Task], list[str]]:
    """Returns ({task_id: Task}, [error messages]). Broken files never crash the hub."""
    tasks: dict[str, Task] = {}
    errors: list[str] = []
    for project, root in projects.items():
        for file in sorted((root / ".hub" / "tasks").glob("*.y*ml")):
            try:
                t = parse_task(file, project, root)
                tasks[t.id] = t
            except Exception as e:  # noqa: BLE001 - report, don't crash
                errors.append(f"{file}: {e}")
    return tasks, errors


def resolve(tasks: dict[str, Task], ref: str) -> Task | None:
    """Accept 'project/name' or just 'name' when it is unique."""
    if ref in tasks:
        return tasks[ref]
    matches = [t for t in tasks.values() if t.name == ref]
    return matches[0] if len(matches) == 1 else None


def parse_schedule(spec: str) -> tuple[str, int | tuple[int, int]]:
    """'every 30m' -> ('every', 1800); 'daily 07:30' -> ('daily', (7, 30))."""
    spec = spec.strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*([smhd])", spec)
    if m:
        return "every", int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", spec)
    if m:
        return "daily", (int(m[1]), int(m[2]))
    raise ValueError(f"bad schedule '{spec}' (use 'every 30m', 'every 6h' or 'daily 07:30')")


def catalog_markdown(tasks: dict[str, Task]) -> str:
    """The text agents read to learn your scripts (also written to TASKS.md)."""
    lines = ["# Task catalog", "",
             "Run with `ahub run <task> key=value ...` or the MCP tool `run_task`. "
             "Generated from .hub/tasks/*.yaml — edit those files, not this one.", ""]
    for t in sorted(tasks.values(), key=lambda t: t.id):
        lines += [f"## {t.id}", "", t.description, ""]
        lines.append(f"- usage: `ahub run {t.usage()}`")
        for a in t.args:
            extra = []
            if a.required:
                extra.append("required")
            if a.default is not None:
                extra.append(f"default `{a.default}`")
            if a.choices:
                extra.append("one of " + ", ".join(f"`{c}`" for c in a.choices))
            lines.append(f"  - `{a.name}`: {a.description}" + (f" ({'; '.join(extra)})" if extra else ""))
        if t.confirm:
            lines.append("- asks the user on the phone before running")
        if t.schedule:
            lines.append(f"- runs automatically: {t.schedule}")
        for ex in t.examples:
            lines.append(f"- example: `{ex}`")
        lines.append("")
    return "\n".join(lines)
