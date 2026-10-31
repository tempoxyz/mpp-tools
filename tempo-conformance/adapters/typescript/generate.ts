// Generates ../../vectors/*.json (the MPP vector format: version, description,
// spec_ref, commands.operation, scenarios) from the case definitions below, with
// expected outputs from the TypeScript reference adapter. Run: node generate.ts
//
// Skips are maintained in the vector files themselves: each scenario's adapters,
// sdkVersions and skipReasons are carried over by name when the files are
// regenerated, so edit them in vectors/*.json.
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { Account as TempoAccount } from 'viem/tempo'
import { KeyAuthorization, SignatureEnvelope, TxEnvelopeTempo } from 'ox/tempo'
import { Hash, Hex, Secp256k1, Address, AbiFunction, Rlp } from 'ox'
import { handle } from './adapter.ts'

const OX_VERSION = JSON.parse(readFileSync(new URL('./node_modules/ox/package.json', import.meta.url), 'utf8')).version
const key = (label: string) => Hash.keccak256(Hex.fromString('tempo-conformance/' + label))
const addr = (pk: string) => Address.fromPublicKey(Secp256k1.getPublicKey({ privateKey: pk as Hex.Hex })).toLowerCase()

const CHAIN = '1337'
const PATH_USD = '0x20c0000000000000000000000000000000000000'
const ALPHA_USD = '0x20c0000000000000000000000000000000000001'
const RECIPIENT = '0x000000000000000000000000000000000000beef'
const OTHER = '0x000000000000000000000000000000000000cafe'
const TRANSFER = '0xa9059cbb'
const APPROVE = '0x095ea7b3'
const MAX_U256 = (2n ** 256n - 1n).toString()
const transfer = AbiFunction.from('function transfer(address to, uint256 amount) returns (bool)')
const transferCall = (amount = 1n, to = RECIPIENT) => ({ to: PATH_USD, value: '0', data: AbiFunction.encodeData(transfer, [to as Hex.Hex, amount]) })
// Counter contract init code from tempo-go tests/testdata/Counter.hex
const COUNTER_INIT = '0x6080604052348015600e575f5ffd5b5060e180601a5f395ff3fe6080604052348015600e575f5ffd5b50600436106030575f3560e01c80638381f58a146034578063d09de08a14604e575b5f5ffd5b603a6056565b604051604591906078565b60405180910390f35b6054605b565b005b5f5481565b5f80549080606883609c565b9190505550565b5f819050919050565b607f81606e565b82525050565b5f6020820190506096825f8301606f5b92915050565b5f60a282606e565b91507fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff820360ce5760cd60c0565b5b60018201905091905056'
const create = { to: null, value: '0', data: COUNTER_INIT }
// The runner funds only the fee payer of sponsored cases, so their calls move no tokens.
const noop = [{ to: RECIPIENT, value: '0', data: '0x' }]

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

type Node = { expect: string; fund?: string[]; feePayer?: string; requires?: string[]; knownIssue?: { reason: string; problems: string[] } }
type Case = { name: string; op: string; description: string; source: string; tags?: string[]; input: any; expectError?: string; expected?: any; node?: Node }

// --- tags --------------------------------------------------------------------
const authTags = (a: any): string[] => [
  ...(a.allowedCalls != null ? ['call-scopes'] : []),
  ...(a.witness != null ? ['witness'] : []),
  ...(a.isAdmin ? ['admin-key'] : []),
  ...(a.account != null ? ['account-binding'] : []),
  ...(a.limits?.some((l: any) => l.period && l.period !== '0') ? ['periodic-limit'] : []),
  ...(Array.isArray(a.limits) && a.limits.length === 0 ? ['no-spending'] : []),
  ...(a.expiry != null ? ['expiry'] : []),
  ...(a.keyType !== 'secp256k1' ? [a.keyType] : []),
]
function tagsFor(c: Case): string[] {
  const t: string[] = []
  if (c.op === 'tx.sign') {
    const { tx, signer } = c.input
    if (c.input.feePayerPrivateKey) t.push('sponsored')
    if (signer.type === 'keychain') t.push('keychain', 'keychain-' + (signer.version ?? 'v2'))
    if (tx.keyAuthorization) t.push('key-authorization', ...authTags(tx.keyAuthorization.authorization))
    if (tx.calls.some((x: any) => x.to === null)) t.push('contract-creation')
    if (tx.calls.length > 1) t.push('batch')
    if (tx.nonceKey === MAX_U256) t.push('expiring-nonce')
    else if (tx.nonceKey !== '0') t.push('nonce-key')
    if (tx.validBefore != null || tx.validAfter != null) t.push('validity-window')
    if (tx.feeToken) t.push('fee-token')
    if (tx.accessList?.length) t.push('access-list')
  } else if (c.op === 'key_authorization.sign') {
    t.push(...authTags(c.input.authorization))
  }
  if (c.expectError) t.push(c.op === 'tx.decode' ? 'malformed' : c.expectError.replace(/_/g, '-'))
  if (c.node) t.push('node', 'node-' + c.node.expect)
  return [...new Set([...t, ...(c.tags ?? [])])]
}

// --- tx.sign -----------------------------------------------------------------
const signCases: Case[] = []
const sign = (name: string, description: string, source: string, tx: any, opts: { signer?: any; feePayer?: boolean; node?: string; requires?: string; expectError?: string; tags?: string[] } = {}) => {
  const signer = opts.signer ?? { type: 'secp256k1', privateKey: key(name + '/sender') }
  signCases.push({
    name, description, source, op: 'tx.sign', tags: opts.tags,
    input: { tx, signer, ...(opts.feePayer ? { feePayerPrivateKey: key(name + '/fee-payer') } : {}) },
    ...(opts.expectError ? { expectError: opts.expectError } : {}),
    ...(opts.node === 'none' || opts.expectError ? {} : { node: { expect: opts.node ?? 'success', ...(opts.requires ? { requires: [opts.requires] } : {}) } }),
  })
}
const keyAuth = (rootPk: string, accessPk: string, extra: Record<string, unknown> = {}) =>
  ({ privateKey: rootPk, authorization: { chainId: CHAIN, keyType: 'secp256k1', keyId: addr(accessPk), expiry: null, limits: null, ...extra } })
const keychain = (accessPk: string, rootPk: string, version?: string) =>
  ({ type: 'keychain', accessKeyPrivateKey: accessPk, rootAccount: addr(rootPk), ...(version ? { version } : {}) })

