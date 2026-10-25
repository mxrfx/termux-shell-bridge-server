import json
import os
import re
import secrets
import signal
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
TOKEN_FILE = ROOT / "token.txt"
LOG_DIR.mkdir(exist_ok=True)

if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(
        secrets.token_urlsafe(32),
        encoding="utf-8"
    )

TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()
BASH = "/data/data/com.termux/files/usr/bin/bash"
HOME = "/data/data/com.termux/files/home"
MAX_OUTPUT = 50000
MAX_COMMAND = 20000
MAX_JOBS = 3
MAX_TIMEOUT = 120000

jobs = {}
jobs_lock = __import__("threading").Lock()


def redact(text):
    if not text:
        return ""

    patterns = [
        (
            r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(token|password|passwd|api[_-]?key|secret)"
            r"(\s*[:=]\s*)[^\s\"']+",
            r"\1\2[REDACTED]"
        ),
        (
            r"(?i)(sk-[a-z0-9_-]{10,})",
            "[REDACTED_KEY]"
        ),
    ]

    result = str(text)

    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result)

    return result


def write_audit(kind, data):
    path = LOG_DIR / "audit.log"

    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": kind,
        **data,
    }

    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(
            record,
            ensure_ascii=False
        ) + "\n")


def send_json(handler, status, data):
    raw = json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type"
    )
    handler.send_header(
        "Content-Length",
        str(len(raw))
    )
    handler.end_headers()
    handler.wfile.write(raw)


def authorized(handler):
    return handler.headers.get("Authorization", "") == (
        f"Bearer {TOKEN}"
    )


def read_body(handler):
    size = int(handler.headers.get("Content-Length", "0"))

    if size > 30000:
        raise ValueError("请求内容过大")

    return json.loads(handler.rfile.read(size) or b"{}")


def shell_env():
    return {
        "PATH": (
            "/data/data/com.termux/files/usr/bin:"
            "/data/data/com.termux/files/usr/bin/applets"
        ),
        "HOME": HOME,
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
    }


def kill_process_group(process, force=False):
    try:
        os.killpg(
            process.pid,
            signal.SIGKILL if force else signal.SIGTERM
        )
    except ProcessLookupError:
        pass


