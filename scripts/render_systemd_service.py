#!/usr/bin/env python3
"""Render systemd unit file from config/config.yaml.

This keeps machine-specific paths out of the committed unit file.

Usage:
  .venv/bin/python scripts/render_systemd_service.py \
    --config config/config.yaml \
    --template systemd/octopus-logger.service.template \
    --output systemd/octopus-logger.service
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Union
import os
import getpass

import yaml


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def _get(d: Dict[str, Any], key: str, default: Any) -> Any:
    return d.get(key, default)


def _paths_value(value: Union[str, List[Any]]) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ValueError("read_write_paths must be a string or list")


def render(template_text: str, mapping: Dict[str, str]) -> str:
    out = template_text
    for k, v in mapping.items():
        out = out.replace("{{" + k + "}}", v)
    # Basic check: no unreplaced placeholders
    if "{{" in out or "}}" in out:
        raise ValueError("Template contains unreplaced placeholders")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--template", default="systemd/octopus-logger.service.template")
    ap.add_argument("--output", default="systemd/octopus-logger.service")
    args = ap.parse_args()

    config_path = Path(args.config)
    template_path = Path(args.template)
    output_path = Path(args.output)

    cfg = _load_yaml(config_path)
    sysd = cfg.get("systemd") or {}
    if not isinstance(sysd, dict):
        raise ValueError("config.systemd must be a mapping")

    # Defaults allow existing configs (without a `systemd:` section) to work.
    repo_root = Path(os.getcwd()).resolve()
    default_working_directory = str(repo_root)
    default_python_executable = str(repo_root / ".venv" / "bin" / "python")
    default_main_path = str(repo_root / "src" / "main.py")
    default_user = getpass.getuser()
    default_read_write_paths = [
        str(repo_root / "cache"),
        str(repo_root / "octopus_logger.log"),
    ]

    mapping = {
        "working_directory": str(_get(sysd, "working_directory", default_working_directory)),
        "python_executable": str(_get(sysd, "python_executable", default_python_executable)),
        "main_path": str(_get(sysd, "main_path", default_main_path)),
        "user": str(_get(sysd, "user", default_user)),
        # systemd expects space-separated paths
        "read_write_paths": " ".join(
            _paths_value(_get(sysd, "read_write_paths", default_read_write_paths))
        ),
    }

    template_text = template_path.read_text(encoding="utf-8")
    rendered = render(template_text, mapping)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
