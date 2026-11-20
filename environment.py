"""Collect hardware and process limits visible to the benchmark."""

import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import subprocess
from hashlib import sha256


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def sysctl(name):
    try:
        return subprocess.check_output(
            ["sysctl", "-n", name], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def cgroup_mounts():
    memberships = {}
    for line in (read("/proc/self/cgroup") or "").splitlines():
        _, controllers, group = line.split(":", 2)
        for controller in controllers.split(","):
            memberships[controller] = PurePosixPath(group)

    for line in (read("/proc/self/mountinfo") or "").splitlines():
        mount, separator, filesystem = line.partition(" - ")
        if not separator:
            continue
        fs_type, _, options = filesystem.split(maxsplit=2)
        if fs_type not in ("cgroup", "cgroup2"):
            continue
        root, mountpoint = (
            re.sub(r"\\([0-7]{3})", lambda item: chr(int(item[1], 8)), value)
            for value in mount.split()[3:5]
        )
        for controller in ([""] if fs_type == "cgroup2" else options.split(",")):
            group = memberships.get(controller)
            if group is None:
                continue
            base = Path(mountpoint)
            try:
                subpath = group.relative_to(root)
            except ValueError:
                # A container's private cgroup namespace can expose "/" for
                # the current group even when the mount root is a host path.
                subpath = group.relative_to("/")
            if ".." in subpath.parts:
                continue
            path = base / subpath
            while path.is_dir():
                yield controller, path
                if path == base:
                    break
                path = path.parent


def number(raw, *, maximum=None):
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value > 0 and (maximum is None or value < maximum) else None


def cgroup_limits():
    quotas, memory = [], []
    for controller, path in cgroup_mounts():
        if controller == "":
            cpu_max = (read(path / "cpu.max") or "").split()
            if len(cpu_max) == 2:
                quota, period = cpu_max
            else:
                quota, period = None, None
        elif controller == "cpu":
            quota, period = read(path / "cpu.cfs_quota_us"), read(path / "cpu.cfs_period_us")
        else:
            quota, period = None, None
        quota, period = number(quota), number(period)
        if quota and period:
            quotas.append(quota / period)

        if controller in ("", "memory"):
            filename = "memory.max" if controller == "" else "memory.limit_in_bytes"
            limit = number(read(path / filename), maximum=2**60)
            if limit is not None:
                memory.append(limit)

    return min(quotas, default=None), min(memory, default=None)


def linux_cpu_models():
    models = set()
    cpuinfo = read("/proc/cpuinfo") or ""
    for entry in cpuinfo.split("\n\n"):
        fields = {}
        for line in entry.splitlines():
            name, sep, value = line.partition(":")
            if sep:
                fields[name.strip()] = value.strip()
        model = fields.get("model name") or fields.get("Hardware")
        if not model and fields.get("CPU implementer") and fields.get("CPU part"):
            model = f"ARM implementer {fields['CPU implementer']}, part {fields['CPU part']}"
        if model:
            models.add(model)
    return sorted(models)


def hardware():
    system = platform.system()
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    logical_cpus = os.cpu_count()
    quota, memory_limit = cgroup_limits() if system == "Linux" else (None, None)
    capacity = min(
        len(affinity) if affinity else (logical_cpus or 1),
        quota if quota is not None else math.inf,
    )

    if system == "Linux":
        memory_match = re.search(r"^MemTotal:\s+(\d+) kB", read("/proc/meminfo") or "", re.M)
        memory = int(memory_match[1]) * 1024 if memory_match else None
        distro_match = re.search(r"^PRETTY_NAME=(.*)$", read("/etc/os-release") or "", re.M)
        os_release = distro_match[1].strip('"') if distro_match else None
        models = linux_cpu_models()
    elif system == "Darwin":
        memory = number(sysctl("hw.memsize"))
        os_release = platform.mac_ver()[0]
        model = sysctl("machdep.cpu.brand_string")
        models = [model] if model else []
    else:
        memory, os_release, models = None, None, []

    return {
        "os": system,
        "os_release": os_release,
        "kernel_release": platform.release(),
        "architecture": platform.machine(),
        "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
        "cpu_models": models,
        "logical_cpus_visible": logical_cpus,
        "cpu_affinity": affinity,
        "cpu_quota_cores": quota,
        "effective_cpu_capacity": capacity,
        "memory_visible_bytes": memory,
        "memory_cgroup_limit_bytes": memory_limit,
        "python_version": platform.python_version(),
    }


def session_fingerprint(salt=None):
    """Tie a resumable run to one kernel boot without storing its ID."""
    if salt is None:
        salt = secrets.token_hex(16)
    system = platform.system()
    if system == "Linux":
        boot_id = read("/proc/sys/kernel/random/boot_id")
    elif system == "Darwin":
        boot_id = sysctl("kern.bootsessionuuid")
    else:
        boot_id = None
    return {
        "salt": salt,
        "digest": sha256((salt + boot_id).encode()).hexdigest() if boot_id else None,
    }
