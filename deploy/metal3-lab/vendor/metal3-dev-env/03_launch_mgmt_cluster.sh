#!/usr/bin/env bash

set -eux

# shellcheck disable=SC1091
source lib/logging.sh
# shellcheck disable=SC1091
source lib/common.sh
# shellcheck disable=SC1091
source lib/releases.sh
# shellcheck disable=SC1091
source lib/network.sh

# Default CAPI_CONFIG_DIR to ${HOME}/.config directory if XDG_CONFIG_HOME not set
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}"
export CAPI_CONFIG_DIR="${CONFIG_DIR}/cluster-api"
export IRONIC_HOST="${CLUSTER_BARE_METAL_PROVISIONER_HOST}"
export IRONIC_HOST_IP="${CLUSTER_BARE_METAL_PROVISIONER_IP}"
export REPO_IMAGE_PREFIX="quay.io"

declare -a BMO_IRONIC_ARGS
# -k is for keepalived
BMO_IRONIC_ARGS=(-k)

if [[ "${IRONIC_TLS_SETUP:-true}" = "true" ]]; then
    BMO_IRONIC_ARGS+=("-t")
fi
if [[ "${IRONIC_USE_MARIADB:-false}" = "true" ]]; then
    BMO_IRONIC_ARGS+=("-m")
fi

sudo mkdir -p "${IRONIC_DATA_DIR}"
sudo chown -R "${USER}:${USER}" "${IRONIC_DATA_DIR}"

# Use locally pre-downloaded IPA images served by httpd-infra to avoid slow
# remote downloads during Ironic deployment (which can cause timeouts in CI).
IPA_FILENAME="ipa-${IPA_FLAVOR}-$(echo "${IPA_BRANCH}" | tr / -).tar.gz"
if [[ "${IPA_DOWNLOAD_ENABLED}" = "true" ]] \
    && [[ "${USE_LOCAL_IPA}" != "true" ]] \
    && [[ -f "${IRONIC_IMAGE_DIR}/${IPA_FILENAME}" ]]; then
    export IPA_BASEURI="http://${BARE_METAL_PROVISIONER_URL_HOST}/images"
fi

# shellcheck disable=SC1091
source lib/ironic_tls_setup.sh
# shellcheck disable=SC1091
source lib/ironic_basic_auth.sh

# ------------------------------------
# BMO and Ironic deployment functions
# ------------------------------------

#
# Create the BMO deployment (not used for CAPM3 v1a4 since BMO is bundeled there)
#
launch_baremetal_operator()
{
    safe_pushd "${BMOPATH}"

    # Deploy BMO using deploy.sh script
        # Update container images to use local ones
        if [[ -n "${BARE_METAL_OPERATOR_LOCAL_IMAGE:-}" ]]; then
            update_component_image BMO "${BARE_METAL_OPERATOR_LOCAL_IMAGE}"
        else
            update_component_image BMO "${BARE_METAL_OPERATOR_IMAGE}"
        fi
        if [[ -n "${IRONIC_KEEPALIVED_LOCAL_IMAGE:-}" ]]; then
            update_component_image Keepalived "${IRONIC_KEEPALIVED_LOCAL_IMAGE}"
        else
            update_component_image Keepalived "${IRONIC_KEEPALIVED_IMAGE}"
        fi

    # Update Configmap parameters with correct urls
    cat << EOF | sudo tee "${BMOPATH}/config/default/ironic.env"
IRONIC_ENDPOINT=${IRONIC_URL}
EOF

    if [[ -n "${DEPLOY_ISO_URL}" ]]; then
        echo "DEPLOY_ISO_URL=${DEPLOY_ISO_URL}" | sudo tee -a "${BMOPATH}/config/default/ironic.env"
    fi

    # Deploy BMO using deploy.sh script
    "${BMOPATH}/tools/deploy.sh" -b "${BMO_IRONIC_ARGS[@]}"

    popd
}