// Transactions, all submitted to the local node.
sign('minimal', 'One TIP-20 transfer call, protocol nonce, no fee token.', 'tempo-go transaction_vectors_test.go minimal_transaction; ox TxEnvelopeTempo.test.ts serialize minimal', base())
sign('second_nonce', 'Nonce 1 for the same sender as a prior case in this file (runner sends nonce 0 first).', 'tempo-go transaction_vectors_test.go', base({ nonce: '1' }), { signer: { type: 'secp256k1', privateKey: key('minimal/sender') }, requires: 'minimal' })
sign('with_fee_token', 'Explicit fee token preference (pathUSD).', 'tempo-go with_fee_token; ox serialize feeToken', base({ feeToken: PATH_USD }))
sign('fee_token_alpha_usd', 'Fee token set to a second TIP-20 (AlphaUSD); the sender holds none, so the node rejects it.', 'new', base({ feeToken: ALPHA_USD }), { node: 'reject' })
sign('multiple_calls_batch', 'Three calls executed atomically.', 'tempo-go multiple_calls_batch; ox serialize multiple calls', base({ calls: [transferCall(1n), transferCall(2n, OTHER), transferCall(3n)] }))
sign('zero_value_empty_data', 'Call to an EOA with zero value and empty data.', 'tempo-go single_call_zero_value', base({ calls: [{ to: RECIPIENT, value: '0', data: '0x' }] }))
sign('call_with_value', 'Call with non-zero value; Tempo has no native token, so the node rejects it.', 'tempo-go single_call_with_value', base({ calls: [{ to: RECIPIENT, value: '1', data: '0x' }] }), { node: 'reject' })
sign('contract_creation', 'First call deploys Counter (to = null).', 'tempo-go tests/testdata/Counter.hex', base({ gas: '3000000', calls: [create] }))
sign('contract_creation_then_call', 'CREATE as the first call followed by a transfer: the only allowed position for CREATE in a batch.', 'tempo-primitives tempo_transaction.rs test_validate_create_first_then_calls_ok', base({ gas: '3000000', calls: [create, transferCall()] }))
sign('access_list', 'Access list with one address and one storage key.', 'tempo-go transaction_with_all_fields; ox deserialize accessList', base({ accessList: [{ address: PATH_USD, storageKeys: ['0x0000000000000000000000000000000000000000000000000000000000000001'] }] }))
sign('user_nonce_key_1', '2D nonce: nonce key 1.', 'tempo-go user_nonce_key_1; ox deserialize nonceKey', base({ nonceKey: '1' }))
sign('user_nonce_key_large', '2D nonce: 128-bit nonce key.', 'tempo-go user_nonce_key_large', base({ nonceKey: '340282366920938463463374607431768211455' }))
sign('valid_before', 'Expires in 2100.', 'tempo-go with_valid_before; ox deserialize validBefore', base({ validBefore: '4102444800' }))
sign('valid_after', 'Valid after 1 second past epoch (already valid).', 'tempo-go with_valid_after; ox deserialize validAfter', base({ validAfter: '1' }))
sign('validity_window', 'validAfter < validBefore, both set.', 'tempo-go with_validity_window', base({ validAfter: '1', validBefore: '4102444800' }))
sign('expired_valid_before', 'validBefore in the past: signs, but the node rejects it.', 'new', base({ validBefore: '1000' }), { node: 'reject' })
sign('wrong_chain_id', 'Signed for chain 1; the dev node (1337) rejects it.', 'tempo-go minimal_transaction (chain 1)', base({ chainId: '1' }), { node: 'reject' })
sign('high_gas_prices', '100/200 gwei.', 'tempo-go high_gas_prices', base({ maxPriorityFeePerGas: '100000000000', maxFeePerGas: '200000000000' }))
sign('expiring_nonce_without_valid_before', 'Expiring nonce (nonce key 2^256-1, nonce 0) without validBefore: the node rejects it, since an expiring-nonce transaction must expire within its window.', 'tempo-primitives TEMPO_EXPIRING_NONCE_KEY (tempo_transaction.rs is_expiring_nonce_tx)', base({ nonceKey: MAX_U256 }), { node: 'reject' })
sign('expiring_nonce_valid_before_too_far', 'Expiring nonce with validBefore in 2100, far past the expiring-nonce window: the node rejects it.', 'tempo-primitives TEMPO_EXPIRING_NONCE_KEY', base({ nonceKey: MAX_U256, validBefore: '4102444800' }), { node: 'reject' })
sign('sponsored', 'Fee payer co-signs and pays; the sender holds no tokens.', 'tempo-go sponsored_transaction; ox serialize with feePayerSignature', base({ calls: noop }), { feePayer: true })
sign('sponsored_with_fee_token', 'Fee payer picks the fee token; the sender payload leaves it out.', 'tempo-go sponsored_with_fee_token', base({ calls: noop, feeToken: PATH_USD }), { feePayer: true })

const accessKey = key('keychain/access-key')
const root = key('keychain/root')
sign('key_authorization', 'Root signs a tx that also authorizes a secp256k1 access key with a pathUSD limit.', 'ox serialize keyAuthorization (secp256k1); tempo-go keychain SignAndAttach',
  base({ keyAuthorization: keyAuth(root, accessKey, { limits: [{ token: PATH_USD, limit: '1000000000' }] }) }), { signer: { type: 'secp256k1', privateKey: root } })
sign('keychain_access_key', 'The access key authorized above signs for the root account (V2 keychain signature).', 'tempo-go keychain SignWithAccessKey; ox SignatureEnvelope keychain',
  base({ nonce: '1' }), { signer: keychain(accessKey, root), requires: 'key_authorization' })
sign('keychain_sponsored', 'Access key signs, fee payer co-signs.', 'new',
  base({ nonce: '2' }), { signer: keychain(accessKey, root), feePayer: true, requires: 'keychain_access_key' })
sign('keychain_unauthorized_key', 'A key the root never authorized signs for it; the node rejects it.', 'new',
  base({ nonce: '3' }), { signer: keychain(key('keychain/stranger'), root), node: 'reject', requires: 'keychain_sponsored' })
