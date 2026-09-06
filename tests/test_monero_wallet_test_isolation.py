"""A misconfigured test must never charge the host wallet spending ledger."""
import pytest
from app.services.monero_wallet_service import TransferGate


def test_wallet_test_refuses_ledger_outside_its_temporary_directory(tmp_path):
    outside = tmp_path.parent / "host-wallet-spend.sqlite3"
    with pytest.raises(AssertionError, match="outside the test temporary directory"):
        TransferGate._connect(str(outside))
    assert not outside.exists()


def test_wallet_test_can_use_its_own_durable_ledger(tmp_path):
    ledger = tmp_path / "wallet.sqlite3"
    with TransferGate._connect(str(ledger)) as db:
        db.execute("INSERT INTO monero_spend_attempts VALUES (1, 3, 100)")
    with TransferGate._connect(str(ledger)) as db:
        assert db.execute("SELECT SUM(amount_atomic) FROM monero_spend_attempts").fetchone()[0] == 100
