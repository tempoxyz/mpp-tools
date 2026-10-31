// Generates ../../vectors/*.json from the case inputs below, with expected
// outputs from the TypeScript reference adapter. Run: node generate.ts
import { readFileSync, writeFileSync } from 'node:fs'
import { Account as TempoAccount } from 'viem/tempo'
import { SignatureEnvelope, TxEnvelopeTempo } from 'ox/tempo'
import { Hash, Hex, Secp256k1, Address, AbiFunction } from 'ox'
import { handle } from './adapter.ts'

const key = (label: string) => Hash.keccak256(Hex.fromString('tempo-conformance/' + label))
const addr = (pk: string) => Address.fromPublicKey(Secp256k1.getPublicKey({ privateKey: pk as Hex.Hex })).toLowerCase()

const CHAIN = '1337'
const PATH_USD = '0x20c0000000000000000000000000000000000000'
const ALPHA_USD = '0x20c0000000000000000000000000000000000001'
const RECIPIENT = '0x000000000000000000000000000000000000beef'
const transfer = AbiFunction.from('function transfer(address to, uint256 amount) returns (bool)')
const transferCall = (amount = 1n, to = RECIPIENT) => ({ to: PATH_USD, value: '0', data: AbiFunction.encodeData(transfer, [to as Hex.Hex, amount]) })
// Counter contract init code from tempo-go tests/testdata/Counter.hex
const COUNTER_INIT = '0x6080604052348015600e575f5ffd5b5060e180601a5f395ff3fe6080604052348015600e575f5ffd5b50600436106030575f3560e01c80638381f58a146034578063d09de08a14604e575b5f5ffd5b603a6056565b604051604591906078565b60405180910390f35b6054605b565b005b5f5481565b5f80549080606883609c565b9190505550565b5f819050919050565b607f81606e565b82525050565b5f6020820190506096825f8301606f5b92915050565b5f60a282606e565b91507fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff820360ce5760cd60c0565b5b60018201905091905056'

const base = (over: Record<string, unknown> = {}) => ({
  chainId: CHAIN,
  nonce: '0',
  nonceKey: '0',
  gas: '2000000',
  maxFeePerGas: '50000000000',
  maxPriorityFeePerGas: '1000000000',
  calls: [transferCall()],
  ...over,
})

type Case = { name: string; op: string; input: any; expected?: any; expectedError?: string; node?: { expect: string; fund?: string[]; feePayer?: string; requires?: string[]; knownIssue?: { reason: string; problems: string[] } }; source?: string; description?: string; knownIssue?: string }
const signCases: Case[] = []
const sign = (name: string, description: string, source: string, tx: any, opts: { signer?: any; feePayer?: boolean; node?: string; requires?: string } = {}) => {
  const signer = opts.signer ?? { type: 'secp256k1', privateKey: key(name + '/sender') }
  signCases.push({
    name, description, source, op: 'tx.sign',
    input: { tx, signer, ...(opts.feePayer ? { feePayerPrivateKey: key(name + '/fee-payer') } : {}) },
    ...(opts.node === 'none' ? {} : { node: { expect: opts.node ?? 'success', ...(opts.requires ? { requires: [opts.requires] } : {}) } }),
  })
}

