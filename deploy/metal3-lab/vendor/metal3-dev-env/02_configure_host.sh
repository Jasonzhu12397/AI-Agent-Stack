#!/usr/bin/env bash
set -eux

# shellcheck disable=SC1091
source lib/logging.sh
# shellcheck disable=SC1091
source lib/common.sh
# shellcheck disable=SC1091
source lib/network.sh
# shellcheck disable=SC1091
source lib/releases.sh
# pre-pull node and container images
# shellcheck disable=SC1091
source lib/image_prepull.sh

# cleanup ci config file if it exists from earlier run
rm -f "${CI_CONFIG_FILE}"

# add registry config for skopeo
cat > "${WORKING_DIR}/registries.conf" <<EOF
unqualified-search-registries = []

[[registry]]
location = "${REGISTRY}"
insecure = true
EOF

# Add usr/local/go/bin to the PATH environment variable
GOBINARY="${GOBINARY:-/usr/local/go/bin}"
if [[ ! "${PATH}" =~ .*(:|^)(${GOBINARY})(:|$).* ]]; then
    echo "export PATH=${PATH}:${GOBINARY}" >> ~/.bashrc
    export PATH=${PATH}:${GOBINARY}
fi


# Clean-up any old ironic containers
remove_ironic_containers

# Clean-up existing pod, if podman
case "${CONTAINER_RUNTIME}" in
    podman)
        for pod in ironic-pod infra-pod; do
            if  sudo "${CONTAINER_RUNTIME}" pod exists "${pod}" ; then
                sudo "${CONTAINER_RUNTIME}" pod rm "${pod}" -f
            fi
            sudo "${CONTAINER_RUNTIME}" pod create -n "${pod}"
        done
        ;;
    *)
        ;;
esac


# Clean, copy and extract local IPA
if [[ "${USE_LOCAL_IPA}" == "true" ]]; then
    sudo rm -f  "${IRONIC_DATA_DIR}/html/images/ironic-python-agent*"
    sudo cp "${LOCAL_IPA_PATH}/ironic-python-agent.tar" "${IRONIC_DATA_DIR}/html/images"
    sudo tar --extract --file "${IRONIC_DATA_DIR}/html/images/ironic-python-agent.tar" \
        --directory "${IRONIC_DATA_DIR}/html/images"
    # avoid duplicating the same process in BMO run_local script
    export USE_LOCAL_IPA="false"
fi




# The BMHs describe hardware that already exists: no VM, veth, domain or
# inventory file is generated here. tools/metal3_deploy.py renders the BMHs.


# When running kind ironic is running on the host, hence we need ironicendpoint
# interface on the host.
configure_kind_network() {
    if [[ ! -e /etc/NetworkManager/system-connections/provisioning.nmconnection ]]; then
        # Don't define an IP address to the bridge, put both into ironicendpoint.
        # ironicendpoint needs 2 IP-addresses for keepalived.
        sudo tee -a /etc/NetworkManager/system-connections/provisioning.nmconnection <<EOF
[connection]
id=provisioning
type=bridge
interface-name=provisioning

[bridge]
stp=false
EOF
    fi
    sudo chmod 600 /etc/NetworkManager/system-connections/provisioning.nmconnection
    sudo nmcli con load /etc/NetworkManager/system-connections/provisioning.nmconnection
    sudo nmcli con up provisioning

    sudo ip link add ironicendpoint type veth peer name ironic-peer
    sudo ip link set ironic-peer master provisioning

    if [[ "${BARE_METAL_PROVISIONER_SUBNET_IPV6_ONLY}" = "true" ]]; then
        sudo ip -6 addr add dev ironicendpoint "${BARE_METAL_PROVISIONER_IP}"/"${BARE_METAL_PROVISIONER_CIDR}"
        sudo ip -6 addr add dev ironicendpoint "${CLUSTER_BARE_METAL_PROVISIONER_IP}"/32
    else
        sudo ip addr add dev ironicendpoint "${BARE_METAL_PROVISIONER_IP}"/"${BARE_METAL_PROVISIONER_CIDR}"
        sudo ip addr add dev ironicendpoint "${CLUSTER_BARE_METAL_PROVISIONER_IP}"/32
    fi
    sudo ip link set ironicendpoint up
    sudo ip link set ironic-peer up
}


