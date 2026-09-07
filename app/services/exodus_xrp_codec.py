"""Offline XRPL codec entry point, executed only in the dedicated SDK environment."""
import json
import sys

LIMIT = 4096


def handle(data):
    from xrpl.core.addresscodec import is_valid_classic_address, is_valid_xaddress, xaddress_to_classic_address
    if data.get('operation') == 'recipient':
        address, tag = data.get('address'), data.get('tag')
        if not isinstance(address, str) or len(address) > 128:
            raise ValueError('Enter a valid XRP address')
        if tag is not None and (type(tag) is not int or not 0 <= tag < 2**32):
            raise ValueError('Enter a valid destination tag')
        if is_valid_xaddress(address):
            classic, embedded, testnet = xaddress_to_classic_address(address)
            if testnet:
                raise ValueError('Use an XRP mainnet address')
            if tag is not None and embedded is not None and tag != embedded:
                raise ValueError('The destination tag does not match this XRP address')
            return {'address':classic, 'tag':embedded if embedded is not None else tag}
        if not is_valid_classic_address(address):
            raise ValueError('Enter a valid XRP address')
        return {'address':address, 'tag':tag}
    if data.get('operation') == 'sign':
        from xrpl.constants import CryptoAlgorithm
        from xrpl.core.binarycodec import encode
        from xrpl.models.transactions import Payment
        from xrpl.transaction import sign
        from xrpl.wallet import Wallet
        wallet = Wallet(public_key=data['public'], private_key='00' + data['private'],
                        algorithm=CryptoAlgorithm.SECP256K1)
        payment = Payment.from_xrpl(data['payment'])
        if payment.account != wallet.address:
            raise ValueError('The sender address does not match the selected wallet')
        signed = sign(payment, wallet)
        return {'blob':encode(signed.to_xrpl()), 'hash':signed.get_hash(), 'address':wallet.address}
    raise ValueError('Unsupported XRP signing operation')


def main():
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('XRP signing request is too large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Invalid XRP signing request')
        result = handle(data)
    except ValueError as error:
        # Only validation messages are emitted. Never print tracebacks or request values.
        safe = ('Enter a valid XRP address', 'Enter a valid destination tag', 'Use an XRP mainnet address',
                'The destination tag does not match this XRP address',
                'The sender address does not match the selected wallet')
        result = {'error':str(error) if str(error) in safe else 'XRP signing could not be completed'}
    except Exception:
        result = {'error':'XRP signing could not be completed'}
    encoded = json.dumps(result, separators=(',', ':'))
    if len(encoded) > LIMIT:
        encoded = '{"error":"XRP signing response is too large"}'
    sys.stdout.write(encoded)


if __name__ == '__main__':
    main()