sign('keychain_v1_access_key', 'The authorized access key signs with the legacy V1 keychain envelope (0x03, inner signature over the plain signing hash), deprecated at T1C: the node rejects it.', 'ox SignatureEnvelope keychain v1; tempo-primitives KeychainSignature',
  base({ nonce: '3' }), { signer: keychain(accessKey, root, 'v1'), node: 'reject', requires: 'keychain_sponsored' })

const accessKey2 = key('keychain2/access-key')
const root2 = key('keychain2/root')
sign('key_authorization_expiry_periodic_limit', 'Access key with an expiry and a daily pathUSD limit.', 'tempo-primitives TokenLimit.period; ox KeyAuthorization period',
  base({ keyAuthorization: keyAuth(root2, accessKey2, { expiry: '4102444800', limits: [{ token: PATH_USD, limit: '5000000', period: '86400' }] }) }), { signer: { type: 'secp256k1', privateKey: root2 } })
sign('keychain_periodic_key', 'The periodic-limit access key signs for its root.', 'new',
  base({ nonce: '1' }), { signer: keychain(accessKey2, root2), requires: 'key_authorization_expiry_periodic_limit' })

const accessKey3 = key('keychain3/access-key')
const root3 = key('keychain3/root')
sign('key_authorization_no_spending', 'Access key with limits = [] (no spending allowed).', 'tempo-primitives KeyAuthorization.limits Some([])',
  base({ keyAuthorization: keyAuth(root3, accessKey3, { limits: [] }) }), { signer: { type: 'secp256k1', privateKey: root3 } })
sign('keychain_no_spending_transfer', 'The no-spending key tries a pathUSD transfer: the pool rejects it (SpendingLimitExceeded).', 'new',
  base({ nonce: '1' }), { signer: keychain(accessKey3, root3), node: 'reject', requires: 'key_authorization_no_spending' })

// TIP-1011 call scopes: a key that may only call pathUSD transfer() to RECIPIENT.
const scopedKey = key('scoped/access-key')
const scopedRoot = key('scoped/root')
sign('key_authorization_call_scopes', 'Root authorizes an access key scoped (TIP-1011) to pathUSD transfer() with a recipient allowlist of one address.', 'ox KeyAuthorization.test.ts scopes; tempo-primitives CallScope/SelectorRule',
  base({ gas: '5000000', keyAuthorization: keyAuth(scopedRoot, scopedKey, { limits: [{ token: PATH_USD, limit: '1000000000' }], allowedCalls: [{ target: PATH_USD, selectorRules: [{ selector: TRANSFER, recipients: [RECIPIENT] }] }] }) }),
  { signer: { type: 'secp256k1', privateKey: scopedRoot } })
sign('keychain_scoped_allowed_call', 'The scoped key calls pathUSD transfer() to its allowed recipient.', 'new',
  base({ nonce: '1' }), { signer: keychain(scopedKey, scopedRoot), requires: 'key_authorization_call_scopes' })
sign('keychain_scoped_other_recipient', 'The scoped key calls pathUSD transfer() to a recipient outside its allowlist: the call reverts.', 'new',
  base({ nonce: '2', calls: [transferCall(1n, OTHER)] }), { signer: keychain(scopedKey, scopedRoot), node: 'revert', requires: 'keychain_scoped_allowed_call' })
sign('keychain_scoped_other_selector', 'The scoped key calls pathUSD approve(), a selector outside its scope: the call reverts.', 'new',
  base({ nonce: '3', calls: [{ to: PATH_USD, value: '0', data: AbiFunction.encodeData(AbiFunction.from('function approve(address spender, uint256 amount) returns (bool)'), [RECIPIENT, 1n]) }] }),
  { signer: keychain(scopedKey, scopedRoot), node: 'revert', requires: 'keychain_scoped_other_recipient' })

const denyKey = key('deny-all/access-key')
const denyRoot = key('deny-all/root')
sign('key_authorization_deny_all_calls', 'Root authorizes an access key with allowedCalls = [] (scoped mode, no call allowed).', 'tempo-primitives KeyAuthorization.allowed_calls Some([])',
  base({ keyAuthorization: keyAuth(denyRoot, denyKey, { allowedCalls: [] }) }), { signer: { type: 'secp256k1', privateKey: denyRoot } })
sign('keychain_deny_all_calls_transfer', 'The deny-all key tries a pathUSD transfer: the node includes it and the call reverts.', 'new',
  base({ nonce: '1' }), { signer: keychain(denyKey, denyRoot), node: 'revert', requires: 'key_authorization_deny_all_calls' })

sign('key_authorization_witness', 'Key authorization carrying a TIP-1053 witness.', 'ox KeyAuthorization witness; tempo-primitives KeyAuthorization.witness',
  base({ keyAuthorization: keyAuth(key('witness/root'), key('witness/access-key'), { witness: Hash.keccak256(Hex.fromString('tempo-conformance/witness')) }) }), { signer: { type: 'secp256k1', privateKey: key('witness/root') } })
sign('key_authorization_zero_witness', 'Key authorization with the all-zero witness, which is a present witness, not an absent one.', 'tempo-primitives test_decode_accepts_explicit_zero_witness',
  base({ keyAuthorization: keyAuth(key('zero-witness/root'), key('zero-witness/access-key'), { witness: '0x' + '00'.repeat(32) }) }), { signer: { type: 'secp256k1', privateKey: key('zero-witness/root') } })
sign('key_authorization_admin_key', 'Root authorizes a TIP-1049 admin access key bound to its own account.', 'ox KeyAuthorization isAdmin/account; tempo-primitives KeyAuthorization.is_admin',
  base({ keyAuthorization: keyAuth(key('admin/root'), key('admin/access-key'), { isAdmin: true, account: addr(key('admin/root')) }) }), { signer: { type: 'secp256k1', privateKey: key('admin/root') } })
sign('key_authorization_account_bound', 'Root-signed key authorization bound (TIP-1049 account field) to the root account itself.', 'tempo-primitives KeyAuthorization.account',
  base({ keyAuthorization: keyAuth(key('account-bound/root'), key('account-bound/access-key'), { account: addr(key('account-bound/root')) }) }), { signer: { type: 'secp256k1', privateKey: key('account-bound/root') } })
sign('key_authorization_wrong_account', 'Key authorization bound to an account other than the transaction sender: the node rejects it.', 'tempo-primitives KeyAuthorization.account',
  base({ keyAuthorization: keyAuth(key('wrong-account/root'), key('wrong-account/access-key'), { account: OTHER }) }), { signer: { type: 'secp256k1', privateKey: key('wrong-account/root') }, node: 'reject' })