# This lab runs on Ubuntu only (the runner refuses any other host). The
# provisioning and external bridges are plain ip/brctl bridges on real NICs,
# so there is no libvirt network to restart and no driver policy to patch.
# shellcheck disable=SC1091
source ubuntu_bridge_network_configuration.sh

ANSIBLE_FORCE_COLOR=true "${ANSIBLE}-playbook" \
    -e "use_firewalld=${USE_FIREWALLD}" \
    -i vm-setup/inventory.ini \
    -b vm-setup/firewall.yml

# The ansible firewalld module does not always move the interface into the
# zone, so do it explicitly. The zone is the host's own public zone: there
# is no libvirt zone on a machine that runs no virtual machines.
if [[ "${USE_FIREWALLD}" == "True" ]]; then
    sudo firewall-cmd --zone=public --change-interface=provisioning
    sudo firewall-cmd --zone=public --change-interface=external
fi

# Need to route traffic from the provisioning host.
if [[ -n "${EXT_IF}" ]]; then
    sudo iptables -t nat -A POSTROUTING --out-interface "${EXT_IF}" -j MASQUERADE
    sudo iptables -A FORWARD --in-interface external -j ACCEPT
fi

# Local registry for images
reg_state=$(sudo "${CONTAINER_RUNTIME}" inspect registry --format "{{.State.Status}}" || echo "error")

# ubuntu_install_requirements.sh script restarts docker daemon which causes local registry container to be in exited state.
if [[ "${reg_state}" == "exited" ]]; then
    sudo "${CONTAINER_RUNTIME}" start registry
elif [[ "${reg_state}" != "running" ]]; then
    sudo "${CONTAINER_RUNTIME}" rm registry -f || true
    sudo "${CONTAINER_RUNTIME}" run -d -p "${REGISTRY}":5000 --name registry "${DOCKER_REGISTRY_IMAGE}"
fi
sleep 5

detect_mismatch()
{
    local LOCAL_IMAGE="$1"
    local REPO_PATH="$2"
    if [[ -z "${LOCAL_IMAGE}" ]] || [[ "${LOCAL_IMAGE}" == "${REPO_PATH}" ]]; then
        echo "Local image: ${LOCAL_IMAGE} and repo path: ${REPO_PATH} are matching!"
    else
        echo "There is a mismatch between LOCAL_IMAGE:${LOCAL_IMAGE} and IMAGE_PATH:${REPO_PATH}"
        echo "The mismatch could cause difficult to debug errors, PLEASE FIX!"
        exit 1
    fi

}
# Clone all needed repositories (CAPI, CAPM3, BMO, IPAM)
# The repos cloned under M3PATH has two functions, building images and
# providing manifest generation functionality
mkdir -p "${M3PATH}"
# When building local images make sure FORCE_REPO_UPDATE is set to 'false'
# otherwise clone_repo will overwrite the content of whatever is at the end
# of the path
detect_mismatch "${BMO_LOCAL_IMAGE:-}" "${BMOPATH}"
clone_repo "${BMOREPO}" "${BMOBRANCH}" "${BMOPATH}" "${BMOCOMMIT}"

detect_mismatch "${CAPM3_LOCAL_IMAGE:-}" "${CAPM3PATH}"
clone_repo "${CAPM3REPO}" "${CAPM3BRANCH}" "${CAPM3PATH}" "${CAPM3COMMIT}"

detect_mismatch "${IPAM_LOCAL_IMAGE:-}" "${IPAMPATH}"
clone_repo "${IPAMREPO}" "${IPAMBRANCH}" "${IPAMPATH}" "${IPAMCOMMIT}"

detect_mismatch "${CAPI_LOCAL_IMAGE:-}" "${CAPIPATH}"
clone_repo "${CAPIREPO}" "${CAPIBRANCH}" "${CAPIPATH}" "${CAPICOMMIT}"

detect_mismatch "${IRSO_LOCAL_IMAGE:-}" "${IRSOPATH}"
clone_repo "${IRSOREPO}" "${IRSOBRANCH}" "${IRSOPATH}" "${IRSOCOMMIT}"

