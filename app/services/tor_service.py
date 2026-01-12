"""
Built-in Tor client service.
Manages a Tor process with configurable exit nodes.
"""

import os
import subprocess
import threading
import logging
import time
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TorService:
    """
    Manages a built-in Tor process with configurable exit nodes.
    """

    _instance: Optional['TorService'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        socks_port: int = 9050,
        control_port: int = 9051,
        exit_nodes: str = "{us}",
        data_dir: str = "/var/lib/posterchanai/tor",
        hidden_services: str = "",
    ):
        self.listen_host = listen_host
        self.socks_port = socks_port
        self.control_port = control_port
        self.exit_nodes = exit_nodes
        self.data_dir = Path(data_dir)
        self.hidden_services = hidden_services

        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

    @classmethod
    def get_instance(
        cls,
        listen_host: str = "127.0.0.1",
        socks_port: int = 9050,
        control_port: int = 9051,
        exit_nodes: str = "{us}",
        data_dir: str = "/var/lib/posterchanai/tor",
        hidden_services: str = "",
    ) -> 'TorService':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    listen_host=listen_host,
                    socks_port=socks_port,
                    control_port=control_port,
                    exit_nodes=exit_nodes,
                    data_dir=data_dir,
                    hidden_services=hidden_services,
                )
            return cls._instance

    def _find_tor_binary(self) -> Optional[str]:
        """Find the Tor binary."""
        # Check common locations
        paths = [
            "/usr/bin/tor",
            "/usr/local/bin/tor",
            "/opt/homebrew/bin/tor",  # macOS Homebrew
            shutil.which("tor"),
        ]
        for path in paths:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def _create_torrc(self) -> Path:
        """Create torrc configuration file."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        torrc_path = self.data_dir / "torrc"

        # Build SocksPolicy based on listen address
        if self.listen_host in ("0.0.0.0", "::"):
            # Listening on all interfaces - allow private networks
            socks_policy = """# Allow connections from private networks
SocksPolicy accept 127.0.0.0/8
SocksPolicy accept 10.0.0.0/8
SocksPolicy accept 172.16.0.0/12
SocksPolicy accept 192.168.0.0/16
SocksPolicy reject *"""
        elif self.listen_host == "127.0.0.1":
            # Localhost only
            socks_policy = "SocksPolicy accept 127.0.0.0/8\nSocksPolicy reject *"
        else:
            # Specific IP - allow that subnet
            socks_policy = f"""# Allow connections from local network
SocksPolicy accept 127.0.0.0/8
SocksPolicy accept {self.listen_host.rsplit('.', 1)[0]}.0/24
SocksPolicy reject *"""

        config = f"""# Auto-generated torrc for posterchanai
SocksPort {self.listen_host}:{self.socks_port}
ControlPort {self.control_port}
DataDirectory {self.data_dir}

{socks_policy}

# Exit node restrictions
ExitNodes {self.exit_nodes}
StrictNodes 1

# Performance settings
CircuitBuildTimeout 30
LearnCircuitBuildTimeout 0
MaxCircuitDirtiness 600

# Logging
Log notice file {self.data_dir}/tor.log

# Disable unnecessary features
AvoidDiskWrites 1
"""

        # Add hidden services if configured
        if self.hidden_services and self.hidden_services.strip():
            config += f"""
# Hidden Services
{self.hidden_services.strip()}
"""
            logger.info(f"[TOR] Hidden services configured")

        logger.info(f"[TOR] Creating torrc: listen={self.listen_host}:{self.socks_port}, exits={self.exit_nodes}")

        torrc_path.write_text(config)
        logger.info(f"Created torrc at {torrc_path}")
        return torrc_path

    def start(self) -> bool:
        """Start the Tor process."""
        if self._running:
            logger.warning("Tor already running")
            return True

        tor_binary = self._find_tor_binary()
        if not tor_binary:
            logger.error("Tor binary not found. Install Tor: apt install tor")
            return False

        try:
            torrc_path = self._create_torrc()

            # Start Tor process
            self._process = subprocess.Popen(
                [tor_binary, "-f", str(torrc_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            self._running = True

            # Start monitor thread
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self._monitor_thread.start()

            # Wait for Tor to bootstrap
            if self._wait_for_bootstrap():
                logger.info(f"Tor started successfully - SOCKS5 on port {self.socks_port}")
                return True
            else:
                logger.error("Tor failed to bootstrap within timeout")
                self.stop()
                return False

        except Exception as e:
            logger.error(f"Failed to start Tor: {e}")
            return False

    def _wait_for_bootstrap(self, timeout: int = 60) -> bool:
        """Wait for Tor to complete bootstrapping."""
        import socket

        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self._running or not self._process:
                return False

            # Try to connect to SOCKS port
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(('127.0.0.1', self.socks_port))
                sock.close()
                if result == 0:
                    # Port is open, try SOCKS5 handshake
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(5)
                        sock.connect(('127.0.0.1', self.socks_port))
                        sock.send(b'\x05\x01\x00')  # SOCKS5 greeting
                        response = sock.recv(2)
                        sock.close()
                        if response == b'\x05\x00':
                            return True
                    except Exception:
                        pass
            except Exception:
                pass

            time.sleep(1)

        return False

    def _monitor(self):
        """Monitor Tor process and restart if needed."""
        while self._running and self._process:
            ret = self._process.poll()
            if ret is not None:
                logger.warning(f"Tor process exited with code {ret}")
                if self._running:
                    # Attempt restart
                    logger.info("Attempting to restart Tor...")
                    time.sleep(5)
                    self.start()
                break
            time.sleep(5)

    def stop(self):
        """Stop the Tor process."""
        self._running = False

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception as e:
                logger.error(f"Error stopping Tor: {e}")
            finally:
                self._process = None

        logger.info("Tor stopped")

    def get_new_identity(self) -> bool:
        """Request a new Tor circuit (new identity)."""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(('127.0.0.1', self.control_port))
            sock.send(b'AUTHENTICATE ""\r\n')
            response = sock.recv(1024)
            if b'250' in response:
                sock.send(b'SIGNAL NEWNYM\r\n')
                response = sock.recv(1024)
                sock.close()
                if b'250' in response:
                    logger.info("Got new Tor identity")
                    return True
            sock.close()
        except Exception as e:
            logger.error(f"Failed to get new identity: {e}")
        return False

    def is_running(self) -> bool:
        """Check if Tor is running."""
        return self._running and self._process is not None and self._process.poll() is None

    def get_status(self) -> dict:
        """Get Tor status."""
        return {
            "running": self.is_running(),
            "socks_port": self.socks_port,
            "control_port": self.control_port,
            "exit_nodes": self.exit_nodes,
            "data_dir": str(self.data_dir),
        }


def start_tor_service(
    listen_host: str = "127.0.0.1",
    socks_port: int = 9050,
    control_port: int = 9051,
    exit_nodes: str = "{us}",
    data_dir: str = "/var/lib/posterchanai/tor",
    hidden_services: str = "",
) -> Optional[TorService]:
    """Start Tor service and return instance."""
    service = TorService.get_instance(
        listen_host=listen_host,
        socks_port=socks_port,
        control_port=control_port,
        exit_nodes=exit_nodes,
        data_dir=data_dir,
        hidden_services=hidden_services,
    )
    if service.start():
        return service
    return None


def stop_tor_service():
    """Stop Tor service if running."""
    if TorService._instance:
        TorService._instance.stop()