// --- transactions, all submitted to the local node -------------------------
sign('minimal', 'One TIP-20 transfer call, protocol nonce, no fee token.', 'tempo-go transaction_vectors_test.go minimal_transaction; ox TxEnvelopeTempo.test.ts serialize minimal', base())
sign('second_nonce', 'Nonce 1 for the same sender as a prior case in this file (runner sends nonce 0 first).', 'tempo-go transaction_vectors_test.go', base({ nonce: '1' }), { signer: { type: 'secp256k1', privateKey: key('minimal/sender') } , requires: 'minimal' })
sign('with_fee_token', 'Explicit fee token preference (pathUSD).', 'tempo-go with_fee_token; ox serialize feeToken', base({ feeToken: PATH_USD }))
sign('fee_token_alpha_usd', 'Fee token set to a second TIP-20 (AlphaUSD); the sender holds none, so the node rejects it.', 'new', base({ feeToken: ALPHA_USD }), { node: 'reject' })
sign('multiple_calls_batch', 'Three calls executed atomically.', 'tempo-go multiple_calls_batch; ox serialize multiple calls', base({ calls: [transferCall(1n), transferCall(2n, '0x000000000000000000000000000000000000cafe'), transferCall(3n)] }))
sign('zero_value_empty_data', 'Call to an EOA with zero value and empty data.', 'tempo-go single_call_zero_value', base({ calls: [{ to: RECIPIENT, value: '0', data: '0x' }] }))
sign('call_with_value', 'Call with non-zero value; Tempo has no native token, so the node rejects it.', 'tempo-go single_call_with_value', base({ calls: [{ to: RECIPIENT, value: '1', data: '0x' }] }), { node: 'reject' })
sign('contract_creation', 'First call deploys Counter (to = null).', 'tempo-go tests/testdata/Counter.hex', base({ gas: '3000000', calls: [{ to: null, value: '0', data: COUNTER_INIT }] }))
sign('access_list', 'Access list with one address and one storage key.', 'tempo-go transaction_with_all_fields; ox deserialize accessList', base({ accessList: [{ address: PATH_USD, storageKeys: ['0x0000000000000000000000000000000000000000000000000000000000000001'] }] }))
sign('user_nonce_key_1', '2D nonce: nonce key 1.', 'tempo-go user_nonce_key_1; ox deserialize nonceKey', base({ nonceKey: '1' }))
sign('user_nonce_key_large', '2D nonce: 128-bit nonce key.', 'tempo-go user_nonce_key_large', base({ nonceKey: '340282366920938463463374607431768211455' }))
sign('valid_before', 'Expires in 2100.', 'tempo-go with_valid_before; ox deserialize validBefore', base({ validBefore: '4102444800' }))
sign('valid_after', 'Valid after 1 second past epoch (already valid).', 'tempo-go with_valid_after; ox deserialize validAfter', base({ validAfter: '1' }))
sign('validity_window', 'validAfter < validBefore, both set.', 'tempo-go with_validity_window', base({ validAfter: '1', validBefore: '4102444800' }))
sign('expired_valid_before', 'validBefore in the past: signs, but the node rejects it.', 'new', base({ validBefore: '1000' }), { node: 'reject' })
sign('wrong_chain_id', 'Signed for chain 1; the dev node (1337) rejects it.', 'tempo-go minimal_transaction (chain 1)', base({ chainId: '1' }), { node: 'reject' })
sign('high_gas_prices', '100/200 gwei.', 'tempo-go high_gas_prices', base({ maxPriorityFeePerGas: '100000000000', maxFeePerGas: '200000000000' }))
// The runner funds only the fee payer of sponsored cases, so these calls move no tokens.
const noop = [{ to: RECIPIENT, value: '0', data: '0x' }]
sign('sponsored', 'Fee payer co-signs and pays; the sender holds no tokens.', 'tempo-go sponsored_transaction; ox serialize with feePayerSignature', base({ calls: noop }), { feePayer: true })
sign('sponsored_with_fee_token', 'Fee payer picks the fee token; the sender payload leaves it out.', 'tempo-go sponsored_with_fee_token', base({ calls: noop, feeToken: PATH_USD }), { feePayer: true })

const accessKey = key('keychain/access-key')
const root = key('keychain/root')
sign('key_authorization', 'Root signs a tx that also authorizes a secp256k1 access key with a pathUSD limit.', 'ox serialize keyAuthorization (secp256k1); tempo-go keychain SignAndAttach',
  base({ keyAuthorization: { privateKey: root, authorization: { chainId: CHAIN, keyType: 'secp256k1', keyId: addr(accessKey), expiry: null, limits: [{ token: PATH_USD, limit: '1000000000' }] } } }),
  { signer: { type: 'secp256k1', privateKey: root } })