sign('key_authorization_mixed_limits', 'One one-off pathUSD limit and one periodic AlphaUSD limit in the same authorization.', 'tempo-primitives TokenLimit.period',
  base({ keyAuthorization: keyAuth(key('mixed/root'), key('mixed/access-key'), { limits: [{ token: PATH_USD, limit: '1000000', period: '0' }, { token: ALPHA_USD, limit: '5000000', period: '3600' }] }) }), { signer: { type: 'secp256k1', privateKey: key('mixed/root') } })

// An access key that signs the transaction carrying its own authorization.
const selfAuth = (name: string) => keyAuth(key(name + '/root'), key(name + '/access-key'))
sign('keychain_self_authorized_sponsored', 'An access key signs (keychain V2) the transaction that carries its own root-signed authorization, and a fee payer co-signs.', 'viem/tempo Account access keys; ox keyAuthorization + keychain',
  base({ calls: noop, keyAuthorization: selfAuth('self-sponsored') }), { signer: keychain(key('self-sponsored/access-key'), key('self-sponsored/root')), feePayer: true })
sign('keychain_self_authorized', 'An access key signs the transaction carrying its own authorization; the root pays.', 'viem/tempo Account access keys',
  base({ keyAuthorization: selfAuth('self') }), { signer: keychain(key('self/access-key'), key('self/root')) })
sign('keychain_v1_self_authorized', 'Same as keychain_self_authorized with a legacy V1 keychain envelope: the node rejects it.', 'ox SignatureEnvelope keychain v1',
  base({ keyAuthorization: selfAuth('self-v1') }), { signer: keychain(key('self-v1/access-key'), key('self-v1/root'), 'v1'), node: 'reject' })
sign('keychain_contract_creation', 'An access key signs a transaction whose call is a CREATE: the node rejects it (access keys cannot deploy contracts).', 'tempo-primitives / node keychain validation',
  base({ gas: '3000000', calls: [create], keyAuthorization: selfAuth('keychain-create') }), { signer: keychain(key('keychain-create/access-key'), key('keychain-create/root')), node: 'reject' })