#
# Modifies the images to use the ones built locally
# Updates the environment variables to refer to the images
# pushed to the local registry for caching.
#
update_images()
{
    local image_var image image_name local_image old_image_var

    for image_var in $(env | grep "_LOCAL_IMAGE=" | grep -o "^[^=]*" || true) ; do
        image=${!image_var}
        # shellcheck disable=SC2086
        image_name="${image##*/}"
        local_image="${REGISTRY}/localimages/${image_name}"
        old_image_var="${image_var%_LOCAL_IMAGE}_IMAGE"
        declare -g -x "${old_image_var}=${local_image}"
    done

    # Assign images from local image registry after update image
    # This allows to use cached images for faster downloads
    for image_var in $(env | grep -v "_LOCAL_IMAGE=" | grep "_IMAGE=" | grep -o "^[^=]*" || true) ; do
        image=${!image_var}
        # shellcheck disable=SC2086
        image_name="${image##*/}"
        local_image="${REGISTRY}/localimages/${image_name}"
        declare -g "${image_var}=${local_image}"
    done
}

launch_ironic_standalone_operator()
{
    # shellcheck disable=SC2311
    echo "IPA_BASEURI=${IPA_BASEURI}" > "${IRSOPATH}/config/manager/manager.env"
    make -C "${IRSOPATH}" install deploy IMG="$(get_component_image "${IRSO_LOCAL_IMAGE:-${IRSO_IMAGE}}")"
    kubectl wait --for=condition=Available --timeout=60s \
        -n ironic-standalone-operator-system deployment/ironic-standalone-operator-controller-manager
}

launch_ironic_via_irso()
{
    kubectl create secret generic ironic-auth -n "${IRONIC_NAMESPACE}" \
        --from-file=username="${IRONIC_USERNAME_FILE}" \
        --from-file=password="${IRONIC_PASSWORD_FILE}"
    kubectl label secret ironic-auth -n "${IRONIC_NAMESPACE}" \
        environment.metal3.io/ironic-standalone-operator=true

    local ironic="${IRONIC_DATA_DIR}/ironic.yaml"
    cat > "${ironic}" <<EOF
---
apiVersion: ironic.metal3.io/v1alpha1
kind: Ironic
metadata:
  name: ironic
  namespace: "${IRONIC_NAMESPACE}"
spec:
  apiCredentialsName: ironic-auth
  images:
    deployRamdiskBranch: "${IPA_BRANCH}"
    deployRamdiskDownloader: "$(get_component_image "${IPA_DOWNLOADER_LOCAL_IMAGE:-${IPA_DOWNLOADER_IMAGE}}")"
    ironic: "$(get_component_image "${IRONIC_LOCAL_IMAGE:-${IRONIC_IMAGE}}")"
    keepalived: "$(get_component_image "${IRONIC_KEEPALIVED_LOCAL_IMAGE:-${IRONIC_KEEPALIVED_IMAGE}}")"
  version: "${IRSO_IRONIC_VERSION}"
  networking:
    dhcp:
      rangeBegin: "${CLUSTER_DHCP_RANGE_START}"
      rangeEnd: "${CLUSTER_DHCP_RANGE_END}"
      networkCIDR: "${BARE_METAL_PROVISIONER_NETWORK}"
    interface: "${BARE_METAL_PROVISIONER_INTERFACE}"
    ipAddress: "${CLUSTER_BARE_METAL_PROVISIONER_IP}"
    ipAddressManager: keepalived
  deployRamdisk:
    sshKey: "${SSH_PUB_KEY_CONTENT}"
EOF


    if [[ -r "${IRONIC_CERT_FILE}" ]] && [[ -r "${IRONIC_KEY_FILE}" ]]; then
        kubectl create secret tls ironic-cert -n "${IRONIC_NAMESPACE}" --key="${IRONIC_KEY_FILE}" --cert="${IRONIC_CERT_FILE}"
        kubectl label secret ironic-cert -n "${IRONIC_NAMESPACE}" \
            environment.metal3.io/ironic-standalone-operator=true
        cat >> "${ironic}" <<EOF
  tls:
    certificateName: ironic-cert
EOF
    fi

    # This is not used by Ironic currently but is needed by BMO
    if [[ -r "${IRONIC_CACERT_FILE}" ]] && [[ -r "${IRONIC_CAKEY_FILE}" ]]; then
        kubectl create secret tls ironic-cacert -n "${IRONIC_NAMESPACE}" --key="${IRONIC_CAKEY_FILE}" --cert="${IRONIC_CACERT_FILE}"
    fi

    if [[ "${IRONIC_USE_MARIADB:-false}" = "true" ]]; then
        cat >> "${ironic}" <<EOF
  databaseName: ironic-db
---
apiVersion: ironic.metal3.io/v1alpha1
kind: IronicDatabase
metadata:
  name: ironic-db
  namespace: "${IRONIC_NAMESPACE}"
spec:
  image: "$(get_component_image "${MARIADB_LOCAL_IMAGE:-${MARIADB_IMAGE}}")"
EOF
  fi

    # NOTE(dtantsur): the webhook may not be ready immediately, retry if needed.
    local create_retry=20
    while ! kubectl apply --request-timeout=30s -f "${ironic}"; do
        create_retry=$((create_retry - 1))
        if [[ "${create_retry}" -le 0 ]]; then
            echo >&2 "ERROR: failed to apply ironic resource from ${ironic}"
            exit 1
        fi
        sleep 3
    done

    if ! kubectl wait --for=condition=Ready --timeout="${IRONIC_ROLLOUT_WAIT}m" -n "${IRONIC_NAMESPACE}" ironic/ironic; then
        # FIXME(dtantsur): remove this when Ironic objects are collected in the CI
        kubectl get -n "${IRONIC_NAMESPACE}" -o yaml ironic/ironic
        if [[ "${IRONIC_USE_MARIADB:-false}" = "true" ]]; then
            kubectl get -n "${IRONIC_NAMESPACE}" -o yaml ironicdatabase/ironic-db
        fi
        exit 1
    fi
}