sign('keychain_access_key', 'The access key authorized above signs for the root account (V2 keychain signature).', 'tempo-go keychain SignWithAccessKey; ox SignatureEnvelope keychain',
  base({ nonce: '1' }), { signer: { type: 'keychain', accessKeyPrivateKey: accessKey, rootAccount: addr(root) } , requires: 'key_authorization' })
sign('keychain_sponsored', 'Access key signs, fee payer co-signs.', 'new',
  base({ nonce: '2' }), { signer: { type: 'keychain', accessKeyPrivateKey: accessKey, rootAccount: addr(root) }, feePayer: true , requires: 'keychain_access_key' })
sign('keychain_unauthorized_key', 'A key the root never authorized signs for it; the node rejects it.', 'new',
  base({ nonce: '3' }), { signer: { type: 'keychain', accessKeyPrivateKey: key('keychain/stranger'), rootAccount: addr(root) }, node: 'reject' , requires: 'keychain_sponsored' })

const accessKey2 = key('keychain2/access-key')
const root2 = key('keychain2/root')
sign('key_authorization_expiry_periodic_limit', 'Access key with an expiry and a daily pathUSD limit.', 'tempo-primitives TokenLimit.period; ox KeyAuthorization period',
  base({ keyAuthorization: { privateKey: root2, authorization: { chainId: CHAIN, keyType: 'secp256k1', keyId: addr(accessKey2), expiry: '4102444800', limits: [{ token: PATH_USD, limit: '5000000', period: '86400' }] } } }),
  { signer: { type: 'secp256k1', privateKey: root2 } })
sign('keychain_periodic_key', 'The periodic-limit access key signs for its root.', 'new',
  base({ nonce: '1' }), { signer: { type: 'keychain', accessKeyPrivateKey: accessKey2, rootAccount: addr(root2) } , requires: 'key_authorization_expiry_periodic_limit' })

const accessKey3 = key('keychain3/access-key')
const root3 = key('keychain3/root')
sign('key_authorization_no_spending', 'Access key with limits = [] (no spending allowed).', 'tempo-primitives KeyAuthorization.limits Some([])',
  base({ keyAuthorization: { privateKey: root3, authorization: { chainId: CHAIN, keyType: 'secp256k1', keyId: addr(accessKey3), expiry: null, limits: [] } } }),
  { signer: { type: 'secp256k1', privateKey: root3 } })
sign('keychain_no_spending_transfer', 'The no-spending key tries a pathUSD transfer: the pool rejects it (SpendingLimitExceeded).', 'new',
  base({ nonce: '1' }), { signer: { type: 'keychain', accessKeyPrivateKey: accessKey3, rootAccount: addr(root3) }, node: 'reject' , requires: 'key_authorization_no_spending' })

