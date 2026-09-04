"""Security and packaging contract for the opt-in PosterChanOS Monero wallet RPC."""
from pathlib import Path
import os
import re
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "os/bin/pc-monero-wallet-rpc"
PACKAGED_HELPER = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-monero-wallet-rpc"
UNIT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/posterchan-monero-wallet-rpc.service"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"
BIN_PACKAGE = ROOT / "os/overlay/net-p2p/monero-wallet-rpc-bin"
BIN_EBUILD = BIN_PACKAGE / "monero-wallet-rpc-bin-0.18.5.1.ebuild"
BIN_MANIFEST = BIN_PACKAGE / "Manifest"
BIN_PROVENANCE = BIN_PACKAGE / "files/upstream-provenance.txt"


def _run_enable(tmp_path: Path, *args: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "systemctl-calls"
    for name, body in {
        "monero-wallet-rpc": "#!/bin/sh\nexit 0\n",
        "systemctl": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{calls}'\n",
    }.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    backend = tmp_path / "backend"
    (backend / "data").mkdir(parents=True)
    (backend / "run.py").write_text("")
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{bindir}:{os.environ['PATH']}",
               POSTERCHANAI_ROOT=str(backend))
    result = subprocess.run([str(HELPER), "enable", *args], env=env, text=True,
                            capture_output=True, timeout=30)
    conf = tmp_path / "home/.config/posterchanos/monero-wallet-rpc.conf"
    return result, conf, calls


def test_helper_is_valid_and_packaged_copy_is_exact():
    assert subprocess.run(["sh", "-n", str(HELPER)], capture_output=True).returncode == 0
    assert HELPER.read_bytes() == PACKAGED_HELPER.read_bytes()


def test_enable_defaults_to_private_authenticated_stagenet(tmp_path):
    result, conf, calls = _run_enable(tmp_path)
    assert result.returncode == 0, result.stderr
    text = conf.read_text()
    assert "rpc-bind-ip=127.0.0.1\n" in text
    assert "rpc-bind-port=38083\n" in text
    assert "stagenet=1\n" in text
    assert "trusted-daemon=0\n" in text
    assert re.search(r"^rpc-login=posterchan:[0-9a-f]{64}$", text, re.M)
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    assert "rpc-login" not in result.stdout + result.stderr
    assert "daemon-reload" in calls.read_text() and "enable --now" in calls.read_text()
    assert not list((tmp_path / "home/.local/share/posterchanos/monero").glob("*.keys"))
    secrets = tmp_path / "backend/data/secrets.env"
    boot = secrets.read_text()
    password = re.search(r"^rpc-login=posterchan:([0-9a-f]{64})$", text, re.M).group(1)
    assert f"export MONERO_WALLET_RPC_PASSWORD={password}" in boot
    assert "export MONERO_WALLET_RPC_URL=http://127.0.0.1:38083/json_rpc" in boot
    assert "export MONERO_WALLET_NETWORK=stagenet" in boot
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert "restart posterchanai.service" in calls.read_text()


def test_mainnet_requires_explicit_flag_and_creates_no_wallet(tmp_path):
    result, conf, _ = _run_enable(tmp_path, "--mainnet")
    assert result.returncode == 0
    assert "stagenet" not in conf.read_text()
    assert "MONERO_WALLET_NETWORK=mainnet" in (tmp_path / "backend/data/secrets.env").read_text()
    assert not list(conf.parent.parent.parent.rglob("*.keys"))


def test_existing_credentials_are_not_rotated_on_reenable(tmp_path):
    result, conf, _ = _run_enable(tmp_path)
    assert result.returncode == 0
    original = conf.read_bytes()
    # Use the same fake PATH but a direct invocation because _run_enable creates its bin directory.
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               POSTERCHANAI_ROOT=str(tmp_path / "backend"))
    again = subprocess.run([str(HELPER), "enable", "--stagenet"], env=env,
                           text=True, capture_output=True, timeout=30)
    assert again.returncode == 0
    assert conf.read_bytes() == original


def test_reenable_sanitizes_untrusted_existing_directives(tmp_path):
    result, conf, _ = _run_enable(tmp_path)
    assert result.returncode == 0
    credential = re.search(r"^rpc-login=(.+)$", conf.read_text(), re.M).group(1)
    conf.write_text(conf.read_text() + "rpc-bind-ip=0.0.0.0\nmainnet=1\n")
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               POSTERCHANAI_ROOT=str(tmp_path / "backend"))
    again = subprocess.run([str(HELPER), "enable"], env=env, text=True, capture_output=True, timeout=30)
    assert again.returncode == 0
    text = conf.read_text()
    assert text.count("rpc-bind-ip=") == 1 and "0.0.0.0" not in text
    assert "mainnet" not in text and "stagenet=1" in text
    assert f"rpc-login={credential}" in text


def test_unit_exposes_only_authenticated_loopback_config_and_is_not_auto_enabled():
    unit = UNIT.read_text()
    assert "--config-file=%h/.config/posterchanos/monero-wallet-rpc.conf" in unit
    assert "rpc-login" not in unit
    assert "Environment=" not in unit
    assert "NoNewPrivileges=true" in unit and "ProtectSystem=strict" in unit
    assert "AF_NETLINK" in unit, "Monero's resolver needs netlink even though RPC remains loopback"
    assert "WantedBy=default.target" in unit
    ebuild = EBUILD.read_text()
    iuse = re.search(r'^IUSE="([^"]*)"$', ebuild, re.M)
    assert iuse and "monero" in iuse.group(1).split()
    assert "monero? ( net-p2p/monero-wallet-rpc-bin )" in ebuild
    assert 'doins "${FILESDIR}/posterchan-monero-wallet-rpc.service"' in ebuild
    assert "systemctl --global enable posterchan-monero" not in ebuild


def test_no_credentials_or_wallet_creation_are_committed():
    sources = HELPER.read_text() + UNIT.read_text() + EBUILD.read_text()
    assert not re.search(r"rpc-login=posterchan:[0-9a-f]{16,}", sources)
    for dangerous in ("--generate-new-wallet", "generate-from-keys", "--wallet-file"):
        assert dangerous not in sources


def test_overlay_supplies_a_stable_reproducible_wallet_rpc_binary():
    ebuild = BIN_EBUILD.read_text()
    assert 'KEYWORDS="amd64"' in ebuild
    assert "downloads.getmonero.org/cli/monero-linux-x64-v${PV}.tar.bz2" in ebuild
    assert "dobin monero-wallet-rpc" in ebuild
    assert "monerod" not in ebuild.split("src_install()", 1)[1]
    assert "net-p2p" in (ROOT / "os/overlay/profiles/categories").read_text().splitlines()


def test_manifest_is_tied_to_upstreams_signed_checksum_provenance():
    manifest = BIN_MANIFEST.read_text().strip().split()
    provenance = BIN_PROVENANCE.read_text()
    assert manifest[:3] == ["DIST", "monero-wallet-rpc-bin-0.18.5.1.tar.bz2", "84575716"]
    assert manifest[3] == "BLAKE2B" and len(manifest[4]) == 128
    assert manifest[5] == "SHA512" and len(manifest[6]) == 128
    assert "SHA256: 22a7dda7b0cb699fdd6b7674c3b4a4465b337cc98a54983523b759e1e7cc9958" in provenance
    assert "81AC 591F E9C4 B65C 5806 AFC3 F0AF 4D46 2A0B DF92" in provenance
    assert "monero/blob/v0.18.5.1/utils/gpg_keys/binaryfate.asc" in provenance
