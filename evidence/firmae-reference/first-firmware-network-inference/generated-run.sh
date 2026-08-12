#!/bin/bash

set -e
set -u

ARCHEND=armel
IID=1

if [ -e ./firmae.config ]; then
    source ./firmae.config
elif [ -e ../firmae.config ]; then
    source ../firmae.config
elif [ -e ../../firmae.config ]; then
    source ../../firmae.config
else
    echo "Error: Could not find 'firmae.config'!"
    exit 1
fi

RUN_MODE=`basename ${0}`

IMAGE=`get_fs ${IID}`
if (echo ${ARCHEND} | grep -q "mips" && echo ${RUN_MODE} | grep -q "debug"); then
    KERNEL=`get_kernel ${ARCHEND} true`
else
    KERNEL=`get_kernel ${ARCHEND} false`
fi

if (echo ${RUN_MODE} | grep -q "analyze"); then
    QEMU_DEBUG="user_debug=31 firmadyne.syscall=32"
else
    QEMU_DEBUG="user_debug=0 firmadyne.syscall=1"
fi

if (echo ${RUN_MODE} | grep -q "boot"); then
    QEMU_BOOT="-s -S"
else
    QEMU_BOOT=""
fi

QEMU=`get_qemu ${ARCHEND}`
QEMU_MACHINE=`get_qemu_machine ${ARCHEND}`
QEMU_ROOTFS=`get_qemu_disk ${ARCHEND}`
WORK_DIR=`get_scratch ${IID}`

DEVICE=`add_partition "${WORK_DIR}/image.raw"`
mount ${DEVICE} ${WORK_DIR}/image > /dev/null

echo "normal" > ${WORK_DIR}/image/firmadyne/network_type
echo "br0" > ${WORK_DIR}/image/firmadyne/net_bridge
echo "eth0" > ${WORK_DIR}/image/firmadyne/net_interface

echo "#!/firmadyne/sh" > ${WORK_DIR}/image/firmadyne/debug.sh
if (echo ${RUN_MODE} | grep -q "debug"); then
    echo "while (true); do /firmadyne/busybox nc -lp 31337 -e /firmadyne/sh; done &" >> ${WORK_DIR}/image/firmadyne/debug.sh
    echo "/firmadyne/busybox telnetd -p 31338 -l /firmadyne/sh" >> ${WORK_DIR}/image/firmadyne/debug.sh
fi
chmod a+x ${WORK_DIR}/image/firmadyne/debug.sh

sleep 1
sync
umount ${WORK_DIR}/image > /dev/null
del_partition ${DEVICE:0:$((${#DEVICE}-2))}


TAPDEV_0=tap${IID}_0
HOSTNETDEV_0=${TAPDEV_0}
echo "Creating TAP device ${TAPDEV_0}..."
sudo tunctl -t ${TAPDEV_0} -u ${USER}


echo "Initializing VLAN..."
HOSTNETDEV_0=${TAPDEV_0}.1
sudo ip link add link ${TAPDEV_0} name ${HOSTNETDEV_0} type vlan id 1
sudo ip link set ${TAPDEV_0} up


echo "Bringing up TAP device..."
sudo ip link set ${HOSTNETDEV_0} up
sudo ip addr add 192.168.0.2/24 dev ${HOSTNETDEV_0}


echo -n "Starting emulation of firmware... "
QEMU_AUDIO_DRV=none ${QEMU} ${QEMU_BOOT} -m 1024 -M ${QEMU_MACHINE} -kernel ${KERNEL} \
    -drive if=none,file=${IMAGE},format=raw,id=rootfs -device virtio-blk-device,drive=rootfs -append "root=${QEMU_ROOTFS} console=ttyS0 nandsim.parts=64,64,64,64,64,64,64,64,64,64 rdinit=/firmadyne/preInit.sh rw debug ignore_loglevel print-fatal-signals=1 FIRMAE_NET=${FIRMAE_NET} FIRMAE_NVRAM=${FIRMAE_NVRAM} FIRMAE_KERNEL=${FIRMAE_KERNEL} FIRMAE_ETC=${FIRMAE_ETC} ${QEMU_DEBUG}" \
    -serial file:${WORK_DIR}/qemu.final.serial.log \
    -serial unix:/tmp/qemu.${IID}.S1,server,nowait \
    -monitor unix:/tmp/qemu.${IID},server,nowait \
    -display none \
    -device virtio-net-device,netdev=net0 -netdev tap,id=net0,ifname=${TAPDEV_0},script=no | true


echo "Bringing down TAP device..."
sudo ip link set ${TAPDEV_0} down


echo "Removing VLAN..."
sudo ip link delete ${HOSTNETDEV_0}


echo "Deleting TAP device ${TAPDEV_0}..."
sudo tunctl -d ${TAPDEV_0}


echo "Done!"