# Ironic source is not needed unless the images are built locally
# If the repo path does not match with the IMAGE location that means the image
# is built from a repo that is not under dev-env's control thus there is no
# need to clone the repo.
# There is no need to keep the PATH and the IMAGE vars in sync as there
# is no other use of the path variable than cloning

if [[ "${IRONIC_LOCAL_IMAGE:-}" == "${IRONIC_IMAGE_PATH}" ]]; then
    clone_repo "${IRONIC_IMAGE_REPO}" "${IRONIC_IMAGE_BRANCH}" "${IRONIC_IMAGE_PATH}" "${IRONIC_IMAGE_COMMIT}"
fi

# Support for building local images
for IMAGE_VAR in $(env | grep "_LOCAL_IMAGE=" | grep -o "^[^=]*"); do
    IMAGE_REPO_PATH="${!IMAGE_VAR}"
    cd "${IMAGE_REPO_PATH}" || exit

    IMAGE_DIR_NAME=$(basename "${IMAGE_REPO_PATH}")
    IMAGE_URL="${REGISTRY}/localimages/${IMAGE_DIR_NAME}"
    export "${IMAGE_VAR}"="${IMAGE_URL}"

    IMAGE_GIT_HASH="$(git rev-parse --short HEAD || echo "nogit")"
    # [year]_[day]_[hour][minute]
    IMAGE_DATE="$(date -u +%y_%j_%H%M)"

    # Support building ironic-image from source
    if [[ "${IMAGE_VAR/_LOCAL_IMAGE}" == "IRONIC" ]] && [[ ${IRONIC_FROM_SOURCE:-} == "true" ]]; then
        # NOTE(rpittau): to customize the source origin we need to copy the source code we
        # want to use into the sources directory under the ironic-image repository.
        for CODE_SOURCE_VAR in $(env | grep -E '^IRONIC_SOURCE=|^SUSHY_SOURCE=' | grep -o "^[^=]*"); do
            CODE_SOURCE="${!CODE_SOURCE_VAR}"
            SOURCE_DIR_DEST="${CODE_SOURCE##*/}"
            rm -rf "./sources/${SOURCE_DIR_DEST}"
            cp -a "${CODE_SOURCE}" "./sources/${SOURCE_DIR_DEST}"
            CUSTOM_SOURCE_ARGS+="--build-arg ${CODE_SOURCE_VAR}=${SOURCE_DIR_DEST} "
        done

        # shellcheck disable=SC2086
        sudo "${CONTAINER_RUNTIME}" build --build-arg INSTALL_TYPE=source ${CUSTOM_SOURCE_ARGS:-} \
            -t "${IMAGE_URL}:latest" -t "${IMAGE_URL}:${IMAGE_GIT_HASH}_${IMAGE_DATE}" . -f ./Dockerfile

    # TODO: Do we want to support CAPI in dev-env? CI just pulls it anyways ...
    elif [[ "${IMAGE_VAR/_LOCAL_IMAGE}" == "CAPI" ]]; then
        CAPI_GO_VERSION=$(grep "GO_VERSION ?= [0-9].*" Makefile | sed -e 's/GO_VERSION ?= //g')
        # shellcheck disable=SC2016
        CAPI_BASEIMAGE=$(grep "GO_CONTAINER_IMAGE ?=" Makefile | sed -e 's/GO_CONTAINER_IMAGE ?= //g' -e 's/$(GO_VERSION)//g')
        CAPI_TAGGED_BASE_IMAGE="${CAPI_BASEIMAGE}${CAPI_GO_VERSION}"
        sudo DOCKER_BUILDKIT=1 "${CONTAINER_RUNTIME}" build \
            --build-arg builder_image="${CAPI_TAGGED_BASE_IMAGE}" --build-arg ARCH="amd64" \
            -t "${IMAGE_URL}:latest" -t "${IMAGE_URL}:${IMAGE_GIT_HASH}_${IMAGE_DATE}" . -f ./Dockerfile

    else
        sudo "${CONTAINER_RUNTIME}" rmi "${IMAGE_URL}" || true
        sudo "${CONTAINER_RUNTIME}" build -t "${IMAGE_URL}" . -f ./Dockerfile
    fi

    cd - || exit
    if [[ "${CONTAINER_RUNTIME}" == "podman" ]]; then
        sudo "${CONTAINER_RUNTIME}" push --tls-verify=false "${IMAGE_URL}"
    else
        sudo "${CONTAINER_RUNTIME}" push --platform="${LOCAL_CONTAINER_PLATFORM}" "${IMAGE_URL}"
    fi

    # store the locally built images to config, so they're passed to "make test"
    # and used in pivoting tests etc. Exports from environment are lost between
    # make && make test as make isolates the env
    cat <<EOF >>"${CI_CONFIG_FILE}"
