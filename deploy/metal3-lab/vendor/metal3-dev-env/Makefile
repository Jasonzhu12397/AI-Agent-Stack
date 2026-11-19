all: install_requirements configure_host launch_mgmt_cluster verify

nodep: configure_host launch_mgmt_cluster verify

ci_run: configure_host launch_mgmt_cluster verify

install_requirements:
	./01_prepare_host.sh

configure_host:
	./02_configure_host.sh

launch_mgmt_cluster:
	./03_launch_mgmt_cluster.sh

# Verifies the initial environment setup and the BMHs the agent registered.
verify:
	./04_verify.sh

clean: delete_mgmt_cluster host_cleanup

delete_mgmt_cluster:
	./cluster_cleanup.sh

host_cleanup:
	./host_cleanup.sh

prepull_images:
	./lib/image_prepull.sh

.PHONY: all nodep ci_run install_requirements configure_host launch_mgmt_cluster verify clean delete_mgmt_cluster host_cleanup prepull_images
