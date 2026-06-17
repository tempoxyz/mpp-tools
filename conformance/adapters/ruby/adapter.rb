#!/usr/bin/env ruby
# frozen_string_literal: true

require "base64"
require "json"
require "openssl"
require "time"

require "mpp-rb"

def success(result)
  {success: true, result: result}
end

def error(message, error_type = "unknown_error")
  {success: false, error: message, error_type: error_type}
end

def emit(payload)
  puts(JSON.generate(payload))
end

def adapter_success(value)
  {ok: true, value: value}
end

def adapter_error(message, error_type = "unknown_error")
  {ok: false, error: {type: error_type, message: message}}
end

def base64url_encode(data)
  Base64.urlsafe_encode64(data.to_s.b, padding: false)
end

def base64url_decode(data)
  padded = data.to_s + ("=" * ((-data.to_s.length) % 4))
  Base64.urlsafe_decode64(padded)
end

def stable_json(value)
  JSON.generate(deep_sort_keys(value), space: "", object_nl: "", array_nl: "")
end

def deep_sort_keys(value)
  case value
  when Hash
    value.sort_by { |key, _| key.to_s }.to_h { |key, item| [key, deep_sort_keys(item)] }
  when Array
    value.map { |item| deep_sort_keys(item) }
  else
    value
  end
end

def generate_conformance_challenge_id(params)
  request_b64 = base64url_encode(stable_json(params["request"] || {}))
  payload = [
    params["realm"] || "",
    params["method"] || "",
    params["intent"] || "",
    request_b64,
    params["expires"] || "",
    params["digest"] || "",
    params["opaque"] || ""
  ].join("|")
  digest = OpenSSL::HMAC.digest("SHA256", params.fetch("secretKey").encode("UTF-8"), payload.encode("UTF-8"))
  base64url_encode(digest)
end

def challenge_to_h(challenge)
  result = {
    id: challenge.id,
    realm: challenge.realm,
    method: challenge.method,
    intent: challenge.intent,
    request: challenge.request
  }
  result[:expires] = challenge.expires unless challenge.expires.nil?
  result[:description] = challenge.description unless challenge.description.nil?
  result[:digest] = challenge.digest unless challenge.digest.nil?
  result[:opaque] = challenge.opaque unless challenge.opaque.nil?
  result
end

def credential_to_h(credential)
  challenge = credential.challenge
  request = JSON.parse(base64url_decode(challenge.request))
  challenge_hash = {
    id: challenge.id,
    realm: challenge.realm,
    method: challenge.method,
    intent: challenge.intent,
    request: request
  }
  challenge_hash[:expires] = challenge.expires unless challenge.expires.nil?
  challenge_hash[:digest] = challenge.digest unless challenge.digest.nil?
  challenge_hash[:opaque] = challenge.opaque unless challenge.opaque.nil?

  result = {
    challenge: challenge_hash,
    payload: credential.payload
  }
  result[:source] = credential.source unless credential.source.nil?
  result
end

def receipt_to_h(receipt)
  result = {
    status: receipt.status,
    timestamp: receipt.timestamp.utc.iso8601,
    reference: receipt.reference
  }
  result[:method] = receipt.method unless receipt.method.nil? || receipt.method.empty?
  result[:externalId] = receipt.external_id unless receipt.external_id.nil?
  result[:extra] = receipt.extra unless receipt.extra.nil?
  result
end

def challenge_from_h(data)
  Mpp::Challenge.new(
    id: data.fetch("id"),
    method: data.fetch("method"),
    intent: data.fetch("intent"),
    request: data.fetch("request", {}),
    realm: data.fetch("realm", ""),
    request_b64: base64url_encode(stable_json(data.fetch("request", {}))),
    digest: data["digest"],
    expires: data["expires"],
    description: data["description"],
    opaque: data["opaque"]
  )
end

def echo_from_challenge_h(data)
  request = data.fetch("request", {})
  Mpp::ChallengeEcho.new(
    id: data.fetch("id", ""),
    realm: data.fetch("realm", ""),
    method: data.fetch("method", ""),
    intent: data.fetch("intent", ""),
    request: base64url_encode(stable_json(request)),
    expires: data["expires"],
    digest: data["digest"],
    opaque: data["opaque"]
  )
end

OP_TO_COMMAND = {
  "challenge.parse" => "parse-www-authenticate",
  "challenge.format" => "format-www-authenticate",
  "credential.parse" => "parse-authorization",
  "credential.format" => "format-authorization",
  "receipt.parse" => "parse-receipt",
  "receipt.format" => "format-receipt",
  "base64url.encode" => "base64url-encode",
  "base64url.decode" => "base64url-decode",
  "challenge.id" => "generate-challenge-id",
  "stripe.external_id_binding" => "verify-stripe-external-id-binding"
}.freeze

def command_input_for_request(op, input)
  return input.fetch("header") if op.end_with?(".parse")
  return input.fetch("text") if op.start_with?("base64url.")

  JSON.generate(input)
end

def response_value_for_operation(op, result)
  return {header: result} if op.end_with?(".format")
  return {text: result} if op.start_with?("base64url.")
  return {id: result} if op == "challenge.id"

  result
end

