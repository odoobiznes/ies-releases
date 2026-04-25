"""IES Fleet status dashboard — minimal FastAPI app.

Runs on the OCR box (or any customer box) at port 5099. Shows:
  - This host's installed services + versions
  - ies-updater status + last poll
  - Aggregated index.json (what's currently shipping)
  - Sparse uptime / disk / memory

URL endpoints:
  GET /            HTML status page (auto-refresh every 30s)
  GET /api/status  JSON snapshot
  GET /healthz     liveness

No auth — bind to LAN only via --host=192.168.122.0/24 or front with nginx.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, ORJSONResponse
import orjson  # noqa: F401

INDEX_URL = "https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json"
UPDATER_DIR = Path(os.environ.get("IES_UPDATER_DIR", "/opt/ies-updater"))
DEFAULT_INSTALL_BASES = [Path("/opt"), Path(r"C:\Apps") if os.name == "nt" else None]
DEFAULT_INSTALL_BASES = [p for p in DEFAULT_INSTALL_BASES if p and p.exists()]

app = FastAPI(title="IES Fleet Dashboard", version="0.1", default_response_class=ORJSONResponse)


# ============================================================ helpers


def hostname() -> str:
    return socket.gethostname()


def uptime_seconds() -> int:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.readline().split()[0]))
    except FileNotFoundError:
        # Windows: rough approximation via 'net statistics workstation' or wmic
        return -1


def memory() -> dict:
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                return {"total_mb": int(parts[1]), "used_mb": int(parts[2]), "free_mb": int(parts[3])}
    except Exception:
        pass
    return {}


def disk(path: str = "/") -> dict:
    try:
        out = subprocess.run(["df", "-Pm", path], capture_output=True, text=True, timeout=2).stdout
        line = out.splitlines()[-1].split()
        return {"path": path, "total_mb": int(line[1]), "used_mb": int(line[2]), "free_mb": int(line[3]),
                "use_pct": int(line[4].rstrip("%"))}
    except Exception:
        return {}


def fetch_index() -> dict:
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def installed_services() -> list:
    """Inspect /opt and C:\\Apps for installed.json files (where ies-updater wrote them).
    Permission errors on individual entries are silently skipped — dashboard runs as
    a low-priv user and many service install dirs are owned by their own service user."""
    out = []
    for base in DEFAULT_INSTALL_BASES:
        try:
            entries = list(base.iterdir())
        except (PermissionError, OSError):
            continue
        for p in entries:
            try:
                inst = p / "installed.json"
                if not inst.is_file(): continue
                d = json.loads(inst.read_text())
                rel_dir = p / "releases"
                snaps = list(rel_dir.glob("*.tgz")) if rel_dir.is_dir() else []
                out.append({
                    "install_dir": str(p),
                    "service": p.name,
                    "version": d.get("version"),
                    "updated_at": d.get("updated_at"),
                    "rollback_count": len(snaps),
                })
            except (PermissionError, OSError, json.JSONDecodeError):
                continue
    return out


def updater_status() -> dict:
    """systemd state of ies-updater + recent log lines."""
    try:
        active = subprocess.run(["systemctl", "is-active", "ies-updater.service"],
                                capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        active = "unknown"
    try:
        out = subprocess.run(["journalctl", "-u", "ies-updater.service", "-n", "8",
                              "--no-pager", "-o", "short-iso"],
                             capture_output=True, text=True, timeout=4).stdout
        recent = [ln for ln in out.splitlines() if "ies-updater" in ln or "snapshot" in ln or "update" in ln][-5:]
    except Exception:
        recent = []
    cfg = {}
    cfg_p = UPDATER_DIR / "config.yml"
    if cfg_p.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_p.read_text()) or {}
        except Exception:
            pass
    return {
        "active": active,
        "config_path": str(cfg_p),
        "subscriptions": list((cfg.get("subscriptions") or {}).keys()),
        "poll_interval_sec": cfg.get("poll_interval_sec"),
        "release_index_url": cfg.get("release_index"),
        "recent_log": recent,
    }


# ============================================================ endpoints


@app.get("/healthz")
async def healthz():
    return {"ok": True, "host": hostname(), "now": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
async def api_status():
    return {
        "host": hostname(),
        "now": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": uptime_seconds(),
        "memory": memory(),
        "disk": disk("/"),
        "updater": updater_status(),
        "installed": installed_services(),
        "available": fetch_index(),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    s = await api_status()
    upd = s["updater"]
    av_services = s["available"].get("services") or {}

    # Per installed service, compare to available
    rows = []
    installed_by_name = {x["service"]: x for x in s["installed"]}
    # Map common install_dir names → service codes
    name_aliases = {
        "iesocr-deploy": "iesocr-worker", "PohodaDigi": "pohoda-digi", "PohodaAPI": "pohoda-api",
        "PohodaXmlAgent": "pohoda-xml-agent", "PohodaKontrola": "pohoda-kontrola",
        "FormsDoks": "forms-doks", "PohodaApiGateway": "pohoda-api-gateway",
    }
    for inst in s["installed"]:
        svc = name_aliases.get(inst["service"], inst["service"])
        avail = av_services.get(svc, {}).get("stable", "?")
        ver = inst["version"] or "?"
        ok = ver == avail
        ts = inst["updated_at"]
        ts_str = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else "-"
        rows.append((svc, ver, avail, ok, ts_str, inst["rollback_count"], inst["install_dir"]))

    av_rows = "".join(
        f"<tr><td>{k}</td><td>{v.get('stable','?')}</td><td><a href='{v.get('repo','')}' target=_blank>{v.get('repo','')}</a></td></tr>"
        for k, v in sorted(av_services.items())
    )

    inst_rows = "".join(
        f"<tr><td>{svc}</td><td>{ver}</td><td>{avail}</td>"
        f"<td>{'✅' if ok else '⚠️'}</td><td>{ts_str}</td><td>{rb}</td><td><code>{idr}</code></td></tr>"
        for svc, ver, avail, ok, ts_str, rb, idr in rows
    ) or "<tr><td colspan=7 style='text-align:center;color:#888'>no services installed yet — edit /opt/ies-updater/config.yml + restart</td></tr>"

    log_lines = "".join(f"<div>{ln}</div>" for ln in upd["recent_log"]) or "<div style='color:#888'>no recent updater activity</div>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>IES Fleet — {s['host']}</title>
<meta http-equiv="refresh" content="30">
<style>
  body{{font-family:system-ui;max-width:1100px;margin:24px auto;padding:0 16px;color:#111}}
  h1{{margin:0 0 4px 0}}
  h2{{margin:24px 0 8px 0;border-bottom:2px solid #ddd;padding-bottom:4px}}
  table{{border-collapse:collapse;width:100%;font-size:13px}}
  th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;vertical-align:top}}
  th{{background:#f3f3f3}}
  code{{font-size:12px}}
  .meta{{color:#666;font-size:13px}}
  .pill{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#e0f2fe;color:#075985}}
  .pill.green{{background:#dcfce7;color:#166534}}
  .pill.warn{{background:#fef3c7;color:#92400e}}
  .pill.red{{background:#fee2e2;color:#991b1b}}
</style></head><body>

<h1>IES Fleet Dashboard</h1>
<div class="meta">host <strong>{s['host']}</strong> &middot;
  uptime {s['uptime_seconds']//3600}h{(s['uptime_seconds']%3600)//60}m &middot;
  mem {s['memory'].get('used_mb','?')}/{s['memory'].get('total_mb','?')} MB &middot;
  disk {s['disk'].get('used_mb','?')}/{s['disk'].get('total_mb','?')} MB ({s['disk'].get('use_pct','?')}%) &middot;
  rendered {s['now']}
</div>

<h2>ies-updater</h2>
<p>
  status: <span class="pill {'green' if upd['active']=='active' else 'red'}">{upd['active']}</span>
  poll: {upd['poll_interval_sec']}s &middot;
  subscriptions: {', '.join(upd['subscriptions']) or '(none)'} &middot;
  config: <code>{upd['config_path']}</code><br>
  index: <a href="{upd['release_index_url']}" target=_blank>{upd['release_index_url']}</a>
</p>
<div style="font-family:monospace;background:#f9f9f9;padding:8px;border-radius:4px;font-size:12px">{log_lines}</div>

<h2>Installed services on this host</h2>
<table>
  <thead><tr><th>Service</th><th>Installed</th><th>Available stable</th><th>Match</th><th>Last update (UTC)</th><th>Rollback snapshots</th><th>install_dir</th></tr></thead>
  <tbody>{inst_rows}</tbody>
</table>

<h2>Available across the fleet (live index.json)</h2>
<table>
  <thead><tr><th>Service</th><th>Stable</th><th>Repo</th></tr></thead>
  <tbody>{av_rows}</tbody>
</table>

<p class="meta" style="margin-top:24px">
  ies-fleet-dashboard v0.1 &middot; auto-refresh 30s &middot;
  source: <a href="https://github.com/odoobiznes/ies-releases" target=_blank>github.com/odoobiznes/ies-releases</a>
</p>
</body></html>"""