export ${IMAGE_VAR/_LOCAL_IMAGE/_IMAGE}="${IMAGE_URL}"
EOF
done

# unset all *_IMAGE env vars that have a *_LOCAL_IMAGE counterpart to avoid
# tagging and pushing upstream images thus avoid creating duplicates
for IMAGE_VAR in $(env | grep "_LOCAL_IMAGE=" | grep -o "^[^=]*") ; do
    NO_LOCAL_NAME="${IMAGE_VAR%_LOCAL_IMAGE}_IMAGE"
    unset -v "${NO_LOCAL_NAME}"
done

# IRONIC_IMAGE is also used in this script so when it is built locally and
# consequently unset, it has to be redefined for local use
if [[ "${BUILD_IRONIC_IMAGE_LOCALLY:-}" == "true" ]] || [[ -n "${IRONIC_LOCAL_IMAGE:-}" ]]; then
    IRONIC_IMAGE="${REGISTRY}/localimages/$(basename "${IRONIC_LOCAL_IMAGE}")"
    export IRONIC_IMAGE
fi

# Pushing images to local registry
for IMAGE_VAR in $(env | grep -v "_LOCAL_IMAGE=" | grep "_IMAGE=" | grep -o "^[^=]*") ; do
    IMAGE="${!IMAGE_VAR}"
    # shellcheck disable=SC2086
    IMAGE_NAME="${IMAGE##*/}"
    # shellcheck disable=SC2086
    LOCAL_IMAGE="${REGISTRY}/localimages/${IMAGE_NAME%@*}"
    if [[ "${LOCAL_IMAGE}" != "${IMAGE}" ]]; then
        sudo "${CONTAINER_RUNTIME}" rmi "${LOCAL_IMAGE}" || true
    fi
    if [[ "${CONTAINER_RUNTIME}" == "podman" ]]; then
        sudo "${CONTAINER_RUNTIME}" tag "${IMAGE}" "${LOCAL_IMAGE}"
        sudo "${CONTAINER_RUNTIME}" push --tls-verify=false "${LOCAL_IMAGE}"
    else
        sudo "${CONTAINER_RUNTIME}" run --rm --network host \
            -v "/${WORKING_DIR}/registries.conf:/etc/containers/registries.conf:ro" \
            quay.io/skopeo/stable:latest \
            copy \
            --dest-tls-verify=false \
            "docker://${IMAGE}" "docker://${LOCAL_IMAGE}"
    fi
done

# Start httpd-infra container
if [[ "${OS}" == "ubuntu" ]]; then
    # shellcheck disable=SC2086
    sudo "${CONTAINER_RUNTIME}" run -d --net host --privileged --name httpd-infra ${POD_NAME_INFRA} \
        -v "${IRONIC_DATA_DIR}":/shared --entrypoint /bin/runhttpd \
        --env "PROVISIONING_INTERFACE=ironicendpoint" "${IRONIC_IMAGE}"
else
    # shellcheck disable=SC2086
    sudo "${CONTAINER_RUNTIME}" run -d --net host --name httpd-infra ${POD_NAME_INFRA} \
        -v "${IRONIC_DATA_DIR}":/shared --entrypoint /bin/runhttpd "${IRONIC_IMAGE}"
fi

# Physical machines expose their own BMC: no libvirt, no vbmc, no sushy-tools.
# Remove an emulator left behind by an older run of the upstream tree.
sudo "${CONTAINER_RUNTIME}" rm -f vbmc sushy-tools 2>/dev/null || true
