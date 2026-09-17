from pathlib import Path

path = Path("/opt/firmae/firmae.config")

if not path.exists():
    raise SystemExit(f"FirmAE config not found: {path}")

lines = path.read_text().splitlines()

replacement = r'''
add_partition () {
    local IMAGE_PATH="${1}"
    local LOOP_DEV=""
    local LOOP_BASE=""
    local PART_BASE=""
    local SYSDEV=""
    local DEV_PATH=""
    local MAJMIN=""
    local MAJ=""
    local MIN=""

    losetup -Pf "${IMAGE_PATH}"

    for i in $(seq 1 20)
    do
        LOOP_DEV="$(losetup -j "${IMAGE_PATH}" | cut -d: -f1 | head -1)"
        if [ -n "${LOOP_DEV}" ]; then
            break
        fi
        sleep 1
    done

    if [ -z "${LOOP_DEV}" ]; then
        echo "[FIRMARBITER][firmae] add_partition failed: no loop device for ${IMAGE_PATH}" >&2
        return 1
    fi

    LOOP_BASE="$(basename "${LOOP_DEV}")"
    PART_BASE="${LOOP_BASE}p1"
    SYSDEV="/sys/block/${LOOP_BASE}/${PART_BASE}/dev"
    DEV_PATH="/dev/${PART_BASE}"

    for i in $(seq 1 20)
    do
        if [ -e "${DEV_PATH}" ]; then
            echo "${DEV_PATH}"
            return 0
        fi

        if [ -e "${SYSDEV}" ]; then
            MAJMIN="$(cat "${SYSDEV}")"
            MAJ="${MAJMIN%:*}"
            MIN="${MAJMIN#*:}"

            mknod "${DEV_PATH}" b "${MAJ}" "${MIN}" 2>/dev/null || true
            chmod 660 "${DEV_PATH}" 2>/dev/null || true
            chgrp disk "${DEV_PATH}" 2>/dev/null || true

            if [ -e "${DEV_PATH}" ]; then
                echo "${DEV_PATH}"
                return 0
            fi
        fi

        sleep 1
    done

    kpartx -avs "${LOOP_DEV}" >/dev/null 2>&1 || kpartx -av "${LOOP_DEV}" >/dev/null 2>&1 || true

    for i in $(seq 1 10)
    do
        if [ -e "/dev/mapper/${PART_BASE}" ]; then
            echo "/dev/mapper/${PART_BASE}"
            return 0
        fi
        sleep 1
    done

    echo "[FIRMARBITER][firmae] add_partition failed: no partition node for ${LOOP_DEV}" >&2
    return 1
}
'''.strip().splitlines()

start = None
for i, line in enumerate(lines):
    if line.strip() in ("add_partition () {", "add_partition() {"):
        start = i
        break

if start is None:
    raise SystemExit("Could not find add_partition function")

end = None
for j in range(start + 1, len(lines)):
    if lines[j].strip() == "}":
        end = j
        break

if end is None:
    raise SystemExit("Could not find end of add_partition function")

lines = lines[:start] + replacement + lines[end + 1:]
path.write_text("\n".join(lines) + "\n")

text = path.read_text()
required = [
    'SYSDEV="/sys/block/${LOOP_BASE}/${PART_BASE}/dev"',
    'mknod "${DEV_PATH}" b "${MAJ}" "${MIN}"',
    'kpartx -avs "${LOOP_DEV}"',
]

missing = [r for r in required if r not in text]
if missing:
    raise SystemExit("Patch verification failed. Missing: " + ", ".join(missing))

print("FIRMARBITER patch applied and verified inside /opt/firmae/firmae.config")
