#!/usr/bin/env python3
"""Offline Docker/containerd storage migration with explicit manual stages.

Stages:
  1. migrate          Stop workloads, copy data, update daemon configs.
  2. verify           Start services, verify paths and preserved Docker objects.
  3. delete-old-data  Delete the old stores after an explicit confirmation.
  4. restore          Restore every configuration file changed by migrate.

Run each stage separately as root. Every stage aborts on a failed precondition or
postcondition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Sequence


DOCKER_SOURCE = Path("/var/lib/docker")
CONTAINERD_SOURCE = Path("/var/lib/containerd")
DOCKER_CONFIG = Path("/etc/docker/daemon.json")
CONTAINERD_CONFIG = Path("/etc/containerd/config.toml")
STATE_DIR = Path("/var/lib/ugp2pnetlib")
STATE_FILE = STATE_DIR / "docker-storage-migration.json"
CONTAINERD_RUNTIME_STATE = "/run/containerd"
DELETE_CONFIRMATION = "DELETE-OLD-DOCKER-DATA"
DATA_MOUNT = Path("/data")

RUNNER_SERVICE = "gitlab-runner"
DOCKER_SERVICE = "docker"
DOCKER_SOCKET = "docker.socket"
CONTAINERD_SERVICE = "containerd"


class StageAbort(RuntimeError):
    """Abort the current stage without continuing to the next operation."""


def log(message: str) -> None:
    print(f"[docker-storage-migration] {message}", flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StageAbort(message)


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    log("run: " + " ".join(command))
    try:
        result = subprocess.run(
            list(command),
            check=False,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise StageAbort(f"command not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise StageAbort(f"command timed out: {' '.join(command)}") from error

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StageAbort(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def command_stdout(command: Sequence[str], *, timeout: int | None = 30) -> str:
    return run(command, timeout=timeout).stdout.strip()


def require_root() -> None:
    require(os.geteuid() == 0, "this script must run as root")


def require_command(name: str) -> None:
    require(shutil.which(name) is not None, f"required command is missing: {name}")


def service_exists(service: str) -> bool:
    result = run(["systemctl", "cat", service], check=False)
    return result.returncode == 0


def service_active(service: str) -> bool:
    result = run(["systemctl", "is-active", "--quiet", service], check=False)
    return result.returncode == 0


def require_active(service: str, expected: bool) -> None:
    require(
        service_active(service) == expected,
        f"service {service} is unexpectedly {'inactive' if expected else 'active'}",
    )


def stop_service(service: str) -> None:
    if not service_exists(service):
        return
    run(["systemctl", "stop", service], check=False)
    require_active(service, False)


def start_service(service: str) -> None:
    require(service_exists(service), f"service does not exist: {service}")
    run(["systemctl", "start", service])
    require_active(service, True)


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state() -> dict[str, Any]:
    require(STATE_FILE.is_file(), f"state file is missing: {STATE_FILE}")
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StageAbort(f"cannot read state file {STATE_FILE}: {error}") from error
    require(isinstance(state, dict), f"invalid state file: {STATE_FILE}")
    require(state.get("version") == 1, f"unsupported state version in {STATE_FILE}")
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = int(time.time())
    atomic_write_json(STATE_FILE, state)


def load_backup_manifest(path: Path) -> list[dict[str, Any]]:
    require(path.is_file(), f"configuration backup manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StageAbort(f"cannot read backup manifest {path}: {error}") from error
    require(isinstance(manifest, list), f"invalid backup manifest: {path}")
    return manifest


def save_backup_manifest(path: Path, manifest: list[dict[str, Any]]) -> None:
    atomic_write_json(path, manifest)


def backup_before_modification(
    path: Path,
    *,
    label: str,
    new_content: str,
    backup_dir: Path,
    manifest_path: Path,
) -> None:
    manifest = load_backup_manifest(manifest_path) if manifest_path.exists() else []
    if any(entry.get("path") == str(path) for entry in manifest):
        return

    require(not path.is_symlink(), f"refusing to modify symlinked config: {path}")
    existed = path.exists()
    if existed:
        require(path.is_file(), f"configuration target is not a regular file: {path}")

    config_backup_dir = backup_dir / "configs"
    config_backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup_path: str | None = None
    original_hash: str | None = None
    original_mode: int | None = None
    if existed:
        original_mode = path.stat().st_mode & 0o777
        original_hash = sha256_file(path)
        destination = config_backup_dir / f"{label}.bak"
        require(not destination.exists(), f"configuration backup already exists: {destination}")
        shutil.copy2(path, destination)
        require(destination.is_file(), f"failed to back up configuration: {path}")
        require(sha256_file(destination) == original_hash, f"configuration backup checksum mismatch: {path}")
        backup_path = str(destination)

    manifest.append(
        {
            "path": str(path),
            "existed": existed,
            "backup_path": backup_path,
            "original_mode": original_mode,
            "original_sha256": original_hash,
            "migrated_sha256": sha256_text(new_content),
        }
    )
    save_backup_manifest(manifest_path, manifest)


def directory_usage(path: Path) -> int:
    require(path.is_dir(), f"directory is missing: {path}")
    output = command_stdout(["du", "-sx", "--block-size=1", str(path)], timeout=None)
    fields = output.split()
    require(len(fields) >= 2, f"unexpected du output for {path}: {output}")
    return int(fields[0])


def docker_ids(command: Sequence[str]) -> list[str]:
    output = command_stdout(list(command))
    return sorted({item for item in output.splitlines() if item})


def docker_inventory() -> dict[str, list[str]]:
    return {
        "running_containers": docker_ids(["docker", "ps", "-q", "--no-trunc"]),
        "containers": docker_ids(["docker", "ps", "-aq", "--no-trunc"]),
        "images": docker_ids(["docker", "images", "-q", "--no-trunc"]),
        "volumes": docker_ids(["docker", "volume", "ls", "-q"]),
    }


def require_data_mount(path: Path) -> None:
    require(path.is_dir(), f"target data root does not exist: {path}")
    require(os.path.ismount(path), f"target data root is not a mount point: {path}")


def require_plain_directory(path: Path, expected_owner: int = 0) -> None:
    require(path.exists(), f"directory is missing: {path}")
    require(path.is_dir(), f"path is not a directory: {path}")
    require(not path.is_symlink(), f"directory must not be a symlink: {path}")
    require(path.stat().st_uid == expected_owner, f"directory must be owned by uid {expected_owner}: {path}")


def top_level_toml_value(document: str, key: str) -> Any:
    parsed = tomllib.loads(document)
    return parsed.get(key)


def set_top_level_toml_value(document: str, key: str, value: str) -> str:
    lines = document.splitlines()
    first_section = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    replacement = f'{key} = "{value}"'
    for index in range(first_section):
        stripped = lines[index].lstrip()
        if re.match(rf"^{re.escape(key)}\s*=", stripped):
            prefix = lines[index][: len(lines[index]) - len(stripped)]
            lines[index] = prefix + replacement
            return "\n".join(lines) + "\n"

    insert_at = first_section
    lines.insert(insert_at, replacement)
    return "\n".join(lines) + "\n"


def prepare_docker_config(target: Path) -> str:
    if DOCKER_CONFIG.exists():
        require(DOCKER_CONFIG.is_file(), f"invalid Docker config path: {DOCKER_CONFIG}")
        try:
            config = json.loads(DOCKER_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise StageAbort(f"cannot parse {DOCKER_CONFIG}: {error}") from error
        require(isinstance(config, dict), f"invalid Docker config root in {DOCKER_CONFIG}")
        current_root = config.get("data-root")
        require(
            current_root in (None, str(DOCKER_SOURCE)),
            f"Docker data-root is already customized to {current_root!r}",
        )
    else:
        config = {}
    config["data-root"] = str(target)
    content = json.dumps(config, indent=2, sort_keys=True) + "\n"
    json.loads(content)
    return content


def prepare_containerd_config(target: Path) -> str:
    if CONTAINERD_CONFIG.exists():
        require(CONTAINERD_CONFIG.is_file(), f"invalid containerd config path: {CONTAINERD_CONFIG}")
        try:
            document = CONTAINERD_CONFIG.read_text(encoding="utf-8")
            current_root = top_level_toml_value(document, "root")
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise StageAbort(f"cannot parse {CONTAINERD_CONFIG}: {error}") from error
        require(
            current_root in (None, str(CONTAINERD_SOURCE)),
            f"containerd root is already customized to {current_root!r}",
        )
    else:
        document = run(["containerd", "config", "default"]).stdout

    document = set_top_level_toml_value(document, "root", str(target))
    document = set_top_level_toml_value(document, "state", CONTAINERD_RUNTIME_STATE)
    parsed = tomllib.loads(document)
    require(parsed.get("root") == str(target), "generated containerd config has the wrong root")
    require(parsed.get("state") == CONTAINERD_RUNTIME_STATE, "generated containerd config has the wrong state")
    return document


def install_mount_dependency(
    service: str,
    data_root: Path,
    *,
    backup_dir: Path,
    manifest_path: Path,
) -> None:
    require(service_exists(service), f"systemd service does not exist: {service}")
    dropin_dir = Path("/etc/systemd/system") / f"{service}.d"
    dropin = dropin_dir / "90-ugp2pnetlib-data-mount.conf"
    content = f"[Unit]\nRequiresMountsFor={data_root}\n"
    if dropin.exists():
        require(
            dropin.read_text(encoding="utf-8") == content,
            f"existing systemd drop-in has different content: {dropin}",
        )
        return
    backup_before_modification(
        dropin,
        label=f"systemd-{service}-data-mount",
        new_content=content,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
    )
    atomic_write(dropin, content, 0o644)
    require(dropin.is_file(), f"failed to create systemd drop-in: {dropin}")


def copy_store(source: Path, target: Path, label: str) -> None:
    require_plain_directory(source)
    require(target.is_dir(), f"target directory is missing: {target}")
    require(source.resolve() != target.resolve(), f"{label} source and target must differ")
    require(not target.is_symlink(), f"{label} target must not be a symlink")

    dry_run = run(
        [
            "rsync",
            "-aHAXS",
            "--numeric-ids",
            "--one-file-system",
            "--dry-run",
            f"{source}/",
            f"{target}/",
        ],
        timeout=None,
    )
    require(dry_run.returncode == 0, f"{label} rsync dry-run failed")

    run(
        [
            "rsync",
            "-aHAXS",
            "--numeric-ids",
            "--one-file-system",
            f"{source}/",
            f"{target}/",
        ],
        timeout=None,
    )
    require(target.is_dir(), f"{label} target disappeared after copy")

    source_size = directory_usage(source)
    target_size = directory_usage(target)
    require(
        target_size >= int(source_size * 0.98),
        f"{label} copy is incomplete: source={source_size}, target={target_size}",
    )


def stop_docker_stack(*, require_docker_active: bool = True) -> None:
    if service_exists(RUNNER_SERVICE):
        stop_service(RUNNER_SERVICE)
    else:
        log(f"service not installed, skipping: {RUNNER_SERVICE}")

    docker_active = service_active(DOCKER_SERVICE)
    if require_docker_active:
        require(docker_active, "Docker must be active before migration inventory")
    if docker_active:
        running = docker_ids(["docker", "ps", "-q"])
        if running:
            run(["docker", "stop", *running], timeout=None)
            require(not docker_ids(["docker", "ps", "-q"]), "some Docker containers are still running")

    stop_service(DOCKER_SOCKET)
    stop_service(DOCKER_SERVICE)
    stop_service(CONTAINERD_SERVICE)

    for process in ("dockerd", "containerd", "containerd-shim"):
        result = run(["pgrep", "-a", process], check=False)
        require(result.returncode != 0, f"process still running during migration: {process}")


def start_docker_stack() -> None:
    start_service(CONTAINERD_SERVICE)
    start_service(DOCKER_SOCKET)
    start_service(DOCKER_SERVICE)
    require(run(["docker", "info"], check=False).returncode == 0, "Docker daemon is not ready")


def verify_configs(docker_target: Path, containerd_target: Path) -> None:
    docker_root = command_stdout(["docker", "info", "--format", "{{.DockerRootDir}}"])
    require(docker_root == str(docker_target), f"DockerRootDir mismatch: expected {docker_target}, got {docker_root}")

    dump = command_stdout(["containerd", "config", "dump"], timeout=None)
    try:
        parsed = tomllib.loads(dump)
    except tomllib.TOMLDecodeError as error:
        raise StageAbort(f"cannot parse effective containerd config: {error}") from error
    require(
        parsed.get("root") == str(containerd_target),
        f"containerd root mismatch: expected {containerd_target}, got {parsed.get('root')!r}",
    )
    require(
        parsed.get("state") == CONTAINERD_RUNTIME_STATE,
        f"containerd state mismatch: expected {CONTAINERD_RUNTIME_STATE}, got {parsed.get('state')!r}",
    )


def verify_inventory(
    state: dict[str, Any],
    *,
    allowed_running_states: Sequence[Sequence[str]] = ((),),
) -> None:
    before = state["inventory"]
    current = docker_inventory()
    require(
        current["containers"] == before["containers"],
        "Docker container IDs changed after migration",
    )
    require(current["images"] == before["images"], "Docker image IDs changed after migration")
    require(current["volumes"] == before["volumes"], "Docker volume IDs changed after migration")
    normalized_states = {tuple(sorted(state_values)) for state_values in allowed_running_states}
    require(
        tuple(current["running_containers"]) in normalized_states,
        "running Docker containers do not match the expected state",
    )


def restore_running_workloads(state: dict[str, Any]) -> None:
    original_running = state["inventory"]["running_containers"]
    if original_running:
        run(["docker", "start", *original_running], timeout=None)
        require(
            docker_ids(["docker", "ps", "-q", "--no-trunc"]) == sorted(original_running),
            "failed to restore the original running Docker containers",
        )

    if state["services"]["gitlab_runner_active"]:
        start_service(RUNNER_SERVICE)


def stage_migrate(args: argparse.Namespace) -> None:
    data_root = args.data_root.resolve()
    docker_target = data_root / "docker"
    containerd_target = data_root / "containerd"

    require(not STATE_FILE.exists(), f"migration state already exists: {STATE_FILE}")
    require_command("du")
    require_command("rsync")
    require_command("docker")
    require_command("containerd")
    require_command("findmnt")
    require_command("pgrep")
    require_command("systemctl")
    require_data_mount(data_root)

    require_plain_directory(DOCKER_SOURCE)
    require_plain_directory(CONTAINERD_SOURCE)
    require_plain_directory(DOCKER_SOURCE.parent)
    require_plain_directory(CONTAINERD_SOURCE.parent)
    require(service_active(DOCKER_SERVICE), "Docker service must be active before migration")
    require(service_active(CONTAINERD_SERVICE), "containerd service must be active before migration")

    docker_root = command_stdout(["docker", "info", "--format", "{{.DockerRootDir}}"])
    require(docker_root == str(DOCKER_SOURCE), f"unexpected DockerRootDir before migration: {docker_root}")

    sources_size = directory_usage(DOCKER_SOURCE) + directory_usage(CONTAINERD_SOURCE)
    free_space = shutil.disk_usage(data_root).free
    require(
        free_space >= sources_size + 5 * 1024**3,
        f"insufficient target space: free={free_space}, sources={sources_size}, safety=5GiB",
    )

    require(not docker_target.exists(), f"target already exists: {docker_target}")
    require(not containerd_target.exists(), f"target already exists: {containerd_target}")

    runner_was_active = service_active(RUNNER_SERVICE)
    stop_service(RUNNER_SERVICE)
    inventory = docker_inventory()
    service_state = {
        "docker_active": service_active(DOCKER_SERVICE),
        "docker_socket_active": service_active(DOCKER_SOCKET),
        "containerd_active": service_active(CONTAINERD_SERVICE),
        "gitlab_runner_active": runner_was_active,
    }

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_root / "ugp2pnetlib-docker-storage-migration" / f"backups-{timestamp}"
    require(not backup_dir.exists(), f"backup directory already exists: {backup_dir}")
    backup_dir.mkdir(parents=True, mode=0o700)
    require(backup_dir.is_dir(), f"failed to create backup directory: {backup_dir}")
    manifest_path = backup_dir / "config-backup-manifest.json"
    save_backup_manifest(manifest_path, [])

    docker_content = prepare_docker_config(docker_target)
    containerd_content = prepare_containerd_config(containerd_target)

    state: dict[str, Any] = {
        "version": 1,
        "phase": "migrate_in_progress",
        "started_at": int(time.time()),
        "data_root": str(data_root),
        "docker_source": str(DOCKER_SOURCE),
        "docker_target": str(docker_target),
        "containerd_source": str(CONTAINERD_SOURCE),
        "containerd_target": str(containerd_target),
        "backup_dir": str(backup_dir),
        "config_backup_manifest": str(manifest_path),
        "services": service_state,
        "inventory": inventory,
        "source_sizes": {
            "docker": directory_usage(DOCKER_SOURCE),
            "containerd": directory_usage(CONTAINERD_SOURCE),
        },
    }
    save_state(state)

    stop_docker_stack()

    docker_target.mkdir(mode=0o710)
    containerd_target.mkdir(mode=0o700)
    require(docker_target.is_dir(), f"failed to create target: {docker_target}")
    require(containerd_target.is_dir(), f"failed to create target: {containerd_target}")

    copy_store(DOCKER_SOURCE, docker_target, "Docker")
    copy_store(CONTAINERD_SOURCE, containerd_target, "containerd")

    install_mount_dependency(
        DOCKER_SERVICE,
        data_root,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
    )
    install_mount_dependency(
        CONTAINERD_SERVICE,
        data_root,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
    )
    if service_exists(RUNNER_SERVICE):
        install_mount_dependency(
            RUNNER_SERVICE,
            data_root,
            backup_dir=backup_dir,
            manifest_path=manifest_path,
        )

    docker_previous = DOCKER_CONFIG.read_text(encoding="utf-8") if DOCKER_CONFIG.exists() else None
    containerd_previous = CONTAINERD_CONFIG.read_text(encoding="utf-8") if CONTAINERD_CONFIG.exists() else None
    try:
        backup_before_modification(
            DOCKER_CONFIG,
            label="docker-daemon",
            new_content=docker_content,
            backup_dir=backup_dir,
            manifest_path=manifest_path,
        )
        backup_before_modification(
            CONTAINERD_CONFIG,
            label="containerd",
            new_content=containerd_content,
            backup_dir=backup_dir,
            manifest_path=manifest_path,
        )
        atomic_write(DOCKER_CONFIG, docker_content, 0o600)
        atomic_write(CONTAINERD_CONFIG, containerd_content, 0o600)
        require(json.loads(DOCKER_CONFIG.read_text(encoding="utf-8"))["data-root"] == str(docker_target),
                "Docker config verification failed")
        require(
            tomllib.loads(CONTAINERD_CONFIG.read_text(encoding="utf-8")).get("root")
            == str(containerd_target),
            "containerd config verification failed",
        )
    except Exception:
        if docker_previous is None:
            DOCKER_CONFIG.unlink(missing_ok=True)
        else:
            atomic_write(DOCKER_CONFIG, docker_previous, 0o600)
        if containerd_previous is None:
            CONTAINERD_CONFIG.unlink(missing_ok=True)
        else:
            atomic_write(CONTAINERD_CONFIG, containerd_previous, 0o600)
        raise

    run(["systemctl", "daemon-reload"])
    state["phase"] = "migrate_complete"
    state["target_sizes"] = {
        "docker": directory_usage(docker_target),
        "containerd": directory_usage(containerd_target),
    }
    save_state(state)
    log("migrate complete; Docker and containerd remain stopped")


def stage_verify(args: argparse.Namespace) -> None:
    state = load_state()
    require(state.get("phase") == "migrate_complete", "verify requires phase=migrate_complete")
    require(state.get("data_root") == str(args.data_root.resolve()), "data root differs from migration state")

    docker_target = Path(state["docker_target"])
    containerd_target = Path(state["containerd_target"])
    require(docker_target.is_dir(), f"missing Docker target: {docker_target}")
    require(containerd_target.is_dir(), f"missing containerd target: {containerd_target}")
    require(not service_active(RUNNER_SERVICE), "GitLab Runner must be stopped before verify starts")

    start_docker_stack()
    verify_configs(docker_target, containerd_target)
    verify_inventory(
        state,
        allowed_running_states=((), state["inventory"]["running_containers"]),
    )

    source_sizes = state["source_sizes"]
    require(
        directory_usage(docker_target) >= int(source_sizes["docker"] * 0.98),
        "Docker target size is materially smaller than the source",
    )
    require(
        directory_usage(containerd_target) >= int(source_sizes["containerd"] * 0.98),
        "containerd target size is materially smaller than the source",
    )

    state["phase"] = "verify_complete"
    state["verified_at"] = int(time.time())
    state["verified_inventory"] = docker_inventory()
    restore_running_workloads(state)
    save_state(state)
    log("verify complete; old stores are still present")


def is_managed_config_path(path: Path) -> bool:
    if path in (DOCKER_CONFIG, CONTAINERD_CONFIG):
        return True
    return re.fullmatch(
        r"/etc/systemd/system/(docker|containerd|gitlab-runner)\.service\.d/"
        r"90-ugp2pnetlib-data-mount\.conf",
        str(path),
    ) is not None


def validate_backup_manifest(manifest: Sequence[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for entry in manifest:
        require(isinstance(entry, dict), "invalid configuration backup entry")
        path_text = entry.get("path")
        require(isinstance(path_text, str), "configuration backup entry has no path")
        require(path_text not in seen, f"duplicate configuration backup entry: {path_text}")
        seen.add(path_text)

        path = Path(path_text)
        require(is_managed_config_path(path), f"refusing to restore unmanaged path: {path}")
        require(not path.is_symlink(), f"refusing to restore symlinked path: {path}")
        existed = entry.get("existed")
        require(isinstance(existed, bool), f"invalid existed value for {path}")

        migrated_hash = entry.get("migrated_sha256")
        require(isinstance(migrated_hash, str), f"missing migrated hash for {path}")
        if path.exists():
            require(path.is_file(), f"restore target is not a regular file: {path}")
            current_hash = sha256_file(path)
            if current_hash != migrated_hash:
                require(
                    existed and current_hash == entry.get("original_sha256"),
                    f"configuration changed after migrate; refusing to overwrite: {path}",
                )

        if existed:
            backup_path_text = entry.get("backup_path")
            original_hash = entry.get("original_sha256")
            original_mode = entry.get("original_mode")
            require(isinstance(backup_path_text, str), f"missing backup path for {path}")
            require(isinstance(original_hash, str), f"missing original hash for {path}")
            require(isinstance(original_mode, int), f"missing original mode for {path}")
            backup_path = Path(backup_path_text)
            require(backup_path.is_file(), f"backup file is missing: {backup_path}")
            require(sha256_file(backup_path) == original_hash, f"backup checksum mismatch: {backup_path}")


def restore_config_file(entry: dict[str, Any]) -> None:
    path = Path(entry["path"])
    if entry["existed"]:
        backup_path = Path(entry["backup_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(backup_path, temporary_path)
            os.chmod(temporary_path, int(entry["original_mode"]))
            require(sha256_file(temporary_path) == entry["original_sha256"],
                    f"restored temporary checksum mismatch: {path}")
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)
    require(
        path.exists() == bool(entry["existed"]),
        f"configuration restore postcondition failed: {path}",
    )
    if entry["existed"]:
        require(sha256_file(path) == entry["original_sha256"], f"configuration restore checksum failed: {path}")


def stage_restore(args: argparse.Namespace) -> None:
    state = load_state()
    phase = state.get("phase")
    require(
        phase in ("migrate_in_progress", "migrate_complete", "verify_complete"),
        f"restore is not valid for phase={phase!r}",
    )
    require(DOCKER_SOURCE.is_dir(), f"original Docker data is missing: {DOCKER_SOURCE}")
    require(CONTAINERD_SOURCE.is_dir(), f"original containerd data is missing: {CONTAINERD_SOURCE}")

    manifest_path = Path(state["config_backup_manifest"])
    manifest = load_backup_manifest(manifest_path)
    validate_backup_manifest(manifest)

    stop_docker_stack(require_docker_active=False)
    for entry in reversed(manifest):
        restore_config_file(entry)
    run(["systemctl", "daemon-reload"])

    start_docker_stack()
    verify_configs(DOCKER_SOURCE, CONTAINERD_SOURCE)
    verify_inventory(state)
    restore_running_workloads(state)

    state["phase"] = "restore_complete"
    state["restored_at"] = int(time.time())
    archive_path = STATE_DIR / f"docker-storage-migration.restored-{int(time.time())}.json"
    atomic_write_json(archive_path, state)
    STATE_FILE.unlink()
    log(f"restore complete; migration state archived at {archive_path}")


def require_no_submounts(path: Path) -> None:
    result = run(["findmnt", "-R", "-n", "-o", "TARGET", str(path)], check=False)
    if result.returncode == 0 and result.stdout.strip():
        raise StageAbort(f"nested mounts exist below {path}:\n{result.stdout.strip()}")


def stage_delete_old_data(args: argparse.Namespace) -> None:
    require(
        args.confirm == DELETE_CONFIRMATION,
        f"delete-old-data requires --confirm {DELETE_CONFIRMATION}",
    )
    state = load_state()
    require(state.get("phase") == "verify_complete", "delete requires phase=verify_complete")
    require(state.get("data_root") == str(args.data_root.resolve()), "data root differs from migration state")

    docker_target = Path(state["docker_target"])
    containerd_target = Path(state["containerd_target"])
    require(state["docker_source"] == str(DOCKER_SOURCE), "state has an unexpected Docker source")
    require(state["containerd_source"] == str(CONTAINERD_SOURCE), "state has an unexpected containerd source")
    require(DOCKER_SOURCE.resolve() != docker_target.resolve(), "Docker source and target must differ")
    require(CONTAINERD_SOURCE.resolve() != containerd_target.resolve(), "containerd source and target must differ")

    require_active(DOCKER_SERVICE, True)
    require_active(CONTAINERD_SERVICE, True)
    verify_configs(docker_target, containerd_target)

    stop_service(RUNNER_SERVICE)
    running = docker_ids(["docker", "ps", "-q"])
    if running:
        run(["docker", "stop", *running], timeout=None)
    verify_inventory(
        {
            **state,
            "inventory": state["verified_inventory"],
        },
        allowed_running_states=((),),
    )

    stop_docker_stack()
    require(not service_active(DOCKER_SERVICE), "Docker is still active before old-data deletion")
    require(not service_active(CONTAINERD_SERVICE), "containerd is still active before old-data deletion")
    require_no_submounts(DOCKER_SOURCE)
    require_no_submounts(CONTAINERD_SOURCE)

    docker_mode = DOCKER_SOURCE.stat().st_mode & 0o777
    containerd_mode = CONTAINERD_SOURCE.stat().st_mode & 0o777
    shutil.rmtree(DOCKER_SOURCE)
    require(not DOCKER_SOURCE.exists(), f"failed to delete old Docker data: {DOCKER_SOURCE}")
    shutil.rmtree(CONTAINERD_SOURCE)
    require(not CONTAINERD_SOURCE.exists(), f"failed to delete old containerd data: {CONTAINERD_SOURCE}")

    DOCKER_SOURCE.mkdir(mode=docker_mode)
    CONTAINERD_SOURCE.mkdir(mode=containerd_mode)
    require(DOCKER_SOURCE.is_dir(), f"failed to recreate empty directory: {DOCKER_SOURCE}")
    require(CONTAINERD_SOURCE.is_dir(), f"failed to recreate empty directory: {CONTAINERD_SOURCE}")

    start_docker_stack()
    verify_configs(docker_target, containerd_target)
    verify_inventory(
        {
            **state,
            "inventory": state["verified_inventory"],
        },
        allowed_running_states=((),),
    )

    state["phase"] = "delete_complete"
    state["old_data_deleted_at"] = int(time.time())
    save_state(state)
    restore_running_workloads(state)
    log("delete-old-data complete")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("migrate", "verify", "delete-old-data", "restore"))
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DATA_MOUNT,
        help="mounted destination filesystem (default: /data)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"delete stage confirmation; must equal {DELETE_CONFIRMATION}",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_root()
        if args.stage == "migrate":
            stage_migrate(args)
        elif args.stage == "verify":
            stage_verify(args)
        elif args.stage == "restore":
            stage_restore(args)
        else:
            stage_delete_old_data(args)
    except StageAbort as error:
        print(f"[docker-storage-migration] ABORT {args.stage}: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"[docker-storage-migration] ABORT {args.stage}: filesystem error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
