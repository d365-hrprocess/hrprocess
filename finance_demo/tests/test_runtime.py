"""Finance runtime and main-application isolation contracts."""

import configparser
from pathlib import Path
import shlex
import sys

import fastapi
import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]


def test_finance_has_exact_requested_interpreter_and_own_dependencies():
    assert sys.version_info[:3] == (3, 13, 3)
    assert sys.prefix != sys.base_prefix
    for module in (fastapi, httpx, uvicorn):
        assert Path(module.__file__).is_relative_to(Path(sys.prefix))


def test_hr_and_finance_are_separate_supervised_processes():
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "deploy/azure/supervisord.conf")
    hr_command = shlex.split(config["program:api"]["command"])
    finance_command = shlex.split(config["program:finance"]["command"])
    assert hr_command[0] == "/app/.venv/bin/uvicorn"
    assert finance_command[0] == "/opt/finance/venv/bin/python"
    assert hr_command[hr_command.index("--port") + 1] != finance_command[finance_command.index("--port") + 1]
    assert finance_command[finance_command.index("--host") + 1] == "127.0.0.1"
    assert finance_command[finance_command.index("--root-path") + 1] == "/finance"
    assert config["program:readiness"]["command"] == "/app/.venv/bin/python /app/azure/runtime.py serve-health"
    assert config["program:finance"]["directory"] != config["program:api"]["directory"]


def test_existing_public_port_and_health_route_remain_owned_by_hr():
    config = (ROOT / "deploy/azure/nginx.conf").read_text()
    health = config.split("location = /healthz {", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8010;" in health
    assert "finance" not in health
    assert "listen 8080;" in config
    assert "listen 8020;" not in config
    assert "location ^~ /finance/ {" in config
    assert "proxy_pass http://127.0.0.1:8020/;" in config
