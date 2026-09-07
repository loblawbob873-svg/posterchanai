"""Offline native UTXO transaction construction using maintained transaction libraries."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import re

from app.services.exodus_send_service import SendRefused

MAX_INPUTS = 50
MAX_FEE = {'BTC':1_000_000, 'LTC':10_000_000, 'DOGE':10_000_000_000, 'BCH':1_000_000}
# Atomic units per virtual byte differ in economic scale between chains.
# DOGE permits up to 0.1 DOGE/byte; MAX_FEE still caps the complete payment fee.
MAX_FEE_RATE = {'BTC':10_000, 'LTC':10_000, 'DOGE':10_000_000, 'BCH':10_000}
DUST = {'BTC':546, 'LTC':1000, 'DOGE':1_000_000, 'BCH':546}
NETWORKS = {'BTC':(0, (5,), 'bc'), 'LTC':(48, (50,5), 'ltc'), 'DOGE':(30,(22,),None)}


@dataclass(frozen=True)
class Coin:
    txid: str
    index: int
    units: int
    script: bytes
    private_key: bytes
    family: str


def address_script(symbol, address):
    from embit import base58, bech32, script
    if not isinstance(address,str) or not 14 <= len(address) <= 128:
        raise SendRefused('Enter a valid recipient address')
    try:
        if symbol == 'BCH':
            from bitcash.cashaddress import Address
            from bitcash.exceptions import InvalidAddress
            try:
                decoded = Address.from_string(address if ':' in address else 'bitcoincash:'+address)
            except InvalidAddress as error:
                raise ValueError('Invalid cash address') from error
            if decoded.prefix != 'bitcoincash' or decoded.version not in ('P2PKH','P2SH20','P2SH32'):
                raise ValueError('Unsupported cash address')
            return script.Script(decoded.scriptcode)
        p2pkh,p2sh,hrp=NETWORKS[symbol]
        if hrp and address.lower().startswith(hrp+'1'):
            version,program=bech32.decode(hrp,address)
            if version is None or program is None or (version==0 and len(program) not in (20,32)):
                raise ValueError('Invalid witness program')
            if version not in (0,1) or (version==1 and (symbol!='BTC' or len(program)!=32)):
                raise ValueError('Unsupported witness program')
            return script.Script(bytes([0 if version==0 else 0x51,len(program)])+bytes(program))
        decoded=base58.decode_check(address)
        if len(decoded)!=21:
            raise ValueError('Invalid address length')
        if decoded[0]==p2pkh:
            return script.Script(b'\x76\xa9\x14'+decoded[1:]+b'\x88\xac')
        if decoded[0] in p2sh:
            return script.Script(b'\xa9\x14'+decoded[1:]+b'\x87')
    except (ValueError,KeyError,TypeError) as error:
        raise SendRefused('The recipient address is invalid or belongs to another network') from error
    raise SendRefused('The recipient address belongs to another network')


def verify_coin(coin, raw):
    """Bind every provider amount/script to the hash-identified funding transaction."""
    from embit.transaction import Transaction
    from embit import ec,script
    if (not re.fullmatch(r'[0-9a-f]{64}',coin.txid) or type(coin.index) is not int or coin.index<0
            or type(coin.units) is not int or not 0<coin.units<2**63):
        raise SendRefused('The network returned an invalid unspent output')
    try:
        transaction=Transaction.parse(raw)
        if transaction.txid().hex()!=coin.txid:
            raise ValueError('Wrong funding hash')
        output=transaction.vout[coin.index]
        if output.value!=coin.units or output.script_pubkey.data!=coin.script:
            raise ValueError('Funding output mismatch')
        key=ec.PrivateKey(coin.private_key)
        expected={'p2pkh':script.p2pkh,'p2wpkh':script.p2wpkh,'p2tr':script.p2tr}[coin.family](key.get_public_key())
        if expected.data!=coin.script:
            raise ValueError('Funding output does not belong to selected key')
    except (ValueError,IndexError,KeyError) as error:
        raise SendRefused('The funding transaction could not be verified against this wallet') from error
    return transaction


def _signed(symbol, coins, outputs):
    from embit import ec,script
    from embit.transaction import Transaction,TransactionInput,TransactionOutput
    tx=Transaction(version=1 if symbol=='BCH' else 2,
                   vin=[TransactionInput(bytes.fromhex(c.txid),c.index,sequence=0xffffffff) for c in coins],
                   vout=[TransactionOutput(units,sc) for sc,units in outputs])
    if symbol=='BCH':
        from bitcash import PrivateKey
        from bitcash.network.meta import Unspent
        from bitcash.transaction import create_p2pkh_transaction
        from bitcash.types import PreparedOutput,CashTokens
        if any(c.family!='p2pkh' for c in coins):
            raise SendRefused('Unsupported BCH funding script')
        unspent=[Unspent(c.units,1,c.script.hex(),c.txid,c.index) for c in coins]
        prepared=[PreparedOutput(sc.data,units,CashTokens(None,None,None,None)) for sc,units in outputs]
        # BitCash signs a single-key transaction. Construct its validated fork-ID signature
        # for each participating key, then retain only that key's matching input scripts.
        # No signature preimage or cryptographic primitive is implemented here.
        for secret in dict.fromkeys(c.private_key for c in coins):
            signed=Transaction.parse(bytes.fromhex(create_p2pkh_transaction(PrivateKey.from_bytes(secret),unspent,prepared)))
            for index,coin in enumerate(coins):
                if coin.private_key==secret:
                    tx.vin[index].script_sig=signed.vin[index].script_sig
        return tx
    for index,coin in enumerate(coins):
        key=ec.PrivateKey(coin.private_key); public=key.get_public_key()
        if coin.family=='p2tr' and symbol=='BTC':
            digest=tx.sighash_taproot(index,[script.Script(c.script) for c in coins],[c.units for c in coins])
            tx.vin[index].witness=script.Witness([key.taproot_tweak().schnorr_sign(digest).serialize()])
        elif coin.family=='p2wpkh' and symbol=='BTC':
            digest=tx.sighash_segwit(index,script.p2pkh(public),coin.units)
            tx.vin[index].witness=script.Witness([key.sign(digest).serialize()+b'\x01',public.sec()])
        elif coin.family=='p2pkh':
            signature=key.sign(tx.sighash_legacy(index,script.Script(coin.script))).serialize()+b'\x01'
            tx.vin[index].script_sig=script.Script(bytes([len(signature)])+signature+bytes([len(public.sec())])+public.sec())
        else:
            raise SendRefused('Unsupported funding script')
    return tx


def vbytes(transaction):
    raw=transaction.serialize()
    if not transaction.is_segwit:
        return len(raw)
    # SDK serialization of the same transaction without witnesses gives its base size.
    from embit.transaction import Transaction
    copy=Transaction.parse(raw)
    from embit.script import Witness
    for inp in copy.vin: inp.witness=Witness([])
    base=len(copy.serialize())
    return (base*3+len(raw)+3)//4


def build(symbol, coins, *, to, units, change, fee_rate):
    """Largest-first bounded selection, explicit change and a checked final serialized fee."""
    if symbol not in MAX_FEE or type(units) is not int or units<DUST[symbol]:
        raise SendRefused('The amount is below this network\'s minimum output')
    try:
        rate=Decimal(str(fee_rate))
        if not rate.is_finite() or not 0<rate<=MAX_FEE_RATE[symbol]:
            raise ValueError('Invalid fee rate')
    except Exception as error:
        raise SendRefused('The network fee rate could not be verified') from error
    recipient,change_script=address_script(symbol,to),address_script(symbol,change)
    if len({(c.txid,c.index) for c in coins})!=len(coins):
        raise SendRefused('The network returned duplicate unspent outputs')
    selected=[];total=0
    for coin in sorted(coins,key=lambda c:(-c.units,c.txid,c.index))[:MAX_INPUTS]:
        selected.append(coin);total+=coin.units
        # Worst-case input sizes bound fees before selection; exact signed size is checked below.
        estimate=12+sum({'p2pkh':149,'p2wpkh':69,'p2tr':58}[c.family] for c in selected)
        estimate+=len(recipient.serialize())+8+len(change_script.serialize())+8
        fee=int((rate*estimate).to_integral_value(rounding=ROUND_CEILING))
        if total>=units+fee:
            break
    else:
        raise SendRefused('Not enough spendable outputs for this amount and fee; wait for address discovery or reduce the amount')
    remainder=total-units-fee
    outputs=[(recipient,units)]
    if remainder>=DUST[symbol]:
        outputs.append((change_script,remainder))
    else:
        fee+=remainder;remainder=0
    if fee>MAX_FEE[symbol]:
        raise SendRefused('The transaction fee exceeds the native wallet safety limit')
    tx=_signed(symbol,selected,outputs)
    size=vbytes(tx)
    if fee<int((rate*size).to_integral_value(rounding=ROUND_CEILING)):
        raise SendRefused('The signed transaction requires a larger fee; nothing was submitted')
    return {'raw':tx.serialize().hex(),'hash':tx.txid().hex(),'fee':fee,'units':units,'to':to,
            'change':remainder,'vbytes':size,'inputs':[{'txid':c.txid,'index':c.index} for c in selected]}
