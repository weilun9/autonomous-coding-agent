"""Permission gate for the coding agent: decides allow / ask / deny for every tool call.

Pure Python with no SDK import, so it can be unit-tested (tests/test_gate.py) and reasoned about on its own.
agent.py calls `check()` from a PreToolUse hook (every tool call) and again from `can_use_tool` before asking a person.

Rules:
  1. Only the tools in ALLOWED_TOOLS exist; anything else is denied.
  2. File tools (Read / Write / Edit / Glob / Grep) may only touch paths inside the workspace.
  3. Bash commands are parsed (shlex), not substring-matched:
       - split into simple commands on ; && || | & ( ) and newlines, including `$( )`, backticks and `bash -c "..."`;
       - deny: privilege escalation, disk/system destruction, deleting or writing outside the workspace,
         piping into a shell, eval, fork bombs, anything that cannot be parsed;
       - ask a person: anything that publishes or talks to a remote (git push, gh write commands, package
         publish, docker push, scp/ssh, uploads with curl/wget);
       - allow everything else.
  The most severe result over all simple commands wins.

What this cannot see (see README, Known limits): what a script or a `python -c` program does once it runs.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ALLOWED_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")
_PATH_KEYS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path", "Grep": "path"}

_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class Decision:
    action: str          # "allow" | "ask" | "deny"
    reason: str = ""


ALLOW = Decision("allow")


def _worst(decisions) -> Decision:
    out = ALLOW
    for d in decisions:
        if _SEVERITY[d.action] > _SEVERITY[out.action]:
            out = d
    return out


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _to_path(raw: str, cwd: Path | None) -> Path | None:
    """Resolve a path the way a shell would, or None if it cannot be checked (variables, unknown cwd)."""
    if "$" in raw or "%" in raw:
        return None
    raw = os.path.expanduser(raw)
    m = re.match(r"^/([a-zA-Z])(/|$)", raw)
    if os.name == "nt" and m:                       # Git Bash style /c/Users/... -> C:/Users/...
        raw = f"{m.group(1)}:/{raw[3:]}"
    p = Path(raw)
    if not p.is_absolute():
        if cwd is None:
            return None
        p = cwd / p
    return p.resolve(strict=False)


def inside(raw: str, workspace: Path, cwd: Path | None = None, allow_root: bool = True) -> bool:
    """True if `raw` resolves to the workspace or something under it."""
    if raw in ("/dev/null", "NUL", "nul"):
        return True
    ws = workspace.resolve()
    p = _to_path(raw, cwd if cwd is not None else ws)
    if p is None:
        return False
    if p == ws:
        return allow_root
    return ws in p.parents


def _glob_root(pattern: str) -> str:
    """The fixed part of a glob pattern, e.g. '../src/**/*.py' -> '../src'."""
    parts = re.split(r"[\\/]", pattern)
    fixed = []
    for part in parts:
        if any(ch in part for ch in "*?[{"):
            break
        fixed.append(part)
    root = "/".join(fixed)
    if pattern.startswith(("/", "\\")) and not root:
        return "/"
    return root or "."


# --------------------------------------------------------------------------- #
# File tools
# --------------------------------------------------------------------------- #

def _check_file_tool(tool: str, data: dict, workspace: Path) -> Decision:
    key = _PATH_KEYS[tool]
    raw = data.get(key)
    if raw and not inside(str(raw), workspace):
        return Decision("deny", f"{tool} outside the workspace: {raw}")
    if tool == "Glob":
        base = str(raw) if raw else "."
        root = _glob_root(str(data.get("pattern", "")))
        target = root if Path(root).is_absolute() or root.startswith(("/", "~")) else os.path.join(base, root)
        if not inside(target, workspace):
            return Decision("deny", f"Glob pattern reaches outside the workspace: {data.get('pattern')}")
    return ALLOW


# --------------------------------------------------------------------------- #
# Bash
# --------------------------------------------------------------------------- #

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
OTHER_SHELLS = {"pwsh", "powershell", "cmd"}
WRAPPERS = {"command", "nohup", "time", "exec", "builtin", "nice"}
PRIVILEGE = {"sudo", "su", "doas", "runas", "pkexec"}
SYSTEM_KILLERS = {"shutdown", "reboot", "halt", "poweroff", "fdisk", "diskpart", "format", "wipefs", "parted"}
WRITE_COMMANDS = {"rm", "rmdir", "unlink", "mv", "cp", "touch", "mkdir", "tee", "chmod", "chown", "chgrp", "ln",
                  "truncate", "install", "shred", "del", "erase", "rd", "remove-item"}
REMOTE_COMMANDS = {"ssh", "scp", "sftp", "ftp", "telnet", "nc", "ncat", "netcat"}
PUBLISH = {("npm", "publish"), ("pnpm", "publish"), ("yarn", "publish"), ("twine", "upload"), ("poetry", "publish"),
           ("cargo", "publish"), ("gem", "push"), ("docker", "push"), ("docker", "login"), ("podman", "push"),
           ("flit", "publish"), ("hatch", "publish"), ("vercel", "deploy"), ("netlify", "deploy")}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
REDIRECT_OUT = {">", ">>", ">|", "&>", "&>>"}
REDIRECT_SKIP = {"<", "<<", "<<<", ">&", "<&", "<>"}
GH_READ_ONLY = {"view", "list", "status", "diff", "checks", "search", "browse"}

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies: they are data written to a file, not commands."""
    lines, out, delim = cmd.split("\n"), [], None
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m:
            delim = m.group(2)
    return "\n".join(out)


