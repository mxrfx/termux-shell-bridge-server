import json
import os
import secrets
import signal
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
HOME = os.environ.get(
    "HOME",
    "/data/data/com.termux/files/home"
)

jobs = {}

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
    value = handler.headers.get("Authorization", "")
    return value == f"Bearer {TOKEN}"

def read_body(handler):
    size = int(handler.headers.get("Content-Length", "0"))

    if size > 20000:
        raise ValueError("请求内容过大")

    raw = handler.rfile.read(size)
    return json.loads(raw or b"{}")

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

def stop_group(process, force=False):
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
        stdout, stderr = process.communicate(
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_group(process)

        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            stop_group(process, force=True)
            stdout, stderr = process.communicate()

    return {
        "ok": process.returncode == 0 and not timed_out,
        "exit_code": -1 if timed_out else process.returncode,
        "stdout": (stdout or "")[-50000:],
        "stderr": (
            "命令超时，进程组已终止"
            if timed_out
            else (stderr or "")[-50000:]
        ),
        "duration_ms": round(
            (time.time() - started) * 1000
        ),
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
            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "termux-shell-bridge",
                    "jobs": len(jobs),
                }
            )

        if self.path.startswith("/jobs/"):
            job_id = self.path.split("/")[2]
            job = jobs.get(job_id)

            if not job:
                return send_json(
                    self,
                    404,
                    {"error": "任务不存在"}
                )

            process = job["process"]
            return send_json(
                self,
                200,
                {
                    "job_id": job_id,
                    "status": (
                        "running"
                        if process.poll() is None
                        else "finished"
                    ),
                    "exit_code": process.poll(),
                    "log": str(job["log"]),
                }
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

            if len(command) > 20000:
                return send_json(
                    self,
                    400,
                    {"error": "命令过长"}
                )

            timeout = min(
                max(int(data.get("timeout", 30000)), 1000),
                120000
            )

            result = run_shell(
                command,
                data.get("cwd") or HOME,
                timeout / 1000
            )

            return send_json(self, 200, result)

        if self.path == "/jobs/start":
            command = str(data.get("command", "")).strip()

            if not command:
                return send_json(
                    self,
                    400,
                    {"error": "命令为空"}
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

            jobs[job_id] = {
                "process": process,
                "log": log_path,
                "file": log_file,
            }

            return send_json(
                self,
                200,
                {
                    "ok": True,
                    "job_id": job_id,
                }
            )

        if (
            self.path.startswith("/jobs/")
            and self.path.endswith("/stop")
        ):
            job_id = self.path.split("/")[2]
            job = jobs.get(job_id)

            if not job:
                return send_json(
                    self,
                    404,
                    {"error": "任务不存在"}
                )

            process = job["process"]

            if process.poll() is None:
                stop_group(process)

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

server = ThreadingHTTPServer(
    ("127.0.0.1", 8765),
    Handler
)

print(
    "Termux Shell Bridge listening "
    "on 127.0.0.1:8765",
    flush=True
)

server.serve_forever()
