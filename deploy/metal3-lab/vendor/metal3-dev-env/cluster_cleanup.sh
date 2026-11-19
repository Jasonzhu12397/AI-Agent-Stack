#!/usr/bin/env bash

set -eux

# shellcheck disable=SC1091
source lib/common.sh

# Delete the kind management cluster. minikube and tilt are not part of this lab.
if [[ "${BOOTSTRAP_CLUSTER}" = "kind" ]]; then
    sudo su -l -c "kind delete cluster  || true" "${USER}"
    # Kill and remove the running ironic containers
    if [[ -x "${BMOPATH}/tools/remove_local_ironic.sh" ]]; then
        "${BMOPATH}"/tools/remove_local_ironic.sh
    fi
fi