def _newlines_to_semicolons(cmd: str) -> str:
    """Unquoted newlines separate commands; shlex would treat them as plain whitespace."""
    out, quote, escaped = [], None, False
    for ch in cmd:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            continue
        out.append(ch)
    return "".join(out)


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _segments(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Split tokens into simple commands; the flag says whether the command reads from a pipe."""
    segs, cur, piped, next_piped = [], [], False, False
    for tok in tokens:
        if tok in SEPARATORS:
            if cur:
                segs.append((cur, piped))
            cur, piped = [], False
            next_piped = tok in ("|", "|&")
            continue
        if not cur:
            piped, next_piped = next_piped, False
        cur.append(tok)
    if cur:
        segs.append((cur, piped))
    return segs


def _program(words: list[str]) -> tuple[str, list[str]]:
    """Skip VAR=value prefixes and wrappers such as env / nohup / time; return (program, args)."""
    i = 0
    while i < len(words):
        w = words[i]
        name = os.path.basename(w).lower().removesuffix(".exe")
        if _ASSIGNMENT.match(w):
            i += 1
        elif name in WRAPPERS:
            i += 1
        elif name == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGNMENT.match(words[i])):
                i += 1
        else:
            return name, words[i + 1:]
    return "", []


def _paths_in(args: list[str], cmd_name: str) -> list[str]:
    out = []
    for a in args:
        if a == "--":
            continue
        if a.startswith("-"):
            continue
        if cmd_name in ("del", "erase", "rd") and re.fullmatch(r"/[a-zA-Z]", a):
            continue                                   # cmd.exe switches such as /f /s /q
        out.append(a)
    return out


def _check_git(args: list[str], ws: Path, cwd: Path | None) -> Decision:
    i = 0
    while i < len(args):                               # global options before the subcommand
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            if a in ("-C", "--git-dir", "--work-tree") and i + 1 < len(args) and not inside(args[i + 1], ws, cwd):
                return Decision("deny", f"git pointed outside the workspace: {args[i + 1]}")
            i += 2
        elif a.startswith(("--git-dir=", "--work-tree=")):
            if not inside(a.split("=", 1)[1], ws, cwd):
                return Decision("deny", f"git pointed outside the workspace: {a}")
            i += 1
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args):
        return ALLOW
    sub, rest = args[i], args[i + 1:]
    if sub == "push" or sub == "send-email":
        return Decision("ask", f"git {sub} publishes to a remote")
    if sub == "remote" and rest and rest[0] in ("add", "set-url", "rename", "remove", "rm"):
        return Decision("ask", f"git remote {rest[0]} changes where code is published")
    if sub == "config" and any(o in rest for o in ("--global", "--system")):
        return Decision("deny", "git config --global/--system changes settings outside the workspace")
    return ALLOW


def _check_gh(args: list[str]) -> Decision:
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return ALLOW
    if words[0] == "auth" and len(words) > 1 and words[1] == "status":
        return ALLOW
    if words[0] == "api":
        method = None
        for j, a in enumerate(args):
            if a in ("-X", "--method") and j + 1 < len(args):
                method = args[j + 1].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            elif a in ("-f", "-F", "--field", "--raw-field", "--input"):
                method = method or "POST"
        if method in (None, "GET", "HEAD"):
            return ALLOW
        return Decision("ask", f"gh api {method} changes data on GitHub")
    if len(words) > 1 and words[1] in GH_READ_ONLY:
        return ALLOW
    return Decision("ask", f"gh {' '.join(words[:2])} may change something on GitHub")


def _check_curl_wget(name: str, args: list[str], ws: Path, cwd: Path | None) -> Decision:
    upload = {"-T", "--upload-file", "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode",
              "-F", "--form", "--post-data", "--post-file", "--json"}
    for j, a in enumerate(args):
        flag = a.split("=", 1)[0]
        if flag in upload:
            return Decision("ask", f"{name} sends data to a remote server")
        if flag in ("-X", "--request", "--method"):
            method = (a.split("=", 1)[1] if "=" in a else (args[j + 1] if j + 1 < len(args) else "")).upper()
            if method not in ("GET", "HEAD"):
                return Decision("ask", f"{name} {method} sends data to a remote server")
        if flag in ("-o", "--output", "-O", "--output-document") and "=" not in a and j + 1 < len(args):
            if flag in ("-o", "--output", "--output-document") and not inside(args[j + 1], ws, cwd):
                return Decision("deny", f"{name} writes outside the workspace: {args[j + 1]}")
    return ALLOW


def _check_simple(words: list[str], piped: bool, ws: Path, cwd: Path | None, depth: int) -> tuple[Decision, Path | None]:
    """Check one simple command. Returns the decision and the working directory after it (for `cd`)."""
    # redirections first: anything written by > or >> must stay in the workspace
    plain, k = [], 0
    while k < len(words):
        w = words[k]
        if w in REDIRECT_OUT:
            if k + 1 < len(words) and not inside(words[k + 1], ws, cwd):
                return Decision("deny", f"redirect writes outside the workspace: {words[k + 1]}"), cwd
            k += 2
            continue
        if w in REDIRECT_SKIP:
            k += 2
            continue
        plain.append(w)
        k += 1

    name, args = _program(plain)
    if not name:
        return ALLOW, cwd

    if name == "cd":
        target = args[0] if args else "~"
        new = _to_path(target, cwd) if target != "-" else None
        return ALLOW, new

    if name in PRIVILEGE:
        return Decision("deny", f"{name}: privilege escalation is not allowed"), cwd
    if name in SYSTEM_KILLERS or name.startswith("mkfs") or (name == "init" and args[:1] in (["0"], ["6"])):
        return Decision("deny", f"{name}: system-level destructive command"), cwd
    if name == "systemctl" and any(a in ("poweroff", "reboot", "halt") for a in args):
        return Decision("deny", "systemctl power command"), cwd
    if name == "kill" and "-1" in args:
        return Decision("deny", "kill -1 targets every process"), cwd
    if name == "eval":
        return Decision("deny", "eval cannot be checked; run the command directly"), cwd
    if name == "dd":
        for a in args:
            if a.startswith("of=") and not inside(a[3:], ws, cwd):
                return Decision("deny", f"dd writes outside the workspace: {a}"), cwd
        return ALLOW, cwd

    if name in SHELLS:
        if "-c" in args:
            j = args.index("-c")
            if j + 1 < len(args):
                return _check_command(args[j + 1], ws, cwd, depth + 1), cwd
        if piped and not [a for a in args if not a.startswith("-")]:
            return Decision("deny", f"piping into {name} runs code that cannot be checked"), cwd
        return ALLOW, cwd
    if name in OTHER_SHELLS:
        if any(a.lower() in ("-c", "-command", "/c", "/k") for a in args):
            return Decision("ask", f"{name} runs a command in another shell syntax that cannot be checked"), cwd
        return ALLOW, cwd

    if name == "xargs":
        inner_name, _ = _program([a for a in args if not a.startswith("-")])
        if inner_name in WRITE_COMMANDS:
            return Decision("deny", f"xargs {inner_name}: the targets cannot be checked"), cwd
        return _check_simple([a for a in args if not a.startswith("-")], False, ws, cwd, depth)[0], cwd

    if name == "find":
        if any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
            roots = []
            for a in args:
                if a.startswith("-") or a in ("(", "!"):
                    break
                roots.append(a)
            for r in roots or ["."]:
                if not inside(r, ws, cwd):
                    return Decision("deny", f"find -delete/-exec outside the workspace: {r}"), cwd
        return ALLOW, cwd

    if name in WRITE_COMMANDS:
        for p in _paths_in(args, name):
            if name in ("chmod", "chown", "chgrp") and p == _paths_in(args, name)[0]:
                continue                               # the mode / owner argument
            deleting = name in ("rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item")
            if not inside(p, ws, cwd, allow_root=not deleting):
                return Decision("deny", f"{name} outside the workspace (or on the workspace itself): {p}"), cwd
        return ALLOW, cwd

    if name == "git":
        return _check_git(args, ws, cwd), cwd
    if name == "gh":
        return _check_gh(args), cwd
    if name in REMOTE_COMMANDS:
        return Decision("ask", f"{name} connects to a remote host"), cwd
    if name == "rsync":
        remote = re.compile(r"^(?:[\w.-]+@)?[\w.-]+:")      # host:path or user@host:path
        if any(remote.match(a) and not re.match(r"^[a-zA-Z]:[\\/]", a) for a in args):
            return Decision("ask", "rsync to a remote host"), cwd
        return ALLOW, cwd
    if name in ("curl", "wget"):
        return _check_curl_wget(name, args, ws, cwd), cwd
    if name in ("npm", "pnpm", "yarn") and any(a in ("-g", "--global") for a in args):
        return Decision("ask", f"{name} global install writes outside the workspace"), cwd

    positional = [a for a in args if not a.startswith("-")]
    if positional and (name, positional[0]) in PUBLISH:
        return Decision("ask", f"{name} {positional[0]} publishes outside this machine"), cwd
    if name == "dotnet" and positional[:2] == ["nuget", "push"]:
        return Decision("ask", "dotnet nuget push publishes a package"), cwd
    if name.startswith("python") and args[:1] == ["-m"] and len(args) > 1:
        return _check_simple(args[1:], False, ws, cwd, depth)[0], cwd
    return ALLOW, cwd


def _check_command(cmd: str, ws: Path, cwd: Path | None, depth: int = 0) -> Decision:
    if depth > 3:
        return Decision("deny", "too many nested shells to check")
    if re.search(r":\s*\(\s*\)\s*\{", cmd):
        return Decision("deny", "fork bomb")
    decisions = []
    for inner in re.findall(r"`([^`]*)`", cmd):        # backtick substitutions run too
        decisions.append(_check_command(inner, ws, cwd, depth + 1))
    text = _newlines_to_semicolons(_strip_heredocs(cmd.replace("\\\n", " ")))
    try:
        tokens = _tokens(text)
    except ValueError as exc:
        return Decision("deny", f"could not parse the command ({exc}); write it more simply")
    for words, piped in _segments(tokens):
        d, cwd = _check_simple(words, piped, ws, cwd, depth)
        decisions.append(d)
    return _worst(decisions)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def check(tool_name: str, tool_input: dict, workspace: Path) -> Decision:
    """Decide what to do with one tool call."""
    if tool_name not in ALLOWED_TOOLS:
        return Decision("deny", f"tool '{tool_name}' is not on the allow-list ({', '.join(ALLOWED_TOOLS)})")
    workspace = Path(workspace).resolve()
    if tool_name == "Bash":
        return _check_command(str(tool_input.get("command", "")), workspace, workspace)
    return _check_file_tool(tool_name, tool_input or {}, workspace)
