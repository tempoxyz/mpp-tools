// TypeScript reference adapter: viem's ox/tempo.
// JSON lines on stdin -> JSON lines on stdout. See ../../SPEC.md.
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { Hash, Hex, Secp256k1, Address, P256, WebAuthnP256 } from 'ox'
import { KeyAuthorization, SignatureEnvelope, TxEnvelopeTempo } from 'ox/tempo'

class AdapterError extends Error {
  type: string
  constructor(type: string, message: string) {
    super(message)
    this.type = type
  }
}

const big = (v: unknown, field: string): bigint => {
  if (typeof v !== 'string' || !/^[0-9]+$/.test(v)) throw new AdapterError('invalid_input', field + ' must be a decimal string')
  return BigInt(v)
}
const optBig = (v: unknown, field: string) => (v === undefined || v === null ? undefined : big(v, field))
const num = (v: unknown, field: string) => Number(big(v, field))
const optNum = (v: unknown, field: string) => (v === undefined || v === null ? undefined : num(v, field))
const dec = (v: bigint | number | undefined | null) => (v === undefined || v === null ? null : BigInt(v).toString())
const lower = (v: string | undefined | null) => (v === undefined || v === null ? null : v.toLowerCase())

const keyType = (t: string) => {
  if (t === 'secp256k1' || t === 'p256') return t
  if (t === 'webauthn') return 'webAuthn'
  throw new AdapterError('invalid_input', 'unknown keyType ' + t)
}
const keyTypeOut = (t: string) => (t === 'webAuthn' ? 'webauthn' : t)

function toAuth(a: any) {
  return {
    address: a.keyId,
    chainId: big(a.chainId, 'authorization.chainId'),
    type: keyType(a.keyType),
    ...(a.expiry !== undefined && a.expiry !== null ? { expiry: num(a.expiry, 'authorization.expiry') } : {}),
    ...(a.limits !== undefined && a.limits !== null
      ? {
          limits: a.limits.map((l: any) => ({
            token: l.token,
            limit: big(l.limit, 'limit.limit'),
            ...(l.period !== undefined && l.period !== null && l.period !== '0' ? { period: num(l.period, 'limit.period') } : {}),
          })),
        }
      : {}),
  } as any
}

function signAuth(a: any, privateKey: Hex.Hex) {
  const auth = toAuth(a)
  const signingHash = KeyAuthorization.getSignPayload(auth)
  const sig = Secp256k1.sign({ payload: signingHash, privateKey })
  const signed = KeyAuthorization.from(auth, { signature: SignatureEnvelope.from(sig) })
  return { signingHash, sig, signed }
}

function toEnvelope(tx: any) {
  return {
    chainId: num(tx.chainId, 'chainId'),
    nonce: big(tx.nonce, 'nonce'),
    nonceKey: big(tx.nonceKey, 'nonceKey'),
    gas: big(tx.gas, 'gas'),
    maxFeePerGas: big(tx.maxFeePerGas, 'maxFeePerGas'),
    maxPriorityFeePerGas: big(tx.maxPriorityFeePerGas, 'maxPriorityFeePerGas'),
    calls: tx.calls.map((c: any) => ({
      ...(c.to ? { to: c.to } : {}),
      value: big(c.value, 'call.value'),
      data: c.data,
    })),
    accessList: tx.accessList ?? [],
    ...(tx.validBefore != null ? { validBefore: num(tx.validBefore, 'validBefore') } : {}),
    ...(tx.validAfter != null ? { validAfter: num(tx.validAfter, 'validAfter') } : {}),
    ...(tx.feeToken ? { feeToken: tx.feeToken } : {}),
    ...(tx.keyAuthorization
      ? { keyAuthorization: signAuth(tx.keyAuthorization.authorization, tx.keyAuthorization.privateKey).signed }
      : {}),
  } as any
}

