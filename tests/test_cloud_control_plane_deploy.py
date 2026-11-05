pathlib = __import__("pathlib")


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_cloud_control_plane_deploy_script_validates_production_environment():
    script = (
        ROOT / "deploy/cloud-control-plane/deploy.sh"
    ).read_text(encoding="utf-8")

    for required in (
        "CLOUD_ENV_FILE_NOT_FOUND",
        "SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "QDRANT_URL",
        "PRODUCTION_SECRET_KEY_INVALID",
        "HTTPS_ORIGINS_REQUIRED",
        "docker compose",
        "CLOUD_CONTROL_PLANE_HEALTHCHECK_FAILED",
        "CLOUD_CONTROL_PLANE_DEPLOYED",
    ):
        assert required in script