// --- static cross-checks against vectors published in the SDK repos ---------
signCases.push({
  name: 'tempo_go_minimal_transaction', op: 'tx.sign', description: 'tempo-go golden: chain 1, anvil key 0, no node submission.',
  source: 'tempo-go pkg/transaction/transaction_vectors_test.go minimal_transaction',
  input: { tx: { chainId: '1', nonce: '0', nonceKey: '0', gas: '21000', maxFeePerGas: '2000000000', maxPriorityFeePerGas: '1000000000', calls: [{ to: '0x70997970c51812dc3a010c7d01b50e0d17dc79c8', value: '0', data: '0x' }] }, signer: { type: 'secp256k1', privateKey: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80' } },
})

// --- invalid inputs every SDK must refuse to sign ---------------------------
const bad = (name: string, description: string, tx: any) => signCases.push({ name, description, op: 'tx.sign', source: 'ox TxEnvelopeTempo.test.ts assert', input: { tx, signer: { type: 'secp256k1', privateKey: key(name) } }, expectedError: 'invalid_input' })
bad('reject_empty_calls', 'calls = [] is invalid.', base({ calls: [] }))
bad('reject_inverted_validity_window', 'validAfter > validBefore.', base({ validAfter: '2000', validBefore: '1000' }))
bad('reject_equal_validity_window', 'validAfter == validBefore.', base({ validAfter: '1000', validBefore: '1000' }))
bad('reject_tip_above_fee_cap', 'maxPriorityFeePerGas > maxFeePerGas.', base({ maxPriorityFeePerGas: '2', maxFeePerGas: '1' }))

for (const c of signCases) {
  const res: any = handle({ op: c.op, input: c.input })
  if (c.expectedError) {
    if (res.ok) throw new Error(c.name + ': expected an error, reference signed it')
    if (res.error.type !== c.expectedError) throw new Error(c.name + ': reference error ' + JSON.stringify(res.error))
  } else {
    if (!res.ok) throw new Error(c.name + ': ' + JSON.stringify(res.error))
    c.expected = res.value
  }
}

// Funding: the sender, or for sponsored cases only the fee payer.
for (const c of signCases) {
  if (!c.node || !c.expected) continue
  const fund = c.input.feePayerPrivateKey ? [addr(c.input.feePayerPrivateKey)] : [c.expected.sender]
  c.node.fund = fund
  // Who the receipt must name as fee payer: the sponsor, or the sender itself.
  c.node.feePayer = c.input.feePayerPrivateKey ? addr(c.input.feePayerPrivateKey) : c.expected.sender
}

// --- decode: every signed transaction, plus malformed ones -----------------
const decodeCases: Case[] = []
for (const c of signCases) {
  if (!c.expected) continue
  const res: any = handle({ op: 'tx.decode', input: { raw: c.expected.raw } })
  if (!res.ok) throw new Error('decode ' + c.name + ': ' + JSON.stringify(res.error))
  decodeCases.push({ name: 'decode_' + c.name, op: 'tx.decode', description: 'Decode the raw transaction of ' + c.name + '.', source: 'tx.sign ' + c.name, input: { raw: c.expected.raw }, expected: res.value })
}
// Each malformed case mutates its own fresh, funded, never-sent transaction,
// so the node's verdict is about the encoding, not a spent nonce.
const fresh = (name: string) => {
  const pk = key(name + '/sender')
  const res: any = handle({ op: 'tx.sign', input: { tx: base(), signer: { type: 'secp256k1', privateKey: pk } } })
  return { raw: res.value.raw as string, sender: res.value.sender as string }
}
const badDecode = (name: string, description: string, mutate: (raw: string) => string, extra: Partial<Case> = {}) => {
  const f = fresh(name)
  decodeCases.push({ name, description, source: 'new', op: 'tx.decode', input: { raw: mutate(f.raw) }, expectedError: 'decode_error', node: { expect: 'reject', fund: [f.sender] }, ...extra })
}
badDecode('decode_reject_truncated', 'Last byte cut off.', (r) => r.slice(0, -2))
badDecode('decode_reject_trailing_byte', 'An extra 0x00 after the RLP list.', (r) => r + '00')
badDecode('decode_reject_wrong_type', 'Type byte 0x77 instead of 0x76.', (r) => '0x77' + r.slice(4))
badDecode('decode_reject_empty', 'Type byte only.', () => '0x76')
const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n
const flipS = (raw: string) => {
  const sig = raw.slice(-130)
  const r = sig.slice(0, 64), s = BigInt('0x' + sig.slice(64, 128)), v = parseInt(sig.slice(128), 16)
  return raw.slice(0, -130) + r + (N - s).toString(16).padStart(64, '0') + (v === 27 ? '1c' : '1b')
}
badDecode('decode_reject_high_s', 'Sender signature with s > n/2 (the malleable twin).', flipS)
badDecode('decode_reject_bad_v', 'Sender signature with v = 0x1d.', (r) => r.slice(0, -2) + '1d')
decodeCases.push({
  name: 'decode_reject_tempo_ts_fee_payer_trailer', op: 'tx.decode', expectedError: 'decode_error', node: { expect: 'reject' },
  description: 'Legacy tempo.ts fee payer format: RLP + sender + feefeefeefee trailer.', source: 'tempo-go pkg/transaction/golden_test.go TestTempoGoldenFormat',
  input: { raw: '0x76f87582a5bd808502cb417800825dc2dcdb9400000000000000000000000000000000000000008084deadbeefc0800b80808000c0b8417607a2e7bea757dc38093db971a7ec7537a690a698119d629b2eb6bb433315767a20aeb717b10285986bc22f0cbb7e9254a2a1f35d5c29ad5119e8b46e202ec401d47b37bbc34fa57e9a67ae7d0a1496edc88f04bbfeefeefeefee' },
})
{
  // yParity written as 0/1 instead of 27/28: tempo-go's published vectors use this form.
  const f = fresh('decode_yparity_0_1')
  const v = parseInt(f.raw.slice(-2), 16)
  const raw = f.raw.slice(0, -2) + (v - 27).toString(16).padStart(2, '0')
  const res: any = handle({ op: 'tx.decode', input: { raw } })
  decodeCases.push({
    name: 'decode_yparity_0_1', op: 'tx.decode', input: { raw }, description: 'Sender signature v byte as 0/1 instead of 27/28. The node accepts it.',
    source: 'tempo-go pkg/transaction/transaction_vectors_test.go (v = 0x01)',
    ...(res.ok ? { expected: res.value } : { expectedError: 'decode_error' }),
    node: { expect: 'success', fund: [f.sender], knownIssue: { reason: 'The node accepts v = 0/1 but re-encodes it as 27/28 and reports the hash of that re-encoding, so keccak256 of the bytes the client sent (what every SDK returns as the hash) is a hash the node never knows.', problems: ['hash'] } },
  })
}

// --- key authorizations (static) --------------------------------------------
const kaCases: Case[] = []
const ka = (name: string, description: string, source: string, authorization: any) => {
  const input = { authorization, privateKey: key(name + '/root') }
  const res: any = handle({ op: 'key_authorization.sign', input })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  kaCases.push({ name, description, source, op: 'key_authorization.sign', input, expected: res.value })
}
const kid = addr(key('ka/access-key'))
ka('ka_unlimited_no_expiry', 'No expiry, no limits.', 'tempo-go key_authorization_test.go; ox KeyAuthorization.test.ts', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid })
ka('ka_expiry', 'Expiry only.', 'ox KeyAuthorization.test.ts', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, expiry: '1893456000' })
ka('ka_one_limit', 'One one-off limit.', 'ox KeyAuthorization.test.ts', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, limits: [{ token: PATH_USD, limit: '10000000' }] })
ka('ka_two_limits', 'Two tokens.', 'pytempo test_keychain.py', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, expiry: '1893456000', limits: [{ token: PATH_USD, limit: '10000000' }, { token: ALPHA_USD, limit: '1' }] })
ka('ka_no_spending', 'limits = [].', 'tempo-primitives', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, limits: [] })
ka('ka_periodic_limit', 'Limit resetting every 86400 s.', 'tempo-primitives TokenLimit.period', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, limits: [{ token: PATH_USD, limit: '5000000', period: '86400' }] })
ka('ka_p256_key', 'Authorizes a P256 key (keyId is the P256 address).', 'ox KeyAuthorization.test.ts p256', { chainId: CHAIN, keyType: 'p256', keyId: kid })
ka('ka_webauthn_key', 'Authorizes a WebAuthn key.', 'ox KeyAuthorization.test.ts webAuthn', { chainId: CHAIN, keyType: 'webauthn', keyId: kid })
ka('ka_max_limit', 'uint256 max limit.', 'new', { chainId: CHAIN, keyType: 'secp256k1', keyId: kid, limits: [{ token: PATH_USD, limit: (2n ** 256n - 1n).toString() }] })
ka('ka_mainnet_chain', 'Chain 4217 (mainnet).', 'new', { chainId: '4217', keyType: 'secp256k1', keyId: kid })