# --------------------------
# CAPM3 deployment functions
# --------------------------

get_component_image()
{
    local orig_image=$1
    # Split the image IMAGE_NAME AND IMAGE_TAG, if any tag exist
    local tmp_image="${orig_image##*/}"
    # Remove the digest (already considered when caching the image)
    tmp_image="${tmp_image%@*}"
    local tmp_image_name="${tmp_image%%:*}"
    local tmp_image_tag="${tmp_image##*:}"

    # Assign the image tag to latest if there is no tag in the image
    if [[ "${tmp_image_name}" = "${tmp_image_tag}" ]]; then
        tmp_image_tag="latest"
    fi

    echo "${REGISTRY}/localimages/${tmp_image_name}:${tmp_image_tag}"
}

#
# Update the CAPM3 and BMO manifests to use local images as defined in variables
#
update_component_image()
{
    local import="$1"
    local orig_image="$2"
    local tmp_image

    # shellcheck disable=SC2311
    tmp_image="$(get_component_image "${orig_image}")"
    export MANIFEST_IMG="${tmp_image%:*}"
    export MANIFEST_TAG="${tmp_image##*:}"

    # NOTE: It is assumed that we are already in the correct directory to run make
    case "${import}" in
        "BMO")
            make set-manifest-image-bmo
            ;;
        "CAPM3")
            make set-manifest-image
            ;;
        "IPAM")
            make set-manifest-image
            ;;
        "Ironic")
            make set-manifest-image-ironic
            ;;
        "Keepalived")
            make set-manifest-image-keepalived
            ;;
        "IPA-downloader")
            make set-manifest-image-ipa-downloader
            ;;
        *)
            echo "WARNING: unknown image: ${import}"
            ;;
    esac
}