def run_shell(command, cwd, timeout):
    started = time.time()

    process = subprocess.Popen(
        [BASH, "-lc", command],
        cwd=cwd or HOME,
        env=shell_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    timed_out = False

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group(process)

        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            kill_process_group(process, force=True)
            stdout, stderr = process.communicate()

    result = {
        "ok": process.returncode == 0 and not timed_out,
        "exit_code": -1 if timed_out else process.returncode,
        "stdout": (stdout or "")[-MAX_OUTPUT:],
        "stderr": (
            "命令超时，进程组已终止"
            if timed_out
            else (stderr or "")[-MAX_OUTPUT:]
        ),
        "duration_ms": round(
            (time.time() - started) * 1000
        ),
    }

    write_audit(
        "shell",
        {
            "command": redact(command),
            "cwd": redact(cwd or HOME),
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "stdout": redact(result["stdout"])[-5000:],
            "stderr": redact(result["stderr"])[-5000:],
        }
    )

    return result


def job_info(job_id, job):
    process = job["process"]
    log_path = job["log"]

    try:
        log_text = log_path.read_text(
            encoding="utf-8",
            errors="replace"
        )[-MAX_OUTPUT:]
    except FileNotFoundError:
        log_text = ""

    return {
        "ok": True,
        "job_id": job_id,
        "status": (
            "running"
            if process.poll() is None
            else "finished"
        ),
        "exit_code": process.poll(),
        "log": redact(log_text),
        "log_file": str(log_path),
        "command": redact(job["command"]),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type"
        )
        self.end_headers()

    def do_GET(self):
        if not authorized(self):
            return send_json(
                self,
                401,
                {"error": "Token 无效"}
            )

        if self.path == "/health":
            with jobs_lock:
                running = sum(
                    job["process"].poll() is None
                    for job in jobs.values()
                )

            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "termux-shell-bridge",
                    "jobs": len(jobs),
                    "running_jobs": running,
                    "max_jobs": MAX_JOBS,
                }
            )

        if self.path == "/jobs":
            with jobs_lock:
                result = [
                    job_info(job_id, job)
                    for job_id, job in jobs.items()
                ]

            return send_json(
                self,
                200,
                {"ok": True, "jobs": result}
            )

        if self.path.startswith("/jobs/"):
            job_id = unquote(self.path.split("/")[2])

            with jobs_lock:
                job = jobs.get(job_id)

            if not job:
                return send_json(
                    self,
                    404,
                    {"error": "任务不存在"}
                )

            return send_json(
                self,
                200,
                job_info(job_id, job)
            )

        return send_json(
            self,
            404,
            {"error": "Not found"}
        )

    def do_POST(self):
        if not authorized(self):
            return send_json(
                self,
                401,
                {"error": "Token 无效"}
            )

        try:
            data = read_body(self)
        except Exception as error:
            return send_json(
                self,
                400,
                {"error": str(error)}
            )

        if self.path == "/shell":
            command = str(data.get("command", "")).strip()

            if not command:
                return send_json(
                    self,
                    400,
                    {"error": "命令为空"}
                )

            if len(command) > MAX_COMMAND:
                return send_json(
                    self,
                    400,
                    {"error": "命令过长"}
                )

            timeout = min(
                max(int(data.get("timeout", 30000)), 1000),
                MAX_TIMEOUT
            )

            return send_json(
                self,
                200,
                run_shell(
                    command,
                    data.get("cwd") or HOME,
                    timeout / 1000
                )
            )

        if self.path == "/jobs/start":
            command = str(data.get("command", "")).strip()

            if not command:
                return send_json(
                    self,
                    400,
                    {"error": "命令为空"}
                )

            with jobs_lock:
                running = sum(
                    job["process"].poll() is None
                    for job in jobs.values()
                )

                if running >= MAX_JOBS:
                    return send_json(
                        self,
                        429,
                        {
                            "error": (
                                f"后台任务已达到上限：{MAX_JOBS}"
                            )
                        }
                    )

            job_id = uuid.uuid4().hex[:12]
            log_path = LOG_DIR / f"{job_id}.log"
            log_file = log_path.open("w", encoding="utf-8")

            process = subprocess.Popen(
                [BASH, "-lc", command],
                cwd=data.get("cwd") or HOME,
                env=shell_env(),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )

            job = {
                "process": process,
                "log": log_path,
                "file": log_file,
                "command": command,
            }

            with jobs_lock:
                jobs[job_id] = job

            write_audit(
                "job_start",
                {
                    "job_id": job_id,
                    "command": redact(command),
                }
            )

            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "job_id": job_id,
                    "max_jobs": MAX_JOBS,
                }
            )

        if self.path == "/jobs/stop-all":
            stopped = []

            with jobs_lock:
                items = list(jobs.items())

            for job_id, job in items:
                process = job["process"]

                if process.poll() is None:
                    kill_process_group(process)
                    stopped.append(job_id)

            write_audit(
                "job_stop_all",
                {"jobs": stopped}
            )

            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "stopped": stopped,
                }
            )

        if (
            self.path.startswith("/jobs/")
            and self.path.endswith("/stop")
        ):
            job_id = unquote(self.path.split("/")[2])

            with jobs_lock:
                job = jobs.get(job_id)

            if not job:
                return send_json(
                    self,
                    404,
                    {"error": "任务不存在"}
                )

            process = job["process"]

            if process.poll() is None:
                kill_process_group(process)

            write_audit(
                "job_stop",
                {"job_id": job_id}
            )

            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "job_id": job_id,
                }
            )

        return send_json(
            self,
            404,
            {"error": "Not found"}
        )


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True


server = ReusableServer(
    ("127.0.0.1", 8765),
    Handler
)

print(
    "Termux Shell Bridge listening "
    "on 127.0.0.1:8765",
    flush=True
)

try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    with jobs_lock:
        items = list(jobs.items())

    for _, job in items:
        if job["process"].poll() is None:
            kill_process_group(job["process"])

    server.server_close()
