#!/usr/bin/env npx tsx
/**
 * Golden Adapter CLI for the published TypeScript SDK (mppx on npm)
 *
 * This adapter wraps the TypeScript SDK and exposes a consistent CLI interface
 * for conformance testing. All other SDK adapters must produce identical output.
 *
 * Usage:
 *   echo '<input>' | npx tsx adapter.ts <command>
 *
 * Commands:
 *   parse-www-authenticate   Parse WWW-Authenticate header → JSON challenge
 *   parse-authorization      Parse Authorization header → JSON credential
 *   parse-receipt            Parse Payment-Receipt header → JSON receipt
 *   format-www-authenticate  Format JSON challenge → WWW-Authenticate header
 *   format-authorization     Format JSON credential → Authorization header
 *   format-receipt           Format JSON receipt → Payment-Receipt header
 *   base64url-encode         Encode plain string → base64url
 *   base64url-decode         Decode base64url → plain string
 *   generate-challenge-id    Generate HMAC-bound challenge ID from parameters
 *
 * Output:
 *   {"success": true, "result": <value>}
 *   {"success": false, "error": "<message>", "error_type": "<type>"}
 */

import { createHmac } from 'node:crypto'

import { Challenge, Credential, Method, Receipt, z } from 'mppx'
import * as Client from 'mppx/client'
import { Mppx, stripe } from 'mppx/server'

interface SuccessResult<T> {
	success: true
	result: T
}

interface ErrorResult {
	success: false
	error: string
	error_type:
		| 'parse_error'
		| 'format_error'
		| 'encoding_error'
		| 'generation_error'
		| 'http_error'
		| 'verification_error'
		| 'unsupported_operation'
		| 'unknown_error'
}

type Result<T> = SuccessResult<T> | ErrorResult
type AdapterResponse =
	| { ok: true; value: unknown }
	| { ok: false; error: { type: ErrorResult['error_type']; message: string } }

function success<T>(result: T): SuccessResult<T> {
	return { success: true, result }
}

function error(message: string, type: ErrorResult['error_type'] = 'unknown_error'): ErrorResult {
	return { success: false, error: message, error_type: type }
}

function adapterSuccess(value: unknown): AdapterResponse {
	return { ok: true, value }
}

function adapterError(message: string, type: ErrorResult['error_type'] = 'unknown_error'): AdapterResponse {
	return { ok: false, error: { type, message } }
}

function readStdin(): Promise<string> {
	return new Promise((resolve) => {
		let data = ''
		process.stdin.setEncoding('utf8')
		process.stdin.on('data', (chunk) => (data += chunk))
		process.stdin.on('end', () => resolve(data.trim()))
	})
}

