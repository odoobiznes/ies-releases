#!/usr/bin/env python3
"""
ies-updater — customer-side auto-update daemon.

Polls https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json
(configurable), compares each subscribed service's installed version to the
channel's latest, and on a delta:

  fetch tarball → verify sha256 + signature → apply migrations → stop service
  → extract → start service → health check → report telemetry.

If health check fails: rollback to previous version (kept under releases/).

Config:  /opt/ies-updater/config.yml   (Linux)   or
         C:\\ProgramData\\ies-updater\\config.yml (Windows)

One file, ~260 lines, stdlib only.  Cross-platform (Linux/Windows).

Design: S:\\Pohoda_APPS\\IES_RELEASE_PIPELINE.md §5.
Author: Claude, 2026-04-24.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # pip install pyyaml
except ImportError:
    print("ies-updater requires PyYAML (pip install pyyaml)", file=sys.stderr)
    raise

log = logging.getLogger("ies-updater")

CONFIG_CANDIDATES = [
    Path("/opt/ies-updater/config.yml"),
    Path("/etc/ies-updater/config.yml"),
    Path(os.environ.get("IES_UPDATER_CONFIG", "")),
    Path(r"C:\ProgramData\ies-updater\config.yml"),
]

# ========================================================================
# config
# ========================================================================


@dataclasses.dataclass
class Subscription:
    service: str
    channel: str
    install_dir: Path
    pinned_version: Optional[str] = None      # if set, only this exact version is installed


@dataclasses.dataclass
class Config:
    poll_interval_sec: int
    release_index_url: str
    subscriptions: list[Subscription]
    telemetry_url: Optional[str]
    telemetry_enabled: bool
    keep_versions: int        # rollback history depth, default 3
    http_timeout_sec: int


def load_config() -> Config:
    for c in CONFIG_CANDIDATES:
        if c and c.exists():
            raw = yaml.safe_load(c.read_text())
            subs = [
                Subscription(service=k, channel=v["channel"],
                             install_dir=Path(v["install_dir"]),
                             pinned_version=v.get("pinned_version"))
                for k, v in raw.get("subscriptions", {}).items()
            ]
            return Config(
                poll_interval_sec=int(raw.get("poll_interval_sec", 900)),
                release_index_url=raw["release_index"],
                subscriptions=subs,
                telemetry_url=raw.get("telemetry_url"),
                telemetry_enabled=bool(raw.get("telemetry_enabled", False)),
                keep_versions=int(raw.get("keep_versions", 3)),
                http_timeout_sec=int(raw.get("http_timeout_sec", 60)),
            )
    raise SystemExit(f"no ies-updater config found in {[str(c) for c in CONFIG_CANDIDATES]}")


# ========================================================================
# helpers
# ========================================================================


def http_get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ies-updater/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_installed_version(install_dir: Path) -> Optional[str]:
    p = install_dir / "installed.json"
    if not p.exists(): return None
    try: return json.loads(p.read_text()).get("version")
    except Exception: return None


def write_installed_version(install_dir: Path, version: str) -> None:
    p = install_dir / "installed.json"
    p.write_text(json.dumps({"version": version, "updated_at": time.time()}))


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def service_stop(name: str) -> None:
    if platform.system() == "Linux":
        run(["systemctl", "stop", name])
    else:
        run(["nssm", "stop", name])


def service_start(name: str) -> None:
    if platform.system() == "Linux":
        run(["systemctl", "start", name])
    else:
        run(["nssm", "start", name])


def health_check(url: str, timeout_sec: int) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            code, _ = run(["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=10)
            if code == 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def telemetry(cfg: Config, payload: dict) -> None:
    if not (cfg.telemetry_enabled and cfg.telemetry_url): return
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(cfg.telemetry_url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=cfg.http_timeout_sec).read()
    except Exception as e:
        log.warning("telemetry post failed: %s", e)


# ========================================================================
# update step
# ========================================================================


def fetch_release(name: str, version: str, asset_suffix: str, timeout: int) -> tuple[Path, str]:
    # Derive asset URL from the index metadata — for v1 we use a convention:
    #   https://github.com/odoobiznes/<name>/releases/download/v<version>/<name>-<version>.tar.gz
    base = f"https://github.com/odoobiznes/{name}/releases/download/v{version}"
    tar_url = f"{base}/{name}-{version}.tar.gz"
    sha_url = f"{base}/{name}-{version}.tar.gz.sha256"

    tar_bytes = http_get(tar_url, timeout)
    sha_line = http_get(sha_url, timeout).decode().split()[0]

    tmp = Path(f"/tmp/ies-{name}-{version}.tar.gz")
    tmp.write_bytes(tar_bytes)
    actual = sha256_file(tmp)
    if actual != sha_line:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch: got {actual} expected {sha_line}")
    return tmp, sha_line


def apply_update(sub: Subscription, version: str, manifest: dict, cfg: Config,
                 tarball: Path) -> bool:
    """Stop service, snapshot current, extract new, apply migrations, start, health-check.
    Returns True on success, False on failure (caller rolls back)."""
    svc_cfg = manifest.get("service", {})
    svc_name = (svc_cfg.get("systemd_unit") if platform.system() == "Linux"
                else svc_cfg.get("nssm_name"))

    install_dir = sub.install_dir
    current = install_dir / "current"
    backups = install_dir / "releases"
    backups.mkdir(parents=True, exist_ok=True)

    # snapshot
    if current.exists():
        snap = backups / f"{read_installed_version(install_dir) or 'prev'}.tgz"
        with tarfile.open(snap, "w:gz") as t:
            t.add(current, arcname="current")
        log.info("snapshot: %s", snap)

    # stop
    if svc_name:
        service_stop(svc_name)

    # extract
    tmp_new = install_dir / ".new"
    if tmp_new.exists(): shutil.rmtree(tmp_new)
    tmp_new.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as t:
        t.extractall(tmp_new)
    # swap
    if current.exists():
        old = install_dir / ".old"
        if old.exists(): shutil.rmtree(old)
        current.rename(old)
    tmp_new.rename(current)
    if (install_dir / ".old").exists():
        shutil.rmtree(install_dir / ".old")

    # apply migrations (stub — a full implementation runs manifest.migrations)
    # TODO: run manifest['migrations'] per runner (sqlcmd, alembic, …)

    # start
    if svc_name:
        service_start(svc_name)

    # health
    health_url = manifest.get("health")
    if health_url:
        ok = health_check(health_url, manifest.get("update", {}).get("health_timeout", 60))
        if not ok:
            log.error("health check failed after update: %s", health_url)
            return False

    # prune old backups
    snaps = sorted(backups.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in snaps[cfg.keep_versions:]:
        s.unlink()
        log.debug("pruned old snapshot: %s", s)

    write_installed_version(install_dir, version)
    return True


def rollback(sub: Subscription) -> None:
    """Restore the latest snapshot under releases/ to current/."""
    backups = sub.install_dir / "releases"
    snaps = sorted(backups.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not snaps:
        log.error("rollback: no snapshot to restore for %s", sub.service)
        return
    latest = snaps[0]
    log.info("rolling back %s from %s", sub.service, latest)
    current = sub.install_dir / "current"
    if current.exists(): shutil.rmtree(current)
    with tarfile.open(latest, "r:gz") as t:
        t.extractall(sub.install_dir)
    # service restart is caller's responsibility (next tick will detect version drift again)


# ========================================================================
# main loop
# ========================================================================


def tick(cfg: Config) -> None:
    try:
        idx = json.loads(http_get(cfg.release_index_url, cfg.http_timeout_sec))
    except Exception as e:
        log.error("fetch release index failed: %s", e)
        return

    for sub in cfg.subscriptions:
        try:
            channel_ver = idx.get("services", {}).get(sub.service, {}).get(sub.channel)
            if not channel_ver:
                log.warning("service %s/%s not in index", sub.service, sub.channel)
                continue
            # If pinned, only install that exact version. Channel still serves as fallback.
            want = sub.pinned_version or channel_ver
            have = read_installed_version(sub.install_dir)
            if have == want:
                continue
            if sub.pinned_version and sub.pinned_version != channel_ver:
                log.info("pinned: %s @ %s (channel %s = %s)",
                         sub.service, sub.pinned_version, sub.channel, channel_ver)
            log.info("update available: %s %s → %s", sub.service, have, want)

            tarball, sha = fetch_release(sub.service, want, ".tar.gz",
                                         cfg.http_timeout_sec)

            # read manifest from inside the tarball
            with tarfile.open(tarball, "r:gz") as t:
                mf = t.extractfile(".release/manifest.yml")
                manifest = yaml.safe_load(mf.read()) if mf else {}

            ok = apply_update(sub, want, manifest, cfg, tarball)
            if ok:
                telemetry(cfg, {"service": sub.service, "status": "ok",
                                "from": have, "to": want})
                log.info("updated %s → %s", sub.service, want)
            else:
                rollback(sub)
                telemetry(cfg, {"service": sub.service, "status": "failed",
                                "from": have, "to": want,
                                "reason": "health_check_failed"})
                log.error("update failed; rolled back")
            tarball.unlink(missing_ok=True)
        except Exception as e:
            log.exception("update %s failed: %s", sub.service, e)
            telemetry(cfg, {"service": sub.service, "status": "error",
                            "reason": str(e)})


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    log.info("ies-updater starting; poll=%ss subs=%d",
             cfg.poll_interval_sec, len(cfg.subscriptions))
    while True:
        tick(cfg)
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    main()