#
# Update the clusterctl deployment files to use local repositories
#
patch_clusterctl()
{
    safe_pushd "${CAPM3PATH}"

    mkdir -p "${CAPI_CONFIG_DIR}"
    if [[ "${CAPI_NIGHTLY_BUILD:-}" = "true" ]]; then
        rm -f "${CAPI_CONFIG_DIR}"/clusterctl.yaml
    fi
    cat << EOF >> "${CAPI_CONFIG_DIR}"/clusterctl.yaml
providers:
- name: metal3
  url: https://github.com/metal3-io/ip-address-manager/releases/${IPAMRELEASE}/ipam-components.yaml
  type: IPAMProvider
EOF
    if [[ "${CAPI_NIGHTLY_BUILD:-}" = "true" ]]; then
      cat << EOF >> "${CAPI_CONFIG_DIR}"/clusterctl.yaml
- name: "cluster-api"
  type: "CoreProvider"
  url: "${CAPI_CONFIG_DIR}/dev-repository/cluster-api/${CAPIRELEASE}/core-components.yaml"
- name: "kubeadm"
  type: "BootstrapProvider"
  url: "${CAPI_CONFIG_DIR}/dev-repository/bootstrap-kubeadm/${CAPIRELEASE}/bootstrap-components.yaml"
- name: "kubeadm"
  type: "ControlPlaneProvider"
  url: "${CAPI_CONFIG_DIR}/dev-repository/control-plane-kubeadm/${CAPIRELEASE}/control-plane-components.yaml"
EOF
    fi

    # At this point the images variables have been updated with update_images
    # Reflect the change in components files
    if [[ -n "${CAPM3_LOCAL_IMAGE:-}" ]]; then
        update_component_image CAPM3 "${CAPM3_LOCAL_IMAGE}"
    else
        update_component_image CAPM3 "${CAPM3_IMAGE}"
    fi

    make release-manifests

    rm -rf "${CAPI_CONFIG_DIR}"/overrides/infrastructure-metal3/"${CAPM3RELEASE}"
    mkdir -p "${CAPI_CONFIG_DIR}"/overrides/infrastructure-metal3/"${CAPM3RELEASE}"
    cp out/*.yaml "${CAPI_CONFIG_DIR}"/overrides/infrastructure-metal3/"${CAPM3RELEASE}"
    popd
}

patch_capi()
{
    safe_pushd "${CAPIPATH}"
    date=$(date '+%Y%m%d' -d "1 day ago")

    rm -f ./clusterctl-settings.json
    cat << EOF >> ./clusterctl-settings.json
{
  "providers": ["cluster-api","bootstrap-kubeadm","control-plane-kubeadm"]
}
EOF
    ./cmd/clusterctl/hack/create-local-repository.py "https://storage.googleapis.com/k8s-staging-cluster-api/components/nightly_main_${date}"

    cp -r "${CAPI_CONFIG_DIR}"/dev-repository/control-plane-kubeadm/ "${CAPI_CONFIG_DIR}"/overrides/
    cp -r "${CAPI_CONFIG_DIR}"/dev-repository/bootstrap-kubeadm/ "${CAPI_CONFIG_DIR}"/overrides/
    cp -r "${CAPI_CONFIG_DIR}"/dev-repository/cluster-api/ "${CAPI_CONFIG_DIR}"/overrides/
    popd
}

patch_ipam()
{
    safe_pushd "${IPAMPATH}"

    if [[ -n "${IPAM_LOCAL_IMAGE:-}" ]]; then
        update_component_image IPAM "${IPAM_LOCAL_IMAGE}"
    else
        update_component_image IPAM "${IPAM_IMAGE}"
    fi

    make release-manifests
    rm -rf "${CAPI_CONFIG_DIR}"/overrides/ipam-metal3/"${IPAMRELEASE}"
    mkdir -p "${CAPI_CONFIG_DIR}"/overrides/ipam-metal3/"${IPAMRELEASE}"
    cp out/*.yaml "${CAPI_CONFIG_DIR}"/overrides/ipam-metal3/"${IPAMRELEASE}"
    popd
}

# Install clusterctl client
# TODO: use download_and_install_clusterctl (see lib/download.sh)
# Currently we just download latest CAPIRELEASE version, which means we don't know
# the expected SHA, and can't pin it
install_clusterctl()
{
    DOWNLOAD_CAPIRELEASE="${CAPIRELEASE}"

    # Can't use version numbers ending in .99 in so fetch latest stable version
    if [[ "${CAPIRELEASE}" =~ ^v([0-9]+\.[0-9]+)\.99$ ]]; then
        DOWNLOAD_CAPIRELEASE=$(get_validated_release_from_goproxy "${CAPIGOPROXY}" "${CAPI_RELEASE_PREFIX}" "beta|rc|pre|alpha")
    fi
    wget --no-verbose -O clusterctl "https://github.com/kubernetes-sigs/cluster-api/releases/download/${DOWNLOAD_CAPIRELEASE}/clusterctl-linux-amd64"
    chmod +x ./clusterctl
    sudo mv ./clusterctl /usr/local/bin/
}

if ! [[ -x "$(command -v clusterctl)" ]]; then
    install_clusterctl
elif [[ "$(clusterctl version | grep -o -P '(?<=GitVersion:").*?(?=",)')" != "${CAPIRELEASE}" ]]; then
    sudo rm /usr/local/bin/clusterctl
    install_clusterctl
fi

#
# Launch the cluster-api provider metal3.
#
launch_cluster_api_provider_metal3()
{
    safe_pushd "${CAPM3PATH}"

    clusterctl init --core cluster-api:"${CAPIRELEASE}" --bootstrap kubeadm:"${CAPIRELEASE}" \
        --control-plane kubeadm:"${CAPIRELEASE}" --infrastructure=metal3:"${CAPM3RELEASE}" -v5 --ipam=metal3:"${IPAMRELEASE}"


    popd
}


# -------------
# Miscellaneous
# -------------

render_j2_config ()
{
    "${ANSIBLE_VENV}/bin/python" -c 'import os; import sys; import jinja2; sys.stdout.write(jinja2.Template(sys.stdin.read()).render(env=os.environ))' < "${1}"
}

#
# Write out a clouds.yaml for this environment
#
create_clouds_yaml()
{
    # To bind this into the ironic-client container we need a directory
    mkdir -p "${SCRIPTDIR}"/_clouds_yaml
    if [[ "${IRONIC_TLS_SETUP}" = "true" ]]; then
        cp "${IRONIC_CACERT_FILE}" "${SCRIPTDIR}"/_clouds_yaml/ironic-ca.crt
    fi
    render_j2_config "${SCRIPTDIR}"/clouds.yaml.j2 > _clouds_yaml/clouds.yaml
}


# ------------------------
# Management cluster infra
# ------------------------

# -----------------------------
# Deploy the management cluster
# -----------------------------

# Reuse the EPH single-node cluster. No kind cluster or host Ironic is created.
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
kubectl get --raw=/readyz
create_clouds_yaml
kubectl create namespace metal3 --dry-run=client -o yaml | kubectl apply -f -

patch_clusterctl

if [[ "${CAPI_NIGHTLY_BUILD:-}" = "true" ]]; then
    patch_capi
fi

patch_ipam
launch_cluster_api_provider_metal3
BMO_NAME_PREFIX="${NAMEPREFIX}"
launch_baremetal_operator
if [[ "${USE_IRSO}" = true ]]; then
    launch_ironic_standalone_operator
    launch_ironic_via_irso
else
    echo "USE_IRSO=true is required: Ironic must run in the EPH Kubernetes cluster" >&2
    exit 1
fi

if ! kubectl rollout status deployment "${BMO_NAME_PREFIX}"-controller-manager -n "${IRONIC_NAMESPACE}" --timeout="${BMO_ROLLOUT_WAIT}"m; then
    echo >&2 "ERROR: baremetal-operator-controller-manager deployment can not be rollout"
    exit 1
fi