function stableStringify(value: unknown): string {
	if (Array.isArray(value)) {
		return `[${value.map((item) => stableStringify(item)).join(',')}]`
	}

	if (value && typeof value === 'object') {
		const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
			left.localeCompare(right),
		)
		return `{${entries
			.map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`)
			.join(',')}}`
	}

	return JSON.stringify(value)
}

function base64UrlEncode(value: string): string {
	return Buffer.from(value, 'utf8').toString('base64url')
}

function base64UrlDecode(value: string): string {
	return Buffer.from(value, 'base64url').toString('utf8')
}

function generateConformanceChallengeId(params: {
	secretKey: string
	realm?: string
	method?: string
	intent?: string
	request?: Record<string, unknown>
	expires?: string
	digest?: string
	opaque?: string
}): string {
	const requestJson = stableStringify(params.request ?? {})
	const requestB64 = base64UrlEncode(requestJson)
	const payload = [
		params.realm ?? '',
		params.method ?? '',
		params.intent ?? '',
		requestB64,
		params.expires ?? '',
		params.digest ?? '',
		params.opaque ?? '',
	].join('|')
	return createHmac('sha256', params.secretKey).update(payload).digest('base64url')
}

async function verifyStripeExternalIdBinding(input: {
	request: Record<string, unknown>
	payload: Record<string, unknown>
	paymentIntent: { id: string; status: string; replayed?: boolean }
}): Promise<
	| {
		ok: true
		receipt: {
			status: string
			method: string
			timestamp: string
			reference: string
			externalId?: string
		}
	}
	| { ok: false; errorType: 'invalid_challenge' | 'verification_failed' }
> {
	const secretKey = 'conformance-stripe-secret'
	const methodDetails =
		typeof input.request.methodDetails === 'object' && input.request.methodDetails !== null
			? (input.request.methodDetails as Record<string, unknown>)
			: {}
	const mppx = Mppx.create({
		secretKey,
		methods: [
			stripe.charge({
				amount: String(input.request.amount ?? '0'),
				client: {
					paymentIntents: {
						async create(params: Record<string, unknown>) {
							if (params.shared_payment_granted_token !== input.payload.spt) {
								throw new Error('Unexpected shared_payment_granted_token')
							}
							return {
								id: input.paymentIntent.id,
								status: input.paymentIntent.status,
								lastResponse: {
									headers: {
										...(input.paymentIntent.replayed
											? { 'idempotent-replayed': 'true' }
											: {}),
									},
								},
							}
						},
					},
				},
				currency: String(input.request.currency ?? 'usd'),
				decimals: 0,
				networkId:
					typeof methodDetails.networkId === 'string' ? methodDetails.networkId : 'conformance',
				paymentMethodTypes: Array.isArray(methodDetails.paymentMethodTypes)
					? methodDetails.paymentMethodTypes.map(String)
					: ['card'],
			}),
		],
	})
	const challenge = Challenge.from({
		secretKey,
		realm: 'conformance.local',
		method: 'stripe',
		intent: 'charge',
		request: input.request,
		expires: '2099-01-29T12:05:30Z',
	})
	const credential = Credential.from({ challenge, payload: input.payload })

	try {
		const receipt = await mppx.verifyCredential(credential)
		return {
			ok: true,
			receipt: {
				...receipt,
				timestamp: '2026-01-29T12:00:30Z',
			},
		}
	} catch (err) {
		if (err instanceof Error && err.name === 'InvalidChallengeError')
			return { ok: false, errorType: 'invalid_challenge' }
		return { ok: false, errorType: 'verification_failed' }
	}
}

function hasDuplicateChallengeParameter(header: string): boolean {
	const seen = new Set<string>()
	for (const match of header.matchAll(/(?:^|,\s*)(\w+)=/g)) {
		const key = match[1]
		if (seen.has(key)) return true
		seen.add(key)
	}
	return false
}

async function runCommand(command: string, input: string): Promise<Result<unknown>> {
	try {
		switch (command) {
			case 'parse-www-authenticate': {
				if (hasDuplicateChallengeParameter(input.replace(/^Payment\s+/, '')))
					return error('Duplicate challenge parameter', 'parse_error')
				const challenge = Challenge.deserialize(input)
				if (!challenge.id) throw new Error('Missing id parameter.')
				return success(challenge)
			}

			case 'parse-authorization': {
				const credential = Credential.deserialize(input)
				return success(credential)
			}

			case 'parse-receipt': {
				const receipt = Receipt.deserialize(input)
				return success(receipt)
			}

			case 'format-www-authenticate': {
				const challengeData = JSON.parse(input)
				const challenge = Challenge.from(challengeData)
				const header = Challenge.serialize(challenge)
				return success(header)
			}

			case 'format-authorization': {
				const credentialData = JSON.parse(input)
				const credential = Credential.from(credentialData)
				const header = Credential.serialize(credential)
				return success(header)
			}

			case 'format-receipt': {
				const receiptData = JSON.parse(input)
				const receipt = Receipt.from(receiptData)
				const header = Receipt.serialize(receipt)
				return success(header)
			}

			case 'base64url-encode': {
				const encoded = base64UrlEncode(input)
				return success(encoded)
			}

			case 'base64url-decode': {
				const decoded = base64UrlDecode(input)
				return success(decoded)
			}

			case 'generate-challenge-id': {
				const params = JSON.parse(input)
				return success(generateConformanceChallengeId(params))
			}

			case 'verify-stripe-external-id-binding': {
				const params = JSON.parse(input)
				return success(await verifyStripeExternalIdBinding(params))
			}

			default:
				return error(`Unknown command: ${command}`, 'unknown_error')
		}
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err)

		if (command.startsWith('parse-')) {
			return error(message, 'parse_error')
		} else if (command.startsWith('format-')) {
			return error(message, 'format_error')
		} else if (command.startsWith('base64url-')) {
			return error(message, 'encoding_error')
		} else if (command.startsWith('generate-')) {
			return error(message, 'generation_error')
		} else if (command.startsWith('verify-')) {
			return error(message, 'verification_error')
		} else {
			return error(message, 'unknown_error')
		}
	}
}

const OP_TO_COMMAND: Record<string, string> = {
	'challenge.parse': 'parse-www-authenticate',
	'challenge.format': 'format-www-authenticate',
	'credential.parse': 'parse-authorization',
	'credential.format': 'format-authorization',
	'receipt.parse': 'parse-receipt',
	'receipt.format': 'format-receipt',
	'base64url.encode': 'base64url-encode',
	'base64url.decode': 'base64url-decode',
	'challenge.id': 'generate-challenge-id',
	'stripe.external_id_binding': 'verify-stripe-external-id-binding',
}

function commandInputForRequest(op: string, input: unknown): string {
	if (op.endsWith('.parse')) return (input as { header: string }).header
	if (op.startsWith('base64url.')) return (input as { text: string }).text
	return JSON.stringify(input)
}

function responseValueForOperation(op: string, result: unknown): unknown {
	if (op.endsWith('.format')) return { header: result }
	if (op.startsWith('base64url.')) return { text: result }
	if (op === 'challenge.id') return { id: result }
	return result
}

type HttpPaymentRequest = {
	url: string
	method: string
	headers: Record<string, string>
	body: string | null
	payment: {
		payload: Record<string, unknown>
		source?: Record<string, unknown> | string | null
	}
	mode: string
}

function headersToObject(headers: Headers): Record<string, string> {
	const result: Record<string, string> = {}
	headers.forEach((value, key) => {
		result[key] = value
	})
	return result
}

const httpPaymentMethod = Method.toClient(
	Method.from({
		name: 'tempo',
		intent: 'charge',
		schema: {
			credential: {
				payload: z.object({
					type: z.literal('transaction'),
					signature: z.string(),
				}),
			},
			request: z.object({
				amount: z.string(),
				currency: z.string(),
				recipient: z.string(),
				expires: z.string(),
				resource: z.optional(z.string()),
			}),
		},
	}),
	{
		async createCredential({ challenge }) {
			return Credential.serialize(
				Credential.from({
					challenge,
					payload: currentHttpPayment?.payment.payload ?? {},
					...(currentHttpPayment?.source === undefined ? {} : { source: currentHttpPayment.source }),
				}),
			)
		},
	},
)

let currentHttpPayment: { payment: HttpPaymentRequest['payment']; source?: string } | undefined

async function runHttpPaymentRequest(input: HttpPaymentRequest): Promise<AdapterResponse> {
	try {
		if (input.mode === 'plain') {
			const response = await fetch(input.url, {
				method: input.method,
				headers: input.headers,
				body: input.body,
			})
			return adapterSuccess({
				status: response.status,
				headers: headersToObject(response.headers),
				body: await response.text(),
			})
		}
		if (input.mode !== 'payment' && input.mode !== 'invalid_payload') {
			return adapterError(`Unsupported http.payment_request mode: ${input.mode}`, 'unsupported_operation')
		}
		currentHttpPayment = {
			payment: input.payment,
			source: typeof input.payment.source === 'string' ? input.payment.source : undefined,
		}
		const mppx = Client.Mppx.create({
			methods: [httpPaymentMethod],
			polyfill: false,
			acceptPaymentPolicy: 'always',
		})
		const response = await mppx.fetch(input.url, {
			method: input.method,
			headers: input.headers,
			body: input.body,
		})
		return adapterSuccess({
			status: response.status,
			headers: headersToObject(response.headers),
			body: await response.text(),
		})
	} catch (err) {
		return adapterError(err instanceof Error ? err.message : String(err), 'http_error')
	} finally {
		currentHttpPayment = undefined
	}
}

async function runAdapterRequest(request: { op: string; input: unknown }): Promise<AdapterResponse> {
	if (request.op === 'http.payment_request') return runHttpPaymentRequest(request.input as HttpPaymentRequest)
	const command = OP_TO_COMMAND[request.op]
	if (!command) return adapterError(`Unknown operation: ${request.op}`, 'unsupported_operation')
	const result = await runCommand(command, commandInputForRequest(request.op, request.input))
	if (!result.success) return adapterError(result.error, result.error_type)
	return adapterSuccess(responseValueForOperation(request.op, result.result))
}

async function main(): Promise<void> {
	const command = process.argv[2]

	if (!command) {
		const stdin = await readStdin()
		const request = JSON.parse(stdin)
		console.log(JSON.stringify(await runAdapterRequest(request)))
		return
	}

	const stdin = await readStdin()
	const result = await runCommand(command, stdin)
	console.log(JSON.stringify(result))
}

main().catch((err) => {
	console.log(JSON.stringify(error(err.message, 'unknown_error')))
	process.exit(1)
})
