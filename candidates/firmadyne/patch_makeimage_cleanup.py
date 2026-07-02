from pathlib import Path

path = Path("/opt/firmadyne/scripts/makeImage.sh")
text = path.read_text()

old = '''echo "----Deleting device mapper----"
kpartx -d "${IMAGE}"
losetup -d "${DEVICE}" &>/dev/null
dmsetup remove $(basename "$DEVICE") &>/dev/null
'''

new = '''echo "----Deleting device mapper----"

# VERITAS compatibility fix:
# DEVICE is a mapper partition such as /dev/mapper/loop18p1.
# losetup requires the parent loop device, such as /dev/loop18.
MAPPER_NAME="$(basename "${DEVICE}")"
LOOP_DEVICE="$(losetup -j "${IMAGE}" | awk -F: 'NR == 1 {print $1}')"

kpartx -d "${IMAGE}" || true

if [ -n "${LOOP_DEVICE}" ]; then
    losetup -d "${LOOP_DEVICE}" || true
fi

# kpartx may already have removed this mapping.
dmsetup remove "${MAPPER_NAME}" >/dev/null 2>&1 || true
'''

if new in text:
    print("makeImage.sh compatibility patch is already applied.")
elif old not in text:
    raise SystemExit(
        "ERROR: Expected makeImage.sh cleanup block was not found. "
        "No upstream file was modified."
    )
else:
    path.write_text(text.replace(old, new, 1))
    print("Patched makeImage.sh cleanup successfully.")