function txSign(input: any) {
  const sponsored = input.feePayerPrivateKey != null
  let envelope: any
  try {
    envelope = TxEnvelopeTempo.from({ ...toEnvelope(input.tx), ...(sponsored ? { feePayerSignature: null } : {}) })
    TxEnvelopeTempo.assert(envelope)
  } catch (e: any) {
    if (e instanceof AdapterError) throw e
    throw new AdapterError('invalid_input', e?.shortMessage ?? e?.message ?? String(e))
  }
  const senderSigningHash = TxEnvelopeTempo.getSignPayload(envelope)
  let signature: any
  let sender: string
  if (input.signer.type === 'secp256k1') {
    const sig = Secp256k1.sign({ payload: senderSigningHash, privateKey: input.signer.privateKey })
    signature = SignatureEnvelope.from(sig)
    sender = Address.fromPublicKey(Secp256k1.getPublicKey({ privateKey: input.signer.privateKey }))
  } else if (input.signer.type === 'keychain') {
    const root = input.signer.rootAccount
    const payload = TxEnvelopeTempo.getSignPayload(envelope, { from: root })
    const inner = Secp256k1.sign({ payload, privateKey: input.signer.accessKeyPrivateKey })
    signature = SignatureEnvelope.from({ type: 'keychain', version: 'v2', userAddress: root, inner: SignatureEnvelope.from(inner) } as any)
    sender = root
  } else throw new AdapterError('unsupported', 'signer type ' + input.signer.type)

  let feePayerSigningHash: Hex.Hex | null = null
  let final: any = { ...envelope, signature }
  if (sponsored) {
    feePayerSigningHash = TxEnvelopeTempo.getFeePayerSignPayload(envelope, { sender: sender as any })
    const fps = Secp256k1.sign({ payload: feePayerSigningHash, privateKey: input.feePayerPrivateKey })
    final = { ...final, feePayerSignature: fps }
  }
  const raw = TxEnvelopeTempo.serialize(final)
  return { senderSigningHash, feePayerSigningHash, raw, hash: Hash.keccak256(raw), sender: sender.toLowerCase() }
}

function txDecode(input: any) {
  let env: any
  try {
    env = TxEnvelopeTempo.deserialize(input.raw)
    TxEnvelopeTempo.assert(env)
  } catch (e: any) {
    throw new AdapterError('decode_error', e?.shortMessage ?? e?.message ?? String(e))
  }
  if (!env.signature) throw new AdapterError('decode_error', 'missing sender signature')
  const sig = env.signature
  const sigType = sig.type === 'webAuthn' ? 'webauthn' : sig.type
  let sender: string
  try {
    if (sig.type === 'keychain') sender = sig.userAddress
    else if (sig.type === 'secp256k1')
      sender = Secp256k1.recoverAddress({ payload: TxEnvelopeTempo.getSignPayload(env), signature: sig.signature })
    else sender = Address.fromPublicKey(sig.publicKey)
  } catch (e: any) {
    throw new AdapterError('decode_error', 'cannot recover sender: ' + (e?.message ?? e))
  }
  let feePayer: string | null = null
  if (env.feePayerSignature) {
    try {
      feePayer = Secp256k1.recoverAddress({
        payload: TxEnvelopeTempo.getFeePayerSignPayload(env, { sender: sender as any }),
        signature: env.feePayerSignature,
      })
    } catch (e: any) {
      throw new AdapterError('decode_error', 'cannot recover fee payer: ' + (e?.message ?? e))
    }
  }
  const ka = env.keyAuthorization
  return {
    tx: {
      chainId: dec(env.chainId),
      nonce: dec(env.nonce ?? 0n),
      nonceKey: dec(env.nonceKey ?? 0n),
      gas: dec(env.gas ?? 0n),
      maxFeePerGas: dec(env.maxFeePerGas ?? 0n),
      maxPriorityFeePerGas: dec(env.maxPriorityFeePerGas ?? 0n),
      calls: env.calls.map((c: any) => ({ to: lower(c.to ?? null), value: dec(c.value ?? 0n), data: (c.data ?? '0x').toLowerCase() })),
      accessList: (env.accessList ?? []).map((a: any) => ({ address: a.address.toLowerCase(), storageKeys: a.storageKeys.map((k: string) => k.toLowerCase()) })),
      validBefore: dec(env.validBefore),
      validAfter: dec(env.validAfter),
      feeToken: lower(env.feeToken),
      keyAuthorization: ka
        ? {
            chainId: dec(ka.chainId),
            keyType: keyTypeOut(ka.type),
            keyId: ka.address.toLowerCase(),
            expiry: dec(ka.expiry),
            limits: ka.limits ? ka.limits.map((l: any) => ({ token: l.token.toLowerCase(), limit: dec(l.limit), period: dec(l.period ?? 0) })) : null,
          }
        : null,
    },
    signatureType: sigType,
    sender: sender.toLowerCase(),
    feePayer: lower(feePayer),
    hash: Hash.keccak256(input.raw),
  }
}

