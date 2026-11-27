#!/usr/bin/env python3

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


SYSTEM_PROMPT = """You are craft_aris, one unified coding agent.
You build and modify web, mobile and desktop application projects.

Work carefully and autonomously:
1. Inspect the project.
2. Make a short implementation plan.
3. Edit files incrementally.
4. Run the smallest relevant build or test command.
5. Read errors and fix them.
6. Finish only after verification or after clearly reporting a blocker.

Use the provided tools. Do not claim a change unless a tool succeeded.
Never expose secrets or API keys. Prefer existing project conventions.
Keep changes focused on the request."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories below the working workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command in the workspace and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 900, "default": 120},
                },
                "required": ["command"],
            },
        },
    },
]


FORBIDDEN_COMMAND_PARTS = (
    "rm -rf /",
    "rm -rf /*",
    "format c:",
    "del /s /q c:",
    "shutdown ",
    "reboot ",
)


class CraftAris:
    def __init__(self, workspace: str, base_url: str, model: str, api_key: str,
                 max_iterations: int = 24):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_iterations = max_iterations

    def safe_path(self, relative_path: str) -> Path:
        path = (self.workspace / relative_path).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError("Path is outside the workspace")
        return path

    def list_files(self, path=".", depth=2):
        base = self.safe_path(path)
        if not base.exists():
            return {"error": f"Path not found: {path}"}

        ignore = {"node_modules", ".git", "__pycache__", "dist", "build"}
        result = []
        for item in sorted(base.rglob("*")):
            if len(item.relative_to(base).parts) > depth:
                continue
            if any(part in ignore for part in item.relative_to(base).parts):
                continue
            if item.is_dir():
                continue
            result.append(str(item.relative_to(self.workspace)).replace("\\", "/"))
        return {"files": result[:500]}

    def read_file(self, path):
        target = self.safe_path(path)
        if not target.is_file():
            return {"error": f"File not found: {path}"}
        if target.stat().st_size > 1_000_000:
            return {"error": "File is too large"}
        return {"content": target.read_text(encoding="utf-8")}

    def write_file(self, path, content):
        target = self.safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(target.relative_to(self.workspace)).replace("\\", "/")}

    def run_command(self, command, timeout=120):
        normalized = command.strip().lower()
        if any(part in normalized for part in FORBIDDEN_COMMAND_PARTS):
            return {"error": "Forbidden destructive command"}

        completed = subprocess.run(
            command,
            cwd=self.workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout, 900)),
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-10000:],
            "stderr": completed.stderr[-10000:],
        }

    def call_tool(self, name, arguments):
        functions = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "run_command": self.run_command,
        }
        if name not in functions:
            return {"error": f"Unknown tool: {name}"}
        try:
            return functions[name](**arguments)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def api_call(self, messages):
        url = f"{self.base_url}/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))

    def run(self, task):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Workspace: {self.workspace}\n"
                    f"Task: {task}\n"
                    "Complete the task, verify it, and report a concise summary."
                ),
            },
        ]

        for iteration in range(1, self.max_iterations + 1):
            try:
                response = self.api_call(messages)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"API HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"API connection failed: {exc.reason}") from exc

            choice = response["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return message.get("content") or "(No response)"

            print(f"\n[craft_aris iteration {iteration}/{self.max_iterations}]")
            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                raw_arguments = tool_call["function"].get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    arguments = {}
                    result = {"error": f"Invalid JSON arguments: {exc}"}
                else:
                    print(f"{name}({json.dumps(arguments, ensure_ascii=False)})")
                    result = self.call_tool(name, arguments)

                print(json.dumps(result, ensure_ascii=False)[:4000])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

        return "Stopped: maximum iteration limit reached."


def build_parser():
    parser = argparse.ArgumentParser(description="craft_aris unified coding agent")
    parser.add_argument("--workspace", default=".", help="Project directory")
    parser.add_argument("--task", help="Task for the agent")
    parser.add_argument("--model", default=os.getenv("CRAFT_ARIS_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--base-url", default=os.getenv(
        "CRAFT_ARIS_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--max-iterations", type=int, default=24)
    parser.add_argument("--check", action="store_true", help="Check API configuration")
    return parser


def main():
    if load_dotenv:
        load_dotenv()

    args = build_parser().parse_args()
    api_key = os.getenv("CRAFT_ARIS_API_KEY")

    if args.check:
        if not api_key:
            print("ERROR: CRAFT_ARIS_API_KEY is not set")
            return 2
        print(f"API key: set")
        print(f"Base URL: {args.base_url}")
        print(f"Model: {args.model}")
        return 0

    if not api_key:
        print("ERROR: Set CRAFT_ARIS_API_KEY in the environment or .env")
        return 2

    agent = CraftAris(
        workspace=args.workspace,
        base_url=args.base_url,
        model=args.model,
        api_key=api_key,
        max_iterations=args.max_iterations,
    )

    task = args.task
    if not task:
        try:
            task = input("Task: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

    if not task:
        print("ERROR: Task is required")
        return 2

    try:
        result = agent.run(task)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    print("\n[craft_aris result]")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
