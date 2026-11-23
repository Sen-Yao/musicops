#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source ./scripts/remote_dev_lib.sh

inspect_script="$(cat <<'REMOTE'
set -euo pipefail
docker inspect musicops --format '{"user":{{json .Config.User}},"privileged":{{json .HostConfig.Privileged}},"cap_add":{{json .HostConfig.CapAdd}},"cap_drop":{{json .HostConfig.CapDrop}},"security_opt":{{json .HostConfig.SecurityOpt}},"mounts":{{json .Mounts}}}'
REMOTE
)"

process_script="$(cat <<'REMOTE'
set -euo pipefail
docker exec musicops python -c 'import json, os; lines = open("/proc/self/status", encoding="utf-8").read().splitlines(); cap_eff = int(next(line.split()[1] for line in lines if line.startswith("CapEff:")), 16); print(json.dumps({"euid": os.geteuid(), "egid": os.getegid(), "cap_eff": cap_eff}))'
REMOTE
)"

inspect_json="$(musicops_remote_exec "$inspect_script")"
process_json="$(musicops_remote_exec "$process_script")"

MUSICOPS_RUNTIME_INSPECT_JSON="$inspect_json" \
MUSICOPS_RUNTIME_PROCESS_JSON="$process_json" \
python3 - <<'PY'
import json
import os


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"musicops_runtime_contract_failed:{message}")


inspect = json.loads(os.environ["MUSICOPS_RUNTIME_INSPECT_JSON"])
process = json.loads(os.environ["MUSICOPS_RUNTIME_PROCESS_JSON"])

normalize_cap = lambda value: str(value).upper().removeprefix("CAP_")
cap_add = {normalize_cap(value) for value in inspect.get("cap_add") or []}
cap_drop = {normalize_cap(value) for value in inspect.get("cap_drop") or []}
security_opt = {str(value).lower() for value in inspect.get("security_opt") or []}
mounts = {
    str(item.get("Destination") or ""): {
        "source": str(item.get("Source") or ""),
        "rw": bool(item.get("RW")),
    }
    for item in inspect.get("mounts") or []
}

require(inspect.get("user") == "0:0", "container_user_must_be_0_0")
require(inspect.get("privileged") is False, "privileged_must_be_false")
require(cap_drop == {"ALL"}, "cap_drop_must_be_all")
require(cap_add == {"DAC_OVERRIDE", "FOWNER"}, "cap_add_must_be_dac_override_and_fowner_only")
require("no-new-privileges:true" in security_opt, "no_new_privileges_required")

expected_mounts = {
    "/data": ("/mnt/user/appdata/musicops/data", True),
    "/sessions": ("/mnt/user/appdata/music-manager/scripts", False),
    "/music": ("/mnt/user/media/music", True),
}
for destination, (source, writable) in expected_mounts.items():
    mount = mounts.get(destination) or {}
    require(mount.get("source") == source, f"unexpected_source_for_{destination}")
    require(mount.get("rw") is writable, f"unexpected_write_mode_for_{destination}")

require(
    all(item.get("source") != "/var/run/docker.sock" for item in mounts.values()),
    "docker_socket_must_not_be_mounted",
)

expected_cap_eff = (1 << 1) | (1 << 3)  # DAC_OVERRIDE and FOWNER.
require(process.get("euid") == 0 and process.get("egid") == 0, "effective_user_must_be_root")
require(process.get("cap_eff") == expected_cap_eff, "effective_capabilities_must_match_contract")

print(
    "MusicOps production runtime contract verified: "
    "user=0:0 cap_add=DAC_OVERRIDE,FOWNER privileged=false mounts=scoped"
)
PY