// Static cross-checks against vectors published in the SDK repos.
signCases.push({
  name: 'tempo_go_minimal_transaction', op: 'tx.sign', description: 'tempo-go golden: chain 1, anvil key 0, no node submission.',
  source: 'tempo-go pkg/transaction/transaction_vectors_test.go minimal_transaction',
  input: { tx: { chainId: '1', nonce: '0', nonceKey: '0', gas: '21000', maxFeePerGas: '2000000000', maxPriorityFeePerGas: '1000000000', calls: [{ to: '0x70997970c51812dc3a010c7d01b50e0d17dc79c8', value: '0', data: '0x' }] }, signer: { type: 'secp256k1', privateKey: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80' } },
})

// Invalid inputs every SDK must refuse to sign.
const bad = (name: string, description: string, tx: any, source = 'ox TxEnvelopeTempo.test.ts assert') =>
  sign(name, description, source, tx, { signer: { type: 'secp256k1', privateKey: key(name) }, expectError: 'invalid_input' })
bad('reject_empty_calls', 'calls = [] is invalid.', base({ calls: [] }))
bad('reject_inverted_validity_window', 'validAfter > validBefore.', base({ validAfter: '2000', validBefore: '1000' }))
bad('reject_equal_validity_window', 'validAfter == validBefore.', base({ validAfter: '1000', validBefore: '1000' }))
bad('reject_tip_above_fee_cap', 'maxPriorityFeePerGas > maxFeePerGas.', base({ maxPriorityFeePerGas: '2', maxFeePerGas: '1' }))
bad('reject_create_not_first_call', 'CREATE as the second call of a batch: only the first call may be a CREATE.', base({ gas: '3000000', calls: [transferCall(), create] }), 'tempo-primitives tempo_transaction.rs validate_calls; test_validate_create_not_first_call_fails')
bad('reject_two_creates', 'Two CREATE calls in one batch.', base({ gas: '3000000', calls: [create, create] }), 'tempo-primitives tempo_transaction.rs validate_calls; test_validate_multiple_creates_fails')

const run = (op: string, input: any): any => handle({ op, input })
for (const c of signCases) {
  const res = run(c.op, c.input)
  if (c.expectError) {
    if (res.ok) console.warn('reference disagrees: ' + c.name + ' must fail with ' + c.expectError + ', ox signs it')
    else if (res.error.type !== c.expectError) throw new Error(c.name + ': reference error ' + JSON.stringify(res.error))
  } else {
    if (!res.ok) throw new Error(c.name + ': ' + JSON.stringify(res.error))
    c.expected = res.value
  }
}

// Funding: the sender, or for sponsored cases only the fee payer.
for (const c of signCases) {
  if (!c.node || !c.expected) continue
  c.node.fund = c.input.feePayerPrivateKey ? [addr(c.input.feePayerPrivateKey)] : [c.expected.sender]
  // Who the receipt must name as fee payer: the sponsor, or the sender itself.
  c.node.feePayer = c.input.feePayerPrivateKey ? addr(c.input.feePayerPrivateKey) : c.expected.sender
}

// --- tx.decode -----------------------------------------------------------------
const decodeCases: Case[] = []
for (const c of signCases) {
  if (!c.expected) continue
  const res = run('tx.decode', { raw: c.expected.raw })
  if (!res.ok) throw new Error('decode ' + c.name + ': ' + JSON.stringify(res.error))
  decodeCases.push({ name: 'decode_' + c.name, op: 'tx.decode', description: 'Decode the raw transaction of ' + c.name + '.', source: 'tx.sign ' + c.name, tags: tagsFor(c).filter((t) => !t.startsWith('node')), input: { raw: c.expected.raw }, expected: res.value })
}
// Each malformed case mutates its own fresh, funded, never-sent transaction,
// so the node's verdict is about the encoding, not a spent nonce.
const fresh = (name: string, tx: any = base()) => {
  const pk = key(name + '/sender')
  const res = run('tx.sign', { tx, signer: { type: 'secp256k1', privateKey: pk } })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  return { raw: res.value.raw as string, sender: res.value.sender as string, pk }
}
const badDecode = (name: string, description: string, raw: string, sender: string, extra: Partial<Case> = {}) =>
  decodeCases.push({ name, description, source: 'new', op: 'tx.decode', input: { raw }, expectError: 'decode_error', node: { expect: 'reject', fund: [sender] }, ...extra })
const mutate = (name: string, description: string, fn: (raw: string) => string, extra: Partial<Case> = {}) => {
  const f = fresh(name)
  badDecode(name, description, fn(f.raw), f.sender, extra)
}
mutate('decode_reject_truncated', 'Last byte cut off.', (r) => r.slice(0, -2))
mutate('decode_reject_trailing_byte', 'An extra 0x00 after the RLP list.', (r) => r + '00')
mutate('decode_reject_wrong_type', 'Type byte 0x77 instead of 0x76.', (r) => '0x77' + r.slice(4))
mutate('decode_reject_empty', 'Type byte only.', () => '0x76')
const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n
const flipS = (raw: string) => {
  const sig = raw.slice(-130)
  const r = sig.slice(0, 64), s = BigInt('0x' + sig.slice(64, 128)), v = parseInt(sig.slice(128), 16)
  return raw.slice(0, -130) + r + (N - s).toString(16).padStart(64, '0') + (v === 27 ? '1c' : '1b')
}
mutate('decode_reject_high_s', 'Sender signature with s > n/2 (the malleable twin).', flipS)
mutate('decode_reject_bad_v', 'Sender signature with v = 0x1d.', (r) => r.slice(0, -2) + '1d')
decodeCases.push({
  name: 'decode_reject_tempo_ts_fee_payer_trailer', op: 'tx.decode', expectError: 'decode_error', node: { expect: 'reject' },
  description: 'Legacy tempo.ts fee payer format: RLP + sender + feefeefeefee trailer.', source: 'tempo-go pkg/transaction/golden_test.go TestTempoGoldenFormat',
  input: { raw: '0x76f87582a5bd808502cb417800825dc2dcdb9400000000000000000000000000000000000000008084deadbeefc0800b80808000c0b8417607a2e7bea757dc38093db971a7ec7537a690a698119d629b2eb6bb433315767a20aeb717b10285986bc22f0cbb7e9254a2a1f35d5c29ad5119e8b46e202ec401d47b37bbc34fa57e9a67ae7d0a1496edc88f04bbfeefeefeefee' },
})
{
  // yParity written as 0/1 instead of 27/28: tempo-go's published vectors used this form.
  const f = fresh('decode_yparity_0_1')
  const v = parseInt(f.raw.slice(-2), 16)
  const raw = f.raw.slice(0, -2) + (v - 27).toString(16).padStart(2, '0')
  const res = run('tx.decode', { raw })
  decodeCases.push({
    name: 'decode_yparity_0_1', op: 'tx.decode', input: { raw }, description: 'Sender signature v byte as 0/1 instead of 27/28. The node accepts it.',
    source: 'tempo-go pkg/transaction/transaction_vectors_test.go (v = 0x01)',
    ...(res.ok ? { expected: res.value } : { expectError: 'decode_error' }),
    node: { expect: 'success', fund: [f.sender], knownIssue: { reason: 'The node accepts v = 0/1 but re-encodes it as 27/28 and reports the hash of that re-encoding, so keccak256 of the bytes the client sent (what every SDK returns as the hash) is a hash the node never knows.', problems: ['hash'] } },
  })
}

// Signed transactions the SDKs must not produce: built with ox's serializer,
// which does not check CREATE placement.
const rawSigned = (name: string, tx: any) => {
  const pk = key(name + '/sender')
  const env = TxEnvelopeTempo.from({ chainId: 1337, nonce: 0n, nonceKey: 0n, gas: BigInt(tx.gas), maxFeePerGas: BigInt(tx.maxFeePerGas), maxPriorityFeePerGas: BigInt(tx.maxPriorityFeePerGas),
    calls: tx.calls.map((c: any) => ({ ...(c.to ? { to: c.to } : {}), value: BigInt(c.value), data: c.data })) } as any)
  const signature = SignatureEnvelope.from(Secp256k1.sign({ payload: TxEnvelopeTempo.getSignPayload(env), privateKey: pk }))
  return { raw: TxEnvelopeTempo.serialize(env, { signature }) as string, sender: addr(pk) }
}
{
  const f = rawSigned('decode_reject_create_not_first_call', base({ gas: '3000000', calls: [transferCall(), create] }))
  badDecode('decode_reject_create_not_first_call', 'A signed transaction whose second call is a CREATE: decoders must reject it, as the node does.', f.raw, f.sender, { source: 'tempo-primitives tempo_transaction.rs validate_calls (AASigned decode validates)' })
}

// Key authorizations with non-canonical optional fields, inside an otherwise valid
// transaction. tempo-primitives decodes them with #[rlp(trailing(canonical))]:
// a trailing absent field must be omitted, not written as 0x80.
const withKeyAuth = (name: string, extra: Record<string, unknown>) => {
  const pk = key(name + '/sender')
  const res = run('tx.sign', { tx: base({ keyAuthorization: keyAuth(pk, key(name + '/access-key'), extra) }), signer: { type: 'secp256k1', privateKey: pk } })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  return { raw: res.value.raw as string, sender: res.value.sender as string }
}
const mutateKeyAuth = (raw: string, fn: (auth: any[]) => any[]) => {
  const tuple = Rlp.toHex(('0x' + raw.slice(4)) as Hex.Hex) as any[]
  const ka = tuple[tuple.length - 2] as any[]
  tuple[tuple.length - 2] = [fn([...ka[0]]), ka[1]]
  return '0x76' + Rlp.fromHex(tuple as any).slice(2)
}
{
  const f = withKeyAuth('decode_reject_key_authorization_trailing_empty_expiry', {})
  badDecode('decode_reject_key_authorization_trailing_empty_expiry', 'Key authorization [chainId, keyType, keyId, 0x80]: the absent expiry is written as a trailing 0x80 instead of being omitted.', mutateKeyAuth(f.raw, (a) => [...a, '0x']), f.sender,
    { source: 'tempo-primitives key_authorization.rs KeyAuthorizationWire #[rlp(trailing(canonical))]; test_decode_rejects_explicit_absent_witness_field' })
}
{
  const f = withKeyAuth('decode_reject_token_limit_explicit_zero_period', { limits: [{ token: PATH_USD, limit: '1000' }] })
  badDecode('decode_reject_token_limit_explicit_zero_period', 'Token limit [token, limit, 0x80]: a one-off limit with its zero period written out instead of omitted.', mutateKeyAuth(f.raw, (a) => { a[4] = [[...a[4][0], '0x']]; return a }), f.sender,
    { source: 'tempo-primitives key_authorization.rs TokenLimitWire #[rlp(trailing(canonical))]' })
}
{
  const f = withKeyAuth('decode_reject_admin_marker_not_one', { isAdmin: true, account: addr(key('decode_reject_admin_marker_not_one/sender')) })
  badDecode('decode_reject_admin_marker_not_one', 'Key authorization whose isAdmin marker is 0x02: TIP-1049 allows only 0x01.', mutateKeyAuth(f.raw, (a) => { a[7] = '0x02'; return a }), f.sender,
    { source: 'ox KeyAuthorization InvalidAdminMarkerError; tempo-primitives KeyAuthorizationWire.is_admin' })
}

{
  // A well-formed transaction the node refuses: its key authorization makes an admin key with a limit.
  const name = 'decode_admin_key_with_limits'
  const f = withKeyAuth(name, { isAdmin: true, account: addr(key(name + '/sender')), limits: [{ token: PATH_USD, limit: '1' }] })
  const res = run('tx.decode', { raw: f.raw })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  decodeCases.push({ name, op: 'tx.decode', input: { raw: f.raw }, expected: res.value, tags: ['key-authorization', 'admin-key'],
    description: 'Decodes (the encoding is valid), but the node rejects the transaction: admin key authorizations cannot carry expiry, limits or call scopes.',
    source: 'tempo crates/evm/src/handler.rs admin key validation', node: { expect: 'reject', fund: [f.sender] } })
}

// --- key_authorization.sign (static) --------------------------------------------
const kaCases: Case[] = []
const ka = (name: string, description: string, source: string, authorization: any) => {
  const input = { authorization, privateKey: key(name + '/root') }
  kaCases.push({ name, description, source, op: 'key_authorization.sign', input })
}
const kid = addr(key('ka/access-key'))
const kaBase = { chainId: CHAIN, keyType: 'secp256k1', keyId: kid }
ka('ka_unlimited_no_expiry', 'No expiry, no limits.', 'tempo-go key_authorization_test.go; ox KeyAuthorization.test.ts', { ...kaBase })
ka('ka_expiry', 'Expiry only.', 'ox KeyAuthorization.test.ts', { ...kaBase, expiry: '1893456000' })
ka('ka_one_limit', 'One one-off limit.', 'ox KeyAuthorization.test.ts', { ...kaBase, limits: [{ token: PATH_USD, limit: '10000000' }] })
ka('ka_two_limits', 'Two tokens.', 'pytempo test_keychain.py', { ...kaBase, expiry: '1893456000', limits: [{ token: PATH_USD, limit: '10000000' }, { token: ALPHA_USD, limit: '1' }] })
ka('ka_no_spending', 'limits = [].', 'tempo-primitives', { ...kaBase, limits: [] })
ka('ka_periodic_limit', 'Limit resetting every 86400 s.', 'tempo-primitives TokenLimit.period', { ...kaBase, limits: [{ token: PATH_USD, limit: '5000000', period: '86400' }] })
ka('ka_zero_period_limit', 'Explicit period "0": a one-off limit, encoded with the period omitted.', 'tempo-primitives TokenLimitWire (period 0 = omitted)', { ...kaBase, limits: [{ token: PATH_USD, limit: '5000000', period: '0' }] })
ka('ka_mixed_limits', 'One one-off and one periodic limit.', 'tempo-primitives TokenLimit.period', { ...kaBase, limits: [{ token: PATH_USD, limit: '1000000' }, { token: ALPHA_USD, limit: '5000000', period: '3600' }] })
ka('ka_p256_key', 'Authorizes a P256 key (keyId is the P256 address).', 'ox KeyAuthorization.test.ts p256', { ...kaBase, keyType: 'p256' })
ka('ka_webauthn_key', 'Authorizes a WebAuthn key.', 'ox KeyAuthorization.test.ts webAuthn', { ...kaBase, keyType: 'webauthn' })
ka('ka_max_limit', 'uint256 max limit.', 'new', { ...kaBase, limits: [{ token: PATH_USD, limit: MAX_U256 }] })
ka('ka_mainnet_chain', 'Chain 4217 (mainnet).', 'new', { ...kaBase, chainId: '4217' })
ka('ka_call_scope_target_only', 'TIP-1011 call scope: any selector on one target.', 'ox KeyAuthorization.test.ts scopes; tempo-primitives CallScope', { ...kaBase, allowedCalls: [{ target: PATH_USD }] })
ka('ka_call_scope_selector', 'Call scope: transfer() on pathUSD, any recipient.', 'ox KeyAuthorization.test.ts scopes', { ...kaBase, allowedCalls: [{ target: PATH_USD, selectorRules: [{ selector: TRANSFER }] }] })
ka('ka_call_scope_recipients', 'Call scope: transfer() on pathUSD to two allowed recipients.', 'ox KeyAuthorization.test.ts scopes recipients', { ...kaBase, allowedCalls: [{ target: PATH_USD, selectorRules: [{ selector: TRANSFER, recipients: [RECIPIENT, OTHER] }] }] })
ka('ka_call_scopes_multiple', 'Two targets: transfer() and approve() on pathUSD (one restricted to a recipient), and any call to AlphaUSD.', 'tempo-primitives CallScope', { ...kaBase, allowedCalls: [{ target: PATH_USD, selectorRules: [{ selector: TRANSFER, recipients: [RECIPIENT] }, { selector: APPROVE }] }, { target: ALPHA_USD }] })
ka('ka_call_scopes_empty', 'allowedCalls = []: scoped mode with no call allowed.', 'tempo-primitives test_key_authorization_decode_accepts_explicit_deny_all_allowed_calls_field', { ...kaBase, allowedCalls: [] })
ka('ka_witness', 'TIP-1053 witness; the absent expiry, limits and allowedCalls before it are written as 0x80.', 'ox KeyAuthorization witness; tempo-primitives witness placeholders test', { ...kaBase, witness: Hash.keccak256(Hex.fromString('tempo-conformance/ka-witness')) })
ka('ka_zero_witness', 'The all-zero witness is present, not absent.', 'tempo-primitives test_decode_accepts_explicit_zero_witness', { ...kaBase, witness: '0x' + '00'.repeat(32) })
ka('ka_admin', 'TIP-1049 admin access key bound to an account.', 'ox KeyAuthorization isAdmin', { ...kaBase, isAdmin: true, account: addr(key('ka_admin/root')) })
ka('ka_account', 'Root-signed authorization bound to an account (TIP-1049 account field) without admin rights.', 'tempo-primitives KeyAuthorization.account', { ...kaBase, account: addr(key('ka_account/root')) })
ka('ka_all_fields', 'Every optional field of a non-admin key: expiry, mixed limits, call scopes, witness and account.', 'new', {
  ...kaBase, expiry: '4102444800', limits: [{ token: PATH_USD, limit: '5' }, { token: ALPHA_USD, limit: '7', period: '3600' }],
  allowedCalls: [{ target: PATH_USD, selectorRules: [{ selector: TRANSFER, recipients: [RECIPIENT] }] }], witness: Hash.keccak256(Hex.fromString('tempo-conformance/ka-all')), account: addr(key('ka_all_fields/root')),
})
kaCases.push({ name: 'ka_reject_admin_with_limits', op: 'key_authorization.sign', expectError: 'invalid_input',
  description: 'An admin key (TIP-1049) with a spending limit: admin authorizations cannot carry expiry, limits or call scopes, and the node rejects any transaction carrying one (see decode_admin_key_with_limits).',
  source: 'tempo crates/evm/src/handler.rs admin key validation; tempo-go key_authorization.go Validate; pytempo keychain.py KeyAuthorization',
  input: { authorization: { ...kaBase, isAdmin: true, account: addr(key('ka_reject_admin_with_limits/root')), limits: [{ token: PATH_USD, limit: '1' }] }, privateKey: key('ka_reject_admin_with_limits/root') } })
for (const c of kaCases) {
  const res = run(c.op, c.input)
  if (c.expectError) {
    if (res.ok) console.warn('reference disagrees: ' + c.name + ' must fail with ' + c.expectError + ', ox signs it')
    continue
  }
  if (!res.ok) throw new Error(c.name + ': ' + JSON.stringify(res.error))
  c.expected = res.value
}

// --- P256 / WebAuthn / access-key signatures -------------------------------------
// Bootstrapped from tempo-go/pytempo testdata/viem (viem 2.57.13, ox 0.14.59).
// Expected signers and accept/reject verdicts are the ones tempo-primitives
// 1.11.0 TempoSignature::recover_signer gives, recorded in those files.
const viemVectors = JSON.parse(readFileSync(new URL('../../vectors/sources/viem/vectors.json', import.meta.url), 'utf8'))
const clientData = JSON.parse(readFileSync(new URL('../../vectors/sources/viem/webauthn_client_data.json', import.meta.url), 'utf8'))
const sigCases: Case[] = []
const viemSource = 'tempo-go/pytempo testdata/viem/vectors.json (viem 2.57.13, ox 0.14.59)'
for (const [name, field, addrField, description, tags] of [
  ['sig_p256', 'p256Signature', 'p256Address', 'P256 root signature (Account.fromP256, RFC 6979, low-s).', ['p256']],
  ['sig_webauthn', 'webAuthnSignature', 'webAuthnAddress', 'Headless WebAuthn signature, rpId localhost.', ['webauthn']],
  ['sig_p256_access_key_v2', 'p256AccessKeySignature', 'rootAddress', 'P256 access key, keychain V2 envelope: signs for the root.', ['p256', 'keychain', 'keychain-v2']],
  ['sig_webauthn_access_key_v2', 'webAuthnAccessKeySignature', 'rootAddress', 'WebAuthn access key, keychain V2 envelope.', ['webauthn', 'keychain', 'keychain-v2']],
  ['sig_p256_keychain_v1', 'p256KeychainV1Signature', 'rootAddress', 'Legacy keychain V1 envelope wrapping a P256 signature.', ['p256', 'keychain', 'keychain-v1']],
] as const)
  sigCases.push({ name, description, source: viemSource, op: 'signature.recover', tags: [...tags], input: { hash: viemVectors.hash, signature: viemVectors[field] }, expected: { signer: viemVectors[addrField].toLowerCase() } })
for (const c of clientData.cases)
  sigCases.push({
    name: 'webauthn_client_data_' + c.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
    description: 'WebAuthn signature over an unusual clientDataJSON encoding: ' + c.name + '.',
    source: 'tempo-go/pytempo testdata/viem/webauthn_client_data.json; verdict from tempo-primitives 1.11.0 recover_signer',
    op: 'signature.recover', tags: ['webauthn', 'client-data-json'],
    input: { hash: clientData.hash, signature: c.envelope },
    ...(c.accept ? { expected: { signer: clientData.address.toLowerCase() } } : { expectError: 'verification_error' }),
  })

// viem's signed transactions (chain 42431): decode only.
for (const [name, field, tags] of [['decode_viem_p256_root_tx', 'p256RootTransaction', ['p256']], ['decode_viem_webauthn_root_tx', 'webAuthnRootTransaction', ['webauthn']], ['decode_viem_p256_access_key_tx', 'p256AccessKeyTransaction', ['p256', 'keychain']]] as const) {
  const raw = viemVectors[field].serialized
  const res = run('tx.decode', { raw })
  if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
  if (res.value.sender !== viemVectors[field].sender.toLowerCase()) throw new Error(name + ': sender ' + res.value.sender)
  decodeCases.push({ name, op: 'tx.decode', description: 'Decode viem\'s ' + field + '.', source: viemSource, tags: [...tags], input: { raw }, expected: res.value })
}

// P256 and WebAuthn transactions on the dev chain, signed with viem's tempo
// accounts, decoded by every SDK and submitted to the node.
{
  const p256Pk = key('p256-root/key')
  const waPk = key('webauthn-root/key')
  const accessRoot = TempoAccount.fromSecp256k1(key('p256-access/root'))
  const waAccessRoot = TempoAccount.fromSecp256k1(key('webauthn-access/root'))
  const rpId = 'localhost', origin = 'http://localhost'
  const p256Acct = TempoAccount.fromP256(p256Pk)
  const waAcct = TempoAccount.fromHeadlessWebAuthn(waPk, { rpId, origin })
  const p256Access = TempoAccount.fromP256(key('p256-access/key'), { access: accessRoot })
  const waAccess = TempoAccount.fromHeadlessWebAuthn(key('webauthn-access/key'), { rpId, origin, access: waAccessRoot })
  const envBase = { chainId: 1337, calls: [{ to: PATH_USD, data: transferCall().data, value: 0n }], gas: 2000000n, maxFeePerGas: 50000000000n, maxPriorityFeePerGas: 1000000000n, nonce: 0n, nonceKey: 0n }
  const txs: [string, string, any, any, string, string[]][] = [
    ['decode_p256_root_tx', 'P256 root account sends a pathUSD transfer.', p256Acct, {}, p256Acct.address, ['p256']],
    ['decode_webauthn_root_tx', 'WebAuthn root account sends a pathUSD transfer.', waAcct, {}, waAcct.address, ['webauthn']],
    ['decode_p256_access_key_tx', 'P256 access key, authorized in the same transaction by its secp256k1 root, signs for the root (keychain V2).', p256Access,
      { keyAuthorization: await TempoAccount.signKeyAuthorization(accessRoot, { key: p256Access, chainId: 1337n }) }, accessRoot.address, ['p256', 'keychain', 'key-authorization']],
    ['decode_webauthn_access_key_tx', 'WebAuthn access key, authorized in the same transaction, signs for its root (keychain V2).', waAccess,
      { keyAuthorization: await TempoAccount.signKeyAuthorization(waAccessRoot, { key: waAccess, chainId: 1337n }) }, waAccessRoot.address, ['webauthn', 'keychain', 'key-authorization']],
  ]
  for (const [name, description, acct, extra, sender, tags] of txs) {
    const env = TxEnvelopeTempo.from({ ...envBase, ...extra } as any)
    const signature = await acct.sign({ hash: TxEnvelopeTempo.getSignPayload(env) })
    const raw = TxEnvelopeTempo.serialize(env, { signature: SignatureEnvelope.from(signature) })
    const res = run('tx.decode', { raw })
    if (!res.ok) throw new Error(name + ': ' + JSON.stringify(res.error))
    if (res.value.sender !== sender.toLowerCase()) throw new Error(name + ': sender ' + res.value.sender + ' want ' + sender)
    decodeCases.push({ name, op: 'tx.decode', description, source: 'viem/tempo Account (fromP256, fromHeadlessWebAuthn, access keys) as in testdata/viem/gen.mjs', tags, input: { raw }, expected: res.value,
      node: { expect: 'success', fund: [sender.toLowerCase()], feePayer: sender.toLowerCase() } })
  }
}

// The reference must agree with every decode error the cases declare; say so when it does not.
for (const c of [...decodeCases, ...sigCases]) {
  if (!c.expectError) continue
  const res = run(c.op, c.input)
  if (res.ok) console.warn('reference disagrees: ' + c.name + ' must fail with ' + c.expectError + ', ox accepts it')
  else if (res.error.type !== c.expectError) console.warn('reference disagrees: ' + c.name + ' fails with ' + res.error.type + ', not ' + c.expectError)
}

// --- write -----------------------------------------------------------------------
const SKIP_KEYS = ['adapters', 'sdkVersions', 'skipReasons'] as const
const write = (file: string, operation: string, cases: Case[], description: string, spec_ref: string) => {
  const url = new URL('../../vectors/' + file, import.meta.url)
  const previous = new Map<string, any>()
  if (existsSync(url)) for (const s of JSON.parse(readFileSync(url, 'utf8')).scenarios ?? []) previous.set(s.name, s)
  const names = new Set<string>()
  const scenarios = cases.map((c) => {
    if (names.has(c.name)) throw new Error('duplicate scenario ' + c.name)
    names.add(c.name)
    const prev = previous.get(c.name) ?? {}
    return {
      name: c.name,
      description: c.description,
      tags: tagsFor(c),
      ...Object.fromEntries(SKIP_KEYS.filter((k) => prev[k] !== undefined).map((k) => [k, prev[k]])),
      source: c.source,
      input: c.input,
      expected: c.expectError ? { ok: false, error: { type: c.expectError } } : { ok: true, value: c.expected },
      ...(c.node ? { node: c.node } : {}),
    }
  })
  for (const name of previous.keys()) if (!names.has(name)) console.warn(file + ': dropped scenario ' + name)
  const doc = { $schema: '../schemas/vector.schema.json', version: '2.0.0', description, spec_ref, generatedBy: 'adapters/typescript/generate.ts (ox ' + OX_VERSION + ')', commands: { operation }, scenarios }
  writeFileSync(url, JSON.stringify(doc, null, 2) + '\n')
}
const TX_SPEC = 'Tempo Transaction (type 0x76): https://docs.tempo.xyz/protocol/transactions/spec-tempo-transaction; tempo-primitives crates/primitives/src/transaction/tempo_transaction.rs'
write('transactions.json', 'tx.sign', signCases, 'tx.sign: signing, sponsorship, key authorizations (limits, call scopes, witness, admin keys, account binding), keychain V1/V2 access keys, expiring nonces, and inputs every SDK must refuse to sign. Scenarios with node run in order on one fresh dev node per adapter, with the bytes that adapter produced.', TX_SPEC)
write('decode.json', 'tx.decode', decodeCases, 'tx.decode: every signed transaction from transactions.json, P256/WebAuthn transactions from viem, and malformed or non-canonical encodings every SDK and the node must reject.', TX_SPEC + '; crates/primitives/src/transaction/tt_signed.rs')
write('signatures.json', 'signature.recover', sigCases, 'signature.recover: P256, WebAuthn and access-key (keychain V1/V2) envelopes, and WebAuthn clientDataJSON edge cases, with tempo-primitives verdicts.', 'Tempo signature envelopes: https://docs.tempo.xyz/protocol/transactions/spec-tempo-transaction#signature-types; tempo-primitives crates/primitives/src/transaction/tt_signature.rs TempoSignature::recover_signer')
write('key-authorizations.json', 'key_authorization.sign', kaCases, 'key_authorization.sign: signing hash, signature envelope and RLP of signed key authorizations, including TIP-1011 call scopes, TIP-1053 witnesses and TIP-1049 admin keys and account binding.', 'Access keys: https://docs.tempo.xyz/protocol/transactions/spec-tempo-transaction#access-keys; TIP-1011, TIP-1049, TIP-1053; tempo-primitives crates/primitives/src/transaction/key_authorization.rs')
console.log('signatures', sigCases.length, 'transactions', signCases.length, 'decode', decodeCases.length, 'key-authorizations', kaCases.length)

