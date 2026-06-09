import CryptoKit
import Foundation
import MPP

struct AdapterRequest: Decodable {
    let op: String
    let input: JSONValue
}

struct HeaderInput: Codable {
    let header: String
}

struct HeaderOutput: Encodable {
    let header: String
}

struct TextValue: Codable {
    let text: String
}

struct ChallengeIdInput: Codable {
    let secretKey: String
    let realm: String
    let method: String
    let intent: String
    let request: [String: JSONValue]
    let expires: String?
    let description: String?
    let digest: String?
    let opaque: String?
}

struct ChallengeIdOutput: Encodable {
    let id: String
}

struct WireChallenge: Codable {
    let id: String
    let realm: String
    let method: String
    let intent: String
    let request: [String: JSONValue]
    let expires: String?
    let description: String?
    let digest: String?
    let opaque: String?

    init(
        id: String,
        realm: String,
        method: String,
        intent: String,
        request: [String: JSONValue],
        expires: String? = nil,
        description: String? = nil,
        digest: String? = nil,
        opaque: String? = nil
    ) {
        self.id = id
        self.realm = realm
        self.method = method
        self.intent = intent
        self.request = request
        self.expires = expires
        self.description = description
        self.digest = digest
        self.opaque = opaque
    }

    init(_ challenge: Challenge) {
        self.init(
            id: challenge.id,
            realm: challenge.realm,
            method: challenge.method,
            intent: challenge.intent,
            request: challenge.request,
            expires: challenge.expires,
            description: challenge.description,
            digest: challenge.digest,
            opaque: challenge.opaque.map { JSONValue.object($0.mapValues(JSONValue.string)).canonicalJSON() }
        )
    }

    var sdkChallenge: Challenge {
        Challenge(
            id: id,
            realm: realm,
            method: method,
            intent: intent,
            request: request,
            description: description,
            digest: digest,
            expires: expires
        )
    }
}

struct WireCredential: Codable {
    let challenge: WireChallenge
    let payload: [String: JSONValue]
    let source: JSONValue?
}

struct WireReceipt: Codable {
    let status: String
    let method: String?
    let timestamp: String
    let reference: String
    let externalId: String?
    let extra: [String: JSONValue]?

    init(_ receipt: Receipt) {
        self.status = receipt.status
        self.method = receipt.method
        self.timestamp = receipt.timestamp
        self.reference = receipt.reference
        self.externalId = receipt.externalId
        self.extra = nil
    }

    func sdkReceipt(errorType: String) throws -> Receipt {
        guard let method else {
            throw AdapterFailure(type: errorType, message: "receipt missing method")
        }
        return Receipt(
            status: status,
            method: method,
            timestamp: timestamp,
            reference: reference,
            externalId: externalId
        )
    }
}

struct AdapterFailure: Error {
    let type: String
    let message: String
}

let input = FileHandle.standardInput.readDataToEndOfFile()

do {
    let request = try JSONDecoder().decode(AdapterRequest.self, from: input)
    let value = try run(request)
    printJSON(["ok": JSONValue.bool(true), "value": value])
} catch let failure as AdapterFailure {
    printJSON([
        "ok": JSONValue.bool(false),
        "error": .object([
            "type": .string(failure.type),
            "message": .string(failure.message),
        ]),
    ])
} catch {
    printJSON([
        "ok": JSONValue.bool(false),
        "error": .object([
            "type": .string("unknown_error"),
            "message": .string(String(describing: error)),
        ]),
    ])
}

func run(_ request: AdapterRequest) throws -> JSONValue {
    switch request.op {
    case "challenge.parse":
        let input: HeaderInput = try decodeInput(request.input, errorType: "parse_error")
        let challenge = try mapError("parse_error") { try Challenge.deserialize(input.header) }
        try validateChallenge(challenge, errorType: "parse_error")
        return try encodeValue(WireChallenge(challenge), errorType: "parse_error")

    case "challenge.format":
        let challenge: WireChallenge = try decodeInput(request.input, errorType: "format_error")
        let header: String
        if let opaque = challenge.opaque {
            header = formatChallengeHeader(challenge, opaque: opaque)
        } else {
            header = Challenge.serialize(challenge.sdkChallenge)
        }
        return try encodeValue(HeaderOutput(header: header), errorType: "format_error")

    case "credential.parse":
        let input: HeaderInput = try decodeInput(request.input, errorType: "parse_error")
        let credential = try mapError("parse_error") { try Credential.deserialize(input.header) }
        try validateChallenge(credential.challenge, errorType: "parse_error")
        let value = WireCredential(
            challenge: WireChallenge(credential.challenge),
            payload: credential.payload,
            source: credential.source.map(JSONValue.string)
        )
        return try encodeValue(value, errorType: "parse_error")

    case "credential.format":
        let credential: WireCredential = try decodeInput(request.input, errorType: "format_error")
        let source = try stringSource(credential.source)
        let sdkCredential = Credential(
            challenge: credential.challenge.sdkChallenge,
            payload: credential.payload,
            source: source
        )
        return try encodeValue(
            HeaderOutput(header: Credential.serialize(sdkCredential)),
            errorType: "format_error"
        )

    case "receipt.parse":
        let input: HeaderInput = try decodeInput(request.input, errorType: "parse_error")
        let receipt = try mapError("parse_error") { try Receipt.deserialize(input.header) }
        try validateTimestamp(receipt.timestamp, errorType: "parse_error")
        return try encodeValue(WireReceipt(receipt), errorType: "parse_error")

    case "receipt.format":
        let receipt: WireReceipt = try decodeInput(request.input, errorType: "format_error")
        try validateTimestamp(receipt.timestamp, errorType: "format_error")
        let header = try Receipt.serialize(receipt.sdkReceipt(errorType: "format_error"))
        return try encodeValue(HeaderOutput(header: header), errorType: "format_error")

    case "base64url.encode":
        let input: TextValue = try decodeInput(request.input, errorType: "encoding_error")
        return try encodeValue(TextValue(text: Base64URL.encodeString(input.text)), errorType: "encoding_error")

    case "base64url.decode":
        let input: TextValue = try decodeInput(request.input, errorType: "encoding_error")
        return try encodeValue(
            TextValue(text: mapError("encoding_error") { try Base64URL.decodeString(input.text) }),
            errorType: "encoding_error"
        )

    case "challenge.id":
        let input: ChallengeIdInput = try decodeInput(request.input, errorType: "generation_error")
        return try encodeValue(
            ChallengeIdOutput(id: generateChallengeId(input)),
            errorType: "generation_error"
        )

    default:
        throw AdapterFailure(type: "unsupported_operation", message: "Unknown operation: \(request.op)")
    }
}