// --- P256 / WebAuthn / access-key signatures -------------------------------
// Bootstrapped from tempo-go/pytempo testdata/viem (viem 2.57.13, ox 0.14.59).
// Expected signers and accept/reject verdicts are the ones tempo-primitives
// 1.11.0 TempoSignature::recover_signer gives, recorded in those files.
const viemVectors = JSON.parse(readFileSync(new URL('../../vectors/sources/viem/vectors.json', import.meta.url), 'utf8'))
const clientData = JSON.parse(readFileSync(new URL('../../vectors/sources/viem/webauthn_client_data.json', import.meta.url), 'utf8'))
const sigCases: Case[] = []
const viemSource = 'tempo-go/pytempo testdata/viem/vectors.json (viem 2.57.13, ox 0.14.59)'
for (const [name, field, addrField, description] of [
  ['sig_p256', 'p256Signature', 'p256Address', 'P256 root signature (Account.fromP256, RFC 6979, low-s).'],
  ['sig_webauthn', 'webAuthnSignature', 'webAuthnAddress', 'Headless WebAuthn signature, rpId localhost.'],
  ['sig_p256_access_key_v2', 'p256AccessKeySignature', 'rootAddress', 'P256 access key, keychain V2 envelope: signs for the root.'],
  ['sig_webauthn_access_key_v2', 'webAuthnAccessKeySignature', 'rootAddress', 'WebAuthn access key, keychain V2 envelope.'],
  ['sig_p256_keychain_v1', 'p256KeychainV1Signature', 'rootAddress', 'Legacy keychain V1 envelope wrapping a P256 signature.'],
] as const)
  sigCases.push({ name, description, source: viemSource, op: 'signature.recover', input: { hash: viemVectors.hash, signature: viemVectors[field] }, expected: { signer: viemVectors[addrField].toLowerCase() } })
