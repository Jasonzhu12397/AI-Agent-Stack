#!/usr/bin/env bash
set -x

# shellcheck disable=SC1091
source lib/logging.sh
# shellcheck disable=SC1091
source lib/common.sh
# shellcheck disable=SC1091
source lib/network.sh

# Kill and remove the running ironic containers
remove_ironic_containers

# Remove existing pod
if [[ "${CONTAINER_RUNTIME}" = "podman" ]]; then
    for pod in ironic-pod infra-pod; do
        if sudo "${CONTAINER_RUNTIME}" pod exists "${pod}"; then
           sudo "${CONTAINER_RUNTIME}" pod rm "${pod}" -f
        fi
    done
fi

# The upstream teardown playbook removed libvirt domains, volumes and the
# virtualbmc entries. None of them exist here: the bridges are torn down below.

ANSIBLE_FORCE_COLOR=true "${ANSIBLE}-playbook" \
    -e "use_firewalld=${USE_FIREWALLD}" \
    -e "firewall_rule_state=absent" \
    -i vm-setup/inventory.ini \
    -b -v vm-setup/firewall.yml

# There was a bug in this file, it may need to be recreated.
if [[ "${OS}" = "ubuntu" ]]; then
    if [[ "${MANAGE_PRO_BRIDGE}" = "y" ]]; then
        # these are hardcoded in ./ubuntu_bridge_network_configuration.sh
        sudo ip link delete ironic-peer
        sudo ip link delete ironicendpoint
        sudo ip link delete provisioning
    fi
    if [[ "${MANAGE_EXT_BRIDGE}" = "y" ]]; then
        sudo ip link delete external || true
    fi
else
    sudo rm -rf /etc/NetworkManager/conf.d/dnsmasq.conf
    if [[  "${MANAGE_PRO_BRIDGE}" == "y" ]]; then
        sudo ip link del ironic-peer
        sudo ip link del "${BARE_METAL_PROVISIONER_INTERFACE}"
        sudo nmcli con delete provisioning
    fi
    # External net should have been cleaned already at this stage, but we double
    # check as leaving it around causes issues when the host is rebooted
    if [[ "${MANAGE_EXT_BRIDGE}" = "y" ]]; then
        sudo nmcli con delete external || true
    fi
fi

if [[ -n "${XDG_CONFIG_HOME}" ]]; then
    rm -rf "${XDG_CONFIG_HOME}"/cluster-api
else
    rm -rf "${HOME}"/.config/cluster-api
fi
