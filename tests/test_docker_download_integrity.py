"""Production images must not execute an unverified downloaded MediaMTX binary."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text().splitlines()


def test_every_supported_mediamtx_architecture_has_a_sha256_digest():
    expected = {
        "AMD64": "f9c601cc303ceca8fad2883917b022882672c5bc56311e92dbceb16e5f20c60c",
        "ARM64": "562f419912a8668c18216a9e8c95359ec82fbb754e4a44e2953ef62b98eec688",
        "ARMV7": "de0afed5ba33df231a6c3321207b4a906f1da9be7ce8b3efac008928e982ca6d",
    }
    for arch, digest in expected.items():
        assert f"ARG MEDIAMTX_SHA256_{arch}={digest}" in DOCKERFILE
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_download_is_verified_before_the_archive_is_extracted():
    download = DOCKERFILE.index('curl -fsSL "https://github.com/bluenviron/mediamtx/releases/')
    verify = DOCKERFILE.index("sha256sum -c -", download)
    extract = DOCKERFILE.index("tar -xzf /tmp/m.tgz", download)
    install = DOCKERFILE.index("install -m 0755 /tmp/mediamtx", download)
    assert download < verify < extract < install


def test_unknown_architecture_does_not_silently_receive_amd64_binary():
    block = DOCKERFILE[DOCKERFILE.index('case "${TARGETARCH:-amd64}"'):
                       DOCKERFILE.index("esac &&", DOCKERFILE.index('case "${TARGETARCH:-amd64}"'))]
    assert '*) MTXARCH=amd64' not in block
    assert "unsupported MediaMTX architecture" in block


def test_runtime_sockets_are_not_sent_in_the_docker_build_context():
    """Docker cannot archive Unix sockets; `.run/ssh-keeper.sock` used to poison fresh builds."""
    assert ".run/" in {line.strip() for line in DOCKERIGNORE}