func decodeInput<T: Decodable>(_ value: JSONValue, errorType: String) throws -> T {
    do {
        return try JSONDecoder().decode(T.self, from: try JSONEncoder().encode(value))
    } catch {
        throw AdapterFailure(type: errorType, message: String(describing: error))
    }
}

func encodeValue<T: Encodable>(_ value: T, errorType: String) throws -> JSONValue {
    do {
        return try JSONDecoder().decode(JSONValue.self, from: try JSONEncoder().encode(value))
    } catch {
        throw AdapterFailure(type: errorType, message: String(describing: error))
    }
}

func mapError<T>(_ errorType: String, _ body: () throws -> T) throws -> T {
    do {
        return try body()
    } catch {
        throw AdapterFailure(type: errorType, message: String(describing: error))
    }
}

func stringSource(_ source: JSONValue?) throws -> String? {
    guard let source, !source.isNull else {
        return nil
    }
    guard case let .string(text) = source else {
        throw AdapterFailure(type: "format_error", message: "source must be a string for mpp-swift")
    }
    return text
}

func validateChallenge(_ challenge: Challenge, errorType: String) throws {
    if challenge.id.isEmpty {
        throw AdapterFailure(type: errorType, message: "challenge id is required")
    }
    if challenge.method.isEmpty {
        throw AdapterFailure(type: errorType, message: "challenge method is required")
    }
    if challenge.intent.isEmpty {
        throw AdapterFailure(type: errorType, message: "challenge intent is required")
    }
}

func validateTimestamp(_ value: String, errorType: String) throws {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if formatter.date(from: value) != nil {
        return
    }
    formatter.formatOptions = [.withInternetDateTime]
    if formatter.date(from: value) == nil {
        throw AdapterFailure(type: errorType, message: "invalid receipt timestamp")
    }
}

func generateChallengeId(_ input: ChallengeIdInput) -> String {
    let requestB64 = PaymentRequest.serialize(PaymentRequest(fields: input.request))
    let hmacInput = [
        input.realm,
        input.method,
        input.intent,
        requestB64,
        input.expires ?? "",
        input.digest ?? "",
        input.opaque ?? "",
    ].joined(separator: "|")

    let key = SymmetricKey(data: Data(input.secretKey.utf8))
    let mac = HMAC<SHA256>.authenticationCode(for: Data(hmacInput.utf8), using: key)
    return Base64URL.encode(Data(mac))
}

func formatChallengeHeader(_ challenge: WireChallenge, opaque: String) -> String {
    var parts = [
        "id=\"\(escapeQuoted(challenge.id))\"",
        "realm=\"\(escapeQuoted(challenge.realm))\"",
        "method=\"\(escapeQuoted(challenge.method))\"",
        "intent=\"\(escapeQuoted(challenge.intent))\"",
        "request=\"\(escapeQuoted(PaymentRequest.serialize(PaymentRequest(fields: challenge.request))))\"",
    ]
    if let description = challenge.description {
        parts.append("description=\"\(escapeQuoted(description))\"")
    }
    if let digest = challenge.digest {
        parts.append("digest=\"\(escapeQuoted(digest))\"")
    }
    if let expires = challenge.expires {
        parts.append("expires=\"\(escapeQuoted(expires))\"")
    }
    parts.append("opaque=\"\(escapeQuoted(opaque))\"")
    return "Payment \(parts.joined(separator: ", "))"
}

func escapeQuoted(_ value: String) -> String {
    value
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
}

func printJSON(_ value: [String: JSONValue]) {
    let data = try! JSONEncoder().encode(JSONValue.object(value))
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}