class ConformanceStripeClient
  def initialize(input)
    @input = input
  end

  def v1
    Struct.new(:payment_intents).new(ConformancePaymentIntents.new(@input))
  end
end

class ConformancePaymentIntents
  def initialize(input)
    @input = input
  end

  def create(params, _opts)
    raise "Unexpected shared_payment_granted_token" unless params[:shared_payment_granted_token] == @input.fetch("payload").fetch("spt")

    payment_intent = @input.fetch("paymentIntent")
    headers = payment_intent["replayed"] ? {"idempotent-replayed" => "true"} : {}
    last_response = Struct.new(:headers).new(headers)
    Struct.new(:id, :status, :last_response).new(
      payment_intent.fetch("id"),
      payment_intent.fetch("status"),
      last_response
    )
  end
end

def verify_stripe_external_id_binding(input)
  require "mpp/methods/stripe"
  require "mpp/server"

  secret_key = "conformance-stripe-secret"
  realm = "conformance.local"
  expires = "2099-01-29T12:05:30Z"
  challenge = Mpp::Challenge.create(
    secret_key: secret_key,
    realm: realm,
    method: "stripe",
    intent: "charge",
    request: input.fetch("request"),
    expires: expires
  )
  credential = Mpp::Credential.new(challenge: challenge.to_echo, payload: input.fetch("payload"))
  intent = Mpp::Methods::Stripe::ChargeIntent.new(
    secret_key: "sk_test_conformance",
    client: ConformanceStripeClient.new(input)
  )

  verified = Mpp::Server::Verify.verify_or_challenge(
    authorization: credential.to_authorization,
    intent: intent,
    request: input.fetch("request"),
    realm: realm,
    secret_key: secret_key,
    method: "stripe",
    expires: expires
  )
  return {ok: false, errorType: "invalid_challenge"} if verified.is_a?(Mpp::Challenge)

  _credential, receipt = verified
  receipt_payload = {
    status: receipt.status,
    method: receipt.method,
    timestamp: "2026-01-29T12:00:30Z",
    reference: receipt.reference
  }
  receipt_payload[:externalId] = receipt.external_id unless receipt.external_id.nil?
  {ok: true, receipt: receipt_payload}
rescue Mpp::InvalidChallengeError
  {ok: false, errorType: "invalid_challenge"}
rescue Mpp::VerificationError, Mpp::PaymentError, StandardError
  {ok: false, errorType: "verification_failed"}
end

def run_adapter_request(request)
  op = request.fetch("op")
  input = request.fetch("input")
  command = OP_TO_COMMAND[op]
  return adapter_error("Unknown operation: #{op}", "unsupported_operation") unless command

  begin
    result = run_command(command, command_input_for_request(op, input))
  rescue StandardError => e
    return adapter_error(e.message, error_type_for_command(command))
  end
  return adapter_error(result[:error], result[:error_type]) unless result[:success]

  adapter_success(response_value_for_operation(op, result[:result]))
end

def error_type_for_command(command)
  if command.start_with?("parse-") || command == "base64url-decode"
    "parse_error"
  elsif command.start_with?("format-")
    "format_error"
  elsif command.start_with?("base64url-")
    "encoding_error"
  elsif command.start_with?("generate-")
    "generation_error"
  elsif command.start_with?("verify-")
    "verification_error"
  else
    "unknown_error"
  end
end

def run_command(command, input)
  case command
  when "parse-www-authenticate"
    success(challenge_to_h(Mpp::Challenge.from_www_authenticate(input)))
  when "parse-authorization"
    success(credential_to_h(Mpp::Credential.from_authorization(input)))
  when "parse-receipt"
    success(receipt_to_h(Mpp::Receipt.from_payment_receipt(input)))
  when "format-www-authenticate"
    data = JSON.parse(input)
    success(challenge_from_h(data).to_www_authenticate(data.fetch("realm", "")))
  when "format-authorization"
    data = JSON.parse(input)
    credential = Mpp::Credential.new(
      challenge: echo_from_challenge_h(data.fetch("challenge", {})),
      payload: data.fetch("payload", {}),
      source: data["source"]
    )
    success(credential.to_authorization)
  when "format-receipt"
    data = JSON.parse(input)
    receipt = Mpp::Receipt.new(
      status: data.fetch("status"),
      timestamp: Time.iso8601(data.fetch("timestamp")),
      reference: data.fetch("reference"),
      method: data.fetch("method", ""),
      external_id: data["externalId"],
      extra: data["extra"]
    )
    success(receipt.to_payment_receipt)
  when "base64url-encode"
    success(base64url_encode(input))
  when "base64url-decode"
    success(base64url_decode(input).force_encoding("UTF-8"))
  when "generate-challenge-id"
    success(generate_conformance_challenge_id(JSON.parse(input)))
  when "verify-stripe-external-id-binding"
    success(verify_stripe_external_id_binding(JSON.parse(input)))
  else
    error("Unknown command: #{command}")
  end
end

command = ARGV.fetch(0, nil)
unless command
  begin
    emit(run_adapter_request(JSON.parse(STDIN.read)))
  rescue StandardError => e
    emit(adapter_error(e.message))
  end
  exit
end

begin
  result = run_command(command, STDIN.read.strip)
  emit(result) if result
rescue StandardError => e
  emit(error(e.message, error_type_for_command(command)))
end
