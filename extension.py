"""网页应用扩展：用 Chrome 应用模式打开站点，每条目独立 profile。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from obox.api.app import APP_OPEN_MSG, app_ui_message
from obox.api.component import ComponentModifyMessage
from obox.api.message import OB_BOX
from obox.api.events import CHECKED_CHANGE, CLICK, INPUT
from obox.api.message import COMPONENT_EVENT_MSG
from obox.api.settings import EXTENSION_CONFIG_MSG
from obox.api.view import view_data_message

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CHROME_FLAGS = ("--app={url}", "--user-data-dir={dir}")
IGNORE_CERT = "--ignore-certificate-errors"


def normalize_site(source: dict[str, Any]) -> dict[str, Any]:
    data = source if isinstance(source, dict) else {}
    site_id = data.get("id")
    try:
        site_id = int(site_id) if site_id not in (None, "") else None
    except (TypeError, ValueError):
        site_id = None
    def _text(key: str) -> str:
        return str(data.get(key) or "").strip()

    return {
        "id": site_id,
        "name": _text("name"),
        "url": _text("url"),
        "desc": _text("desc"),
        "ignore": bool(data.get("ignore")),
        "extraOrigins": _text("extraOrigins"),
        "windowSize": _text("windowSize"),
        "windowPos": _text("windowPos"),
        "startMaximized": bool(data.get("startMaximized")),
        "lang": _text("lang"),
        "scaleFactor": _text("scaleFactor"),
        "disableGpu": bool(data.get("disableGpu")),
    }


def validate_site(site: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not site.get("name"):
        errors.append("needName")
    else:
        same = [row for row in rows if row.get("name") == site["name"] and row.get("id") != site.get("id")]
        if same:
            errors.append("dupName")
    url = str(site.get("url") or "")
    if not (url.startswith("http://") or url.startswith("https://")):
        errors.append("needUrl")
    return errors


def chrome_candidates() -> list[str]:
    """注册表 App Paths → Program Files → PATH。"""
    candidates: list[str] = []
    if os.name == "nt":
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe",
                    )
                    value, _kind = winreg.QueryValueEx(key, "")
                    if value:
                        candidates.append(str(value))
                except OSError:
                    continue
        except ImportError:
            pass
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        candidates.append(found)
    return candidates


def find_chrome(candidates: list[str] | None = None) -> str:
    for path in candidates if candidates is not None else chrome_candidates():
        if path and Path(path).is_file():
            return str(path)
    return ""


def _pair(value: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


def build_chrome_args(chrome: str, site: dict[str, Any], profile_dir: Path) -> list[str]:
    args = [chrome, f"--app={site['url']}", f"--user-data-dir={profile_dir}"]
    if site.get("ignore"):
        args.append(IGNORE_CERT)
        origins = [site["url"].split("/")[0] + "//" + site["url"].split("/")[2]]
        for origin in str(site.get("extraOrigins") or "").split(","):
            origin = origin.strip()
            if origin and origin not in origins:
                origins.append(origin)
        if origins:
            args.append("--unsafely-treat-insecure-origin-as-secure=" + ",".join(origins))
    size = _pair(str(site.get("windowSize") or ""))
    if size:
        args.append(f"--window-size={size[0]},{size[1]}")
    position = _pair(str(site.get("windowPos") or ""))
    if position:
        args.append(f"--window-position={position[0]},{position[1]}")
    if site.get("startMaximized"):
        args.append("--start-maximized")
    if site.get("lang"):
        args.append(f"--lang={site['lang']}")
    if site.get("scaleFactor"):
        args.append(f"--force-device-scale-factor={site['scaleFactor']}")
    if site.get("disableGpu"):
        args.append("--disable-gpu")
    return args


def dir_size_mb(path: Path) -> float:
    total = 0
    if not path.exists():
        return 0.0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def launch_chrome(args: list[str]) -> None:
    subprocess.Popen(
        args,
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )


def activate(context) -> None:
    storage = Path(context.storage_dir)
    sites_path = storage / "sites.json"
    state: dict[str, Any] = {
        "sites": list(load_json(sites_path, [])),
        "query": "",
        "form": {"name": "", "url": "", "ignore": False},
        "editing": False,
        "status": "",
        "pending_name": "",
        "pending_delete": 0,
        "window_id": "",
        "config": {"profileroot": "", "limitmb": "2048"},
    }

    def _filtered() -> list[dict[str, Any]]:
        text = state["query"].strip().casefold()
        if not text:
            return list(state["sites"])
        return [
            site
            for site in state["sites"]
            if text in site["name"].casefold() or text in site["url"].casefold()
        ]

    def _set_modal(modal_id: str, visible: bool) -> None:
        try:
            context.request(
                ComponentModifyMessage(
                    src=context.extension_id,
                    dst=OB_BOX,
                    component_id=modal_id,
                    state_type="visible",
                    value=visible,
                )
            )
        except Exception:
            pass

    def _push() -> None:
        if not state["window_id"]:
            return
        sites = [
            {"id": site["id"], "name": site["name"], "url": site["url"]}
            for site in _filtered()
        ]
        context.send(
            view_data_message(
                context.extension_id,
                f"app.{state['window_id']}",
                {
                    "sites": sites,
                    "empty": not sites,
                    "query": state["query"],
                    "form": dict(state["form"]),
                    "editing": state["editing"],
                    "status": state["status"],
                    "pendingName": state["pending_name"],
                },
            )
        )

    def _profile_root() -> Path:
        configured = str(state["config"].get("profileroot") or "").strip()
        return Path(configured) if configured else storage / "profiles"

    def _open(site: dict[str, Any]) -> None:
        if platform.system() != "Windows":
            state["status"] = "windowsOnly"
            return
        chrome = find_chrome()
        if not chrome:
            state["status"] = "noChrome"
            return
        profile = _profile_root() / f"site-{site['id']}"
        limit = str(state["config"].get("limitmb") or "2048")
        if limit != "0" and dir_size_mb(profile) > int(limit):
            state["status"] = "overLimit"
            return
        try:
            launch_chrome(build_chrome_args(chrome, site, profile))
            state["status"] = "opened"
        except OSError:
            state["status"] = "noChrome"

    def _save_form() -> None:
        site = normalize_site({**state["form"], "id": state.get("editing_id")})
        errors = validate_site(site, state["sites"])
        if errors:
            state["status"] = errors[0]
            return
        if site["id"] is None:
            next_id = max([int(row["id"]) for row in state["sites"]] or [0]) + 1
            site["id"] = next_id
            state["sites"].append(site)
        else:
            state["sites"] = [site if row["id"] == site["id"] else row for row in state["sites"]]
        save_json(sites_path, state["sites"])
        state["editing"] = False
        state["status"] = ""
        _set_modal("site-modal", False)

    def _on_event(message) -> None:
        component_id = str(getattr(message, "id", "") or "")
        payload = message.payload or {}
        if message.event == INPUT and component_id == "webapp-search":
            state["query"] = str(payload.get("value") or "")
        elif message.event == INPUT and component_id == "site-name":
            state["form"]["name"] = str(payload.get("value") or "")
        elif message.event == INPUT and component_id == "site-url":
            state["form"]["url"] = str(payload.get("value") or "")
        elif message.event == CHECKED_CHANGE and component_id == "site-ignore":
            state["form"]["ignore"] = bool(payload.get("checked"))
        elif message.event == INPUT and component_id in {
            "site-desc", "site-extra", "site-size", "site-pos", "site-lang", "site-scale"
        }:
            key = {
                "site-desc": "desc", "site-extra": "extraOrigins", "site-size": "windowSize",
                "site-pos": "windowPos", "site-lang": "lang", "site-scale": "scaleFactor",
            }[component_id]
            state["form"][key] = str(payload.get("value") or "")
        elif message.event == CHECKED_CHANGE and component_id in {"site-max", "site-gpu"}:
            key = "startMaximized" if component_id == "site-max" else "disableGpu"
            state["form"][key] = bool(payload.get("checked"))
        elif message.event == CLICK and component_id == "webapp-new":
            state["form"] = {
                "name": "",
                "url": "",
                "desc": "",
                "ignore": False,
                "extraOrigins": "",
                "windowSize": "",
                "windowPos": "",
                "startMaximized": False,
                "lang": "",
                "scaleFactor": "",
                "disableGpu": False,
            }
            state["editing"] = False
            state["status"] = ""
            _push()
            _set_modal("site-modal", True)
            return
        elif message.event == CLICK and component_id == "site-save":
            _save_form()
        elif message.event == CLICK and component_id == "site-cancel":
            state["editing"] = False
            state["status"] = ""
            _set_modal("site-modal", False)
        elif message.event == CLICK and component_id.startswith("open-"):
            site = next((item for item in state["sites"] if f"open-{item['id']}" == component_id), None)
            if site is not None:
                _open(site)
        elif message.event == CLICK and component_id.startswith("edit-"):
            site = next((item for item in state["sites"] if f"edit-{item['id']}" == component_id), None)
            if site is not None:
                state["form"] = {**site}
                state["editing"] = True
                state["editing_id"] = site["id"]
                state["status"] = ""
                _push()
                _set_modal("site-modal", True)
                return
        elif message.event == CLICK and component_id == "delete-cancel":
            state["pending_delete"] = 0
            state["pending_name"] = ""
            _set_modal("confirm-modal", False)
        elif message.event == CLICK and component_id == "delete-ok":
            target = state["pending_delete"]
            state["sites"] = [row for row in state["sites"] if row["id"] != target]
            save_json(sites_path, state["sites"])
            state["pending_delete"] = 0
            state["pending_name"] = ""
            state["status"] = ""
            _set_modal("confirm-modal", False)
        elif message.event == "close" and component_id == "confirm-modal":
            state["pending_delete"] = 0
            state["pending_name"] = ""
        elif message.event == "close" and component_id == "site-modal":
            state["editing"] = False
            state["status"] = ""
        elif message.event == CLICK and component_id.startswith("del-"):
            site = next((item for item in state["sites"] if f"del-{item['id']}" == component_id), None)
            if site is not None:
                state["pending_delete"] = site["id"]
                state["pending_name"] = site["name"]
                state["status"] = ""
                _push()
                _set_modal("confirm-modal", True)
                return
        _push()

    def _on_open(message) -> None:
        payload = message.payload or {}
        state["window_id"] = str(payload.get("window_id") or "")
        context.send(app_ui_message(context.extension_id, state["window_id"], oml="views/main.oml"))
        _push()

    def _on_config(message) -> None:
        payload = message.payload or {}
        values = payload.get("values") or {}
        state["config"] = {
            "profileroot": str(values.get("profileroot") or ""),
            "limitmb": str(values.get("limitmb") or "2048"),
        }

    context.on(APP_OPEN_MSG, _on_open)
    context.on(EXTENSION_CONFIG_MSG, _on_config)
    context.on(COMPONENT_EVENT_MSG, _on_event)


def deactivate(context) -> None:
    context.subscriptions.clear()