function keyAuthSign(input: any) {
  const { signingHash, sig, signed } = signAuth(input.authorization, input.privateKey)
  const signature = SignatureEnvelope.serialize(SignatureEnvelope.from(sig))
  return { signingHash, signature, serialized: KeyAuthorization.serialize(signed) }
}

// signature.recover: the account a Tempo signature envelope signs for. Keychain
// envelopes sign for userAddress; V2 inner signatures cover
// keccak256(0x04 || hash || userAddress), V1 the hash itself.
function signatureRecover(input: any) {
  const hash = input.hash as Hex.Hex
  let env: any
  try {
    env = SignatureEnvelope.deserialize(input.signature)
  } catch (e: any) {
    throw new AdapterError('verification_error', 'deserialize: ' + (e?.shortMessage ?? e?.message ?? e))
  }
  let signer: string
  let payload: Hex.Hex = hash
  let inner = env
  if (env.type === 'keychain') {
    signer = env.userAddress
    if (env.version === 'v2') payload = Hash.keccak256(Hex.concat('0x04', hash, env.userAddress))
    inner = env.inner
  }
  let innerSigner: string
  try {
    if (inner.type === 'secp256k1') innerSigner = Secp256k1.recoverAddress({ payload, signature: inner.signature })
    else if (inner.type === 'p256' || inner.type === 'webAuthn') {
      innerSigner = Address.fromPublicKey(inner.publicKey)
      if (!SignatureEnvelope.verify(inner, { payload, publicKey: inner.publicKey })) throw new Error('signature does not verify')
    } else throw new Error('unsupported inner type ' + inner.type)
  } catch (e: any) {
    throw new AdapterError('verification_error', e?.shortMessage ?? e?.message ?? String(e))
  }
  return { signer: (env.type === 'keychain' ? signer! : innerSigner).toLowerCase() }
}

const ops: Record<string, (input: any) => unknown> = {
  'signature.recover': signatureRecover,
  'tx.sign': txSign,
  'tx.decode': txDecode,
  'key_authorization.sign': keyAuthSign,
}

export function handle(req: any) {
  try {
    const fn = ops[req?.op]
    if (!fn) return { ok: false, error: { type: 'unsupported', message: 'unknown op ' + req?.op } }
    return { ok: true, value: fn(req.input) }
  } catch (e: any) {
    if (e instanceof AdapterError) return { ok: false, error: { type: e.type, message: e.message } }
    return { ok: false, error: { type: 'unknown_error', message: e?.shortMessage ?? e?.message ?? String(e) } }
  }
}

// Not import.meta.main: Node added it only in 24.2. generate.ts imports handle() without starting the loop.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity })
  for await (const line of rl) {
    if (!line.trim()) continue
    let res
    try { res = handle(JSON.parse(line)) } catch (e: any) { res = { ok: false, error: { type: 'invalid_input', message: 'bad JSON: ' + e?.message } } }
    process.stdout.write(JSON.stringify(res) + '\n')
  }
}