for (const c of clientData.cases)
  sigCases.push({
    name: 'webauthn_client_data_' + c.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
    description: 'WebAuthn signature over an unusual clientDataJSON encoding: ' + c.name + '.',
    source: 'tempo-go/pytempo testdata/viem/webauthn_client_data.json; verdict from tempo-primitives 1.11.0 recover_signer',
    op: 'signature.recover',
    input: { hash: clientData.hash, signature: c.envelope },
    ...(c.accept ? { expected: { signer: clientData.address.toLowerCase() } } : { expectedError: 'verification_error' }),
  })

// viem's signed transactions (chain 42431): decode only.
for (const [name, field] of [['decode_viem_p256_root_tx', 'p256RootTransaction'], ['decode_viem_webauthn_root_tx', 'webAuthnRootTransaction'], ['decode_viem_p256_access_key_tx', 'p256AccessKeyTransaction']] as const) {
  const raw = viemVectors[field].serialized
  const res: any = handle({ op: 'tx.decode', input: { raw } })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  if (res.value.sender !== viemVectors[field].sender.toLowerCase()) throw new Error(name + ': sender ' + res.value.sender)
  decodeCases.push({ name, op: 'tx.decode', description: 'Decode viem\'s ' + field + '.', source: viemSource, input: { raw }, expected: res.value })
}

