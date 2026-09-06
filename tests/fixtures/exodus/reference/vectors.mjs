import { Keychain } from '@exodus/keychain/module/index.js';
import { mnemonicToSeed } from '@exodus/bip39';
const mnemonic='abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const seed=await mnemonicToSeed({mnemonic});
const keychain=new Keychain({});const seedId=await keychain.unlock(seed);
const paths={BTC:0,ETH:60,LTC:2,DOGE:3,BCH:145,MATIC:60,BNB:60,AVAX:60,SOL:501,XRP:144};
const vectors=[];
for(const account of [0,1,15])for(const [symbol,coin] of Object.entries(paths)){
 const path=`m/44'/${coin}'/${account}'/0${symbol==='XRP'?"'":''}/0${symbol==='XRP'?"'":''}`;
 const keyId={assetName:symbol.toLowerCase(),derivationAlgorithm:'BIP32',derivationPath:path,keyType:symbol==='SOL'?'nacl':'secp256k1'};
 const got=await keychain.exportKey({seedId,keyId,exportPrivate:true});
 vectors.push({symbol,account,path,privateKey:got.privateKey.toString('hex'),publicKey:got.publicKey.toString('hex')});
}
for(const account of [0,1,15])for(const purpose of [84,86])for(const change of [0,1])for(const index of [0,1]){
 const path=`m/${purpose}'/0'/${account}'/${change}/${index}`;
 const keyId={assetName:'bitcoin',derivationAlgorithm:'BIP32',derivationPath:path,keyType:'secp256k1'};
 const got=await keychain.exportKey({seedId,keyId,exportPrivate:true});
 vectors.push({symbol:'BTC',account,purpose,change,index,path,privateKey:got.privateKey.toString('hex'),publicKey:got.publicKey.toString('hex')});
}
console.log(JSON.stringify({source:'@exodus/keychain 12.0.0; official derivation path table retrieved 2026-09-06',mnemonic,vectors},null,2));