// P256 and WebAuthn transactions on the dev chain, signed with viem's tempo
// accounts, decoded by every SDK and submitted to the node.
{
  const p256Pk = key('p256-root/key')
  const waPk = key('webauthn-root/key')
  const accessRootPk = key('p256-access/root')
  const accessRoot = TempoAccount.fromSecp256k1(accessRootPk)
  const waAccessRootPk = key('webauthn-access/root')
  const waAccessRoot = TempoAccount.fromSecp256k1(waAccessRootPk)
  const rpId = 'localhost', origin = 'http://localhost'
  const p256Acct = TempoAccount.fromP256(p256Pk)
  const waAcct = TempoAccount.fromHeadlessWebAuthn(waPk, { rpId, origin })
  const p256Access = TempoAccount.fromP256(key('p256-access/key'), { access: accessRoot })
  const waAccess = TempoAccount.fromHeadlessWebAuthn(key('webauthn-access/key'), { rpId, origin, access: waAccessRoot })
  const envBase = { chainId: 1337, calls: [{ to: PATH_USD, data: transferCall().data, value: 0n }], gas: 2000000n, maxFeePerGas: 50000000000n, maxPriorityFeePerGas: 1000000000n, nonce: 0n, nonceKey: 0n }
  const txs: [string, string, any, any, string][] = [
    ['decode_p256_root_tx', 'P256 root account sends a pathUSD transfer.', p256Acct, {}, p256Acct.address],
    ['decode_webauthn_root_tx', 'WebAuthn root account sends a pathUSD transfer.', waAcct, {}, waAcct.address],
    ['decode_p256_access_key_tx', 'P256 access key, authorized in the same transaction by its secp256k1 root, signs for the root (keychain V2).', p256Access,
      { keyAuthorization: await TempoAccount.signKeyAuthorization(accessRoot, { key: p256Access, chainId: 1337n }) }, accessRoot.address],
    ['decode_webauthn_access_key_tx', 'WebAuthn access key, authorized in the same transaction, signs for its root (keychain V2).', waAccess,
      { keyAuthorization: await TempoAccount.signKeyAuthorization(waAccessRoot, { key: waAccess, chainId: 1337n }) }, waAccessRoot.address],
  ]
  for (const [name, description, acct, extra, sender] of txs) {
    const env = TxEnvelopeTempo.from({ ...envBase, ...extra } as any)
    const signature = await acct.sign({ hash: TxEnvelopeTempo.getSignPayload(env) })
    const raw = TxEnvelopeTempo.serialize(env, { signature: SignatureEnvelope.from(signature) })
    const res: any = handle({ op: 'tx.decode', input: { raw } })
    if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
    if (res.value.sender !== sender.toLowerCase()) throw new Error(name + ': sender ' + res.value.sender + ' want ' + sender)
    decodeCases.push({ name, op: 'tx.decode', description, source: 'viem/tempo Account (fromP256, fromHeadlessWebAuthn, access keys) as in testdata/viem/gen.mjs', input: { raw }, expected: res.value,
      node: { expect: 'success', fund: [sender.toLowerCase()], feePayer: sender.toLowerCase() } })
  }
}


const write = (file: string, cases: Case[], description: string) =>
  writeFileSync(new URL('../../vectors/' + file, import.meta.url), JSON.stringify({ description, generatedBy: 'adapters/typescript/generate.ts (ox 0.14.59)', cases }, null, 2) + '\n')
write('transactions.json', signCases, 'tx.sign: signing, sponsorship, key authorizations and keychain access keys. Cases with node run in order on one fresh dev node.')
write('decode.json', decodeCases, 'tx.decode: every signed transaction from transactions.json, plus malformed encodings every SDK and the node must reject.')
write('signatures.json', sigCases, 'signature.recover: P256, WebAuthn and access-key (keychain V1/V2) envelopes, and WebAuthn clientDataJSON edge cases, with tempo-primitives verdicts.')
write('key-authorizations.json', kaCases, 'key_authorization.sign: signing hash, signature envelope and RLP of signed key authorizations.')
console.log('signatures', sigCases.length, 'transactions', signCases.length, 'decode', decodeCases.length, 'key-authorizations', kaCases.length)

