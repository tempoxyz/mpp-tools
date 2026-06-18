package xyz.tempo.mpp.conformance;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.stripe.mpp.Challenge;
import com.stripe.mpp.ChallengeEcho;
import com.stripe.mpp.ChallengeId;
import com.stripe.mpp.Credential;
import com.stripe.mpp.Receipt;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class Adapter {
    private static final Pattern AUTH_PARAM = Pattern.compile(
        "(\\w+)=(?:\"([^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"|([^\\s,]+))"
    );

    private static final ObjectMapper MAPPER = JsonMapper.builder()
        .enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS)
        .build();

    private static final List<String> SUPPORTED_OPERATIONS = List.of(
        "challenge.parse",
        "challenge.format",
        "credential.parse",
        "credential.format",
        "receipt.parse",
        "receipt.format",
        "base64url.encode",
        "base64url.decode",
        "challenge.id"
    );

    private Adapter() {}

    public static void main(String[] args) {
        try {
            String input = readStdin();
            writeJson(args.length == 0
                ? handleAdapterRequest(input)
                : adapterError("Command arguments are not supported", "unsupported_operation"));
        } catch (Exception exc) {
            try {
                writeJson(adapterError(exc.getMessage(), "unknown_error"));
            } catch (IOException ignored) {
                System.out.println("{\"ok\":false,\"error\":{\"type\":\"unknown_error\",\"message\":\"unknown failure\"}}");
            }
        }
    }

    private static String readStdin() throws IOException {
        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8).trim();
    }

    private static Map<String, Object> handleAdapterRequest(String input) {
        try {
            Map<String, Object> request = readJsonObject(input, "adapter request", "unknown_error");
            String op = requiredString(request, "op", "unknown_error", false);
            if (!SUPPORTED_OPERATIONS.contains(op)) {
                return adapterError("Unknown operation: " + op, "unsupported_operation");
            }
            Object value = runOperation(op, request.get("input"));
            return adapterSuccess(value);
        } catch (AdapterFailure failure) {
            return adapterError(failure.getMessage(), failure.type);
        } catch (Exception exc) {
            return adapterError(exc.getMessage(), "unknown_error");
        }
    }

    private static Object runOperation(String op, Object input) throws AdapterFailure {
        if ("challenge.parse".equals(op)) {
            Map<String, Object> data = requireMap(input, "input", "parse_error");
            return parseChallenge(requiredString(data, "header", "parse_error", false));
        }
        if ("challenge.format".equals(op)) {
            return mapOf("header", formatChallenge(requireMap(input, "challenge", "format_error")));
        }
        if ("credential.parse".equals(op)) {
            Map<String, Object> data = requireMap(input, "input", "parse_error");
            return parseCredential(requiredString(data, "header", "parse_error", false));
        }
        if ("credential.format".equals(op)) {
            return mapOf("header", formatCredential(requireMap(input, "credential", "format_error")));
        }
        if ("receipt.parse".equals(op)) {
            Map<String, Object> data = requireMap(input, "input", "parse_error");
            return parseReceipt(requiredString(data, "header", "parse_error", false));
        }
        if ("receipt.format".equals(op)) {
            return mapOf("header", formatReceipt(requireMap(input, "receipt", "format_error")));
        }
        if ("base64url.encode".equals(op)) {
            Map<String, Object> data = requireMap(input, "input", "encoding_error");
            return mapOf("text", ChallengeId.b64urlEncode(requiredString(data, "text", "encoding_error", true)));
        }
        if ("base64url.decode".equals(op)) {
            Map<String, Object> data = requireMap(input, "input", "encoding_error");
            return mapOf("text", base64urlDecodeToString(requiredString(data, "text", "encoding_error", true), "text", "encoding_error"));
        }
        if ("challenge.id".equals(op)) {
            return mapOf("id", generateChallengeId(requireMap(input, "challenge id input", "generation_error")));
        }
        throw new AdapterFailure("unsupported_operation", "Unknown operation: " + op);
    }

    private static Map<String, Object> parseChallenge(String header) throws AdapterFailure {
        String authParams = extractPaymentAuthParams(header);
        if (authParams == null) {
            throw new AdapterFailure("parse_error", "Missing Payment challenge");
        }
        parseAuthParams(authParams);

        List<Challenge> challenges;
        try {
            challenges = Challenge.fromWwwAuthenticate(header);
        } catch (RuntimeException exc) {
            throw new AdapterFailure("parse_error", exc.getMessage());
        }
        if (challenges.isEmpty()) {
            throw new AdapterFailure("parse_error", "Missing Payment challenge");
        }

        Challenge challenge = challenges.get(0);
        requirePresent(challenge.id(), "id", "parse_error", false);
        requirePresent(challenge.realm(), "realm", "parse_error", true);
        requirePresent(challenge.method(), "method", "parse_error", false);
        requirePresent(challenge.intent(), "intent", "parse_error", false);
        requirePresent(challenge.requestB64(), "request", "parse_error", false);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", challenge.id());
        result.put("realm", challenge.realm());
        result.put("method", challenge.method());
        result.put("intent", challenge.intent());
        result.put("request", decodeJsonObjectBase64Url(challenge.requestB64(), "request", "parse_error"));
        putIfNotNull(result, "expires", challenge.expires());
        putIfNotNull(result, "description", challenge.description());
        putIfNotNull(result, "digest", challenge.digest());
        if (challenge.opaque() != null) {
            result.put("opaque", stableJson(challenge.opaque(), "parse_error"));
        }
        return result;
    }

    private static String formatChallenge(Map<String, Object> data) throws AdapterFailure {
        Map<String, Object> request = requireMap(data.get("request"), "request", "format_error");
        String requestB64 = encodeJsonBase64Url(request, "format_error");
        String opaque = optionalString(data, "opaque", "format_error");

        Challenge challenge = new Challenge(
            requiredString(data, "id", "format_error", false),
            requiredString(data, "method", "format_error", false),
            requiredString(data, "intent", "format_error", false),
            request,
            requiredString(data, "realm", "format_error", true),
            requestB64,
            optionalString(data, "digest", "format_error"),
            optionalString(data, "expires", "format_error"),
            optionalString(data, "description", "format_error"),
            null
        );

        try {
            if (opaque == null) {
                return challenge.toWwwAuthenticate();
            }

            List<String> parts = new ArrayList<>();
            parts.add("id=" + quote(challenge.id()));
            parts.add("realm=" + quote(challenge.realm()));
            parts.add("method=" + quote(challenge.method()));
            parts.add("intent=" + quote(challenge.intent()));
            parts.add("request=" + quote(challenge.requestB64()));
            if (challenge.expires() != null) parts.add("expires=" + quote(challenge.expires()));
            if (challenge.digest() != null) parts.add("digest=" + quote(challenge.digest()));
            if (challenge.description() != null) parts.add("description=" + quote(challenge.description()));
            parts.add("opaque=" + quote(opaque));
            return "Payment " + String.join(", ", parts);
        } catch (RuntimeException exc) {
            throw new AdapterFailure("format_error", exc.getMessage());
        }
    }

    private static Map<String, Object> parseCredential(String header) throws AdapterFailure {
        String trimmed = header.trim();
        if (!trimmed.toLowerCase(Locale.ROOT).startsWith("payment ")) {
            throw new AdapterFailure("parse_error", "Missing Payment credential");
        }

        String encoded = trimmed.substring("Payment ".length()).trim();
        Map<String, Object> wire = decodeJsonObjectBase64Url(encoded, "credential", "parse_error");
        Credential credential;
        try {
            credential = Credential.fromAuthorization(trimmed);
        } catch (RuntimeException exc) {
            throw new AdapterFailure("parse_error", exc.getMessage());
        }

        Map<String, Object> payload = requireMap(credential.payload(), "payload", "parse_error");

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("challenge", challengeEchoToMap(credential.challenge(), "parse_error"));
        result.put("payload", payload);
        if (wire.containsKey("source")) {
            result.put("source", wire.get("source"));
        }
        return result;
    }

    private static String formatCredential(Map<String, Object> data) throws AdapterFailure {
        Map<String, Object> challenge = requireMap(data.get("challenge"), "challenge", "format_error");
        Map<String, Object> payload = requireMap(data.get("payload"), "payload", "format_error");
        String requestB64 = encodeJsonBase64Url(requireMap(challenge.get("request"), "request", "format_error"), "format_error");
        optionalString(challenge, "opaque", "format_error");

        ChallengeEcho echo = new ChallengeEcho(
            requiredString(challenge, "id", "format_error", false),
            requiredString(challenge, "realm", "format_error", true),
            requiredString(challenge, "method", "format_error", false),
            requiredString(challenge, "intent", "format_error", false),
            requestB64,
            optionalString(challenge, "expires", "format_error"),
            optionalString(challenge, "digest", "format_error"),
            null
        );
        Object source = data.get("source");
        if (source != null && !(source instanceof String)) {
            throw new AdapterFailure("format_error", "source must be a string for mpp-java");
        }

        try {
            return new Credential(echo, payload, (String) source).toAuthorization();
        } catch (RuntimeException exc) {
            throw new AdapterFailure("format_error", exc.getMessage());
        }
    }

    private static Map<String, Object> challengeEchoToMap(ChallengeEcho echo, String errorType) throws AdapterFailure {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", requirePresent(echo.id(), "id", errorType, false));
        result.put("realm", requirePresent(echo.realm(), "realm", errorType, true));
        result.put("method", requirePresent(echo.method(), "method", errorType, false));
        result.put("intent", requirePresent(echo.intent(), "intent", errorType, false));
        result.put("request", decodeJsonObjectBase64Url(requirePresent(echo.request(), "request", errorType, false), "request", errorType));
        putIfNotNull(result, "expires", echo.expires());
        putIfNotNull(result, "digest", echo.digest());
        if (echo.opaque() != null) {
            result.put("opaque", stableJson(echo.opaque(), errorType));
        }
        return result;
    }

    private static Map<String, Object> parseReceipt(String header) throws AdapterFailure {
        Receipt receipt;
        try {
            receipt = Receipt.fromPaymentReceipt(header.trim());
        } catch (RuntimeException exc) {
            throw new AdapterFailure("parse_error", exc.getMessage());
        }
        requirePresent(receipt.method(), "method", "parse_error", false);
        return receiptToMap(receipt);
    }

    private static String formatReceipt(Map<String, Object> data) throws AdapterFailure {
        Instant timestamp;
        String timestampRaw = requiredString(data, "timestamp", "format_error", false);
        try {
            timestamp = Instant.parse(timestampRaw);
        } catch (RuntimeException exc) {
            throw new AdapterFailure("format_error", "Invalid timestamp: " + timestampRaw);
        }

        Receipt receipt = new Receipt(
            requiredString(data, "status", "format_error", false),
            timestamp,
            requiredString(data, "reference", "format_error", false),
            optionalString(data, "method", "format_error"),
            optionalString(data, "externalId", "format_error"),
            data.get("extra")
        );

        try {
            return receipt.toPaymentReceipt();
        } catch (RuntimeException exc) {
            throw new AdapterFailure("format_error", exc.getMessage());
        }
    }

    private static Map<String, Object> receiptToMap(Receipt receipt) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", receipt.status());
        if (receipt.method() != null && !receipt.method().isEmpty()) {
            result.put("method", receipt.method());
        }
        result.put("timestamp", DateTimeFormatter.ISO_INSTANT.format(receipt.timestamp()));
        result.put("reference", receipt.reference());
        putIfNotNull(result, "externalId", receipt.externalId());
        putIfNotNull(result, "extra", receipt.extra());
        return result;
    }

    private static String generateChallengeId(Map<String, Object> data) throws AdapterFailure {
        String secretKey = requiredString(data, "secretKey", "generation_error", false);
        Map<String, Object> request = requireMap(data.get("request"), "request", "generation_error");
        String expires = optionalString(data, "expires", "generation_error");
        String digest = optionalString(data, "digest", "generation_error");
        String opaque = optionalString(data, "opaque", "generation_error");

        if (opaque == null) {
            try {
                return ChallengeId.generate(
                    secretKey,
                    requiredString(data, "realm", "generation_error", true),
                    requiredString(data, "method", "generation_error", true),
                    requiredString(data, "intent", "generation_error", true),
                    request,
                    expires,
                    digest,
                    null
                );
            } catch (RuntimeException exc) {
                throw new AdapterFailure("generation_error", exc.getMessage());
            }
        }

        String requestB64 = encodeJsonBase64Url(request, "generation_error");
        String input = String.join("|",
            requiredString(data, "realm", "generation_error", true),
            requiredString(data, "method", "generation_error", true),
            requiredString(data, "intent", "generation_error", true),
            requestB64,
            expires == null ? "" : expires,
            digest == null ? "" : digest,
            opaque
        );

        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secretKey.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return ChallengeId.b64urlEncodeBytes(mac.doFinal(input.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exc) {
            throw new AdapterFailure("generation_error", exc.getMessage());
        }
    }

    private static String encodeJsonBase64Url(Object value, String errorType) throws AdapterFailure {
        return ChallengeId.b64urlEncodeBytes(stableJson(value, errorType).getBytes(StandardCharsets.UTF_8));
    }

    private static Map<String, Object> decodeJsonObjectBase64Url(String encoded, String label, String errorType) throws AdapterFailure {
        byte[] decoded = decodeBase64Url(encoded, label, errorType);
        try {
            Object value = MAPPER.readValue(decoded, Object.class);
            return requireMap(value, label, errorType);
        } catch (IOException exc) {
            throw new AdapterFailure(errorType, "Invalid JSON in " + label + ": " + exc.getMessage());
        }
    }

    private static String base64urlDecodeToString(String encoded, String label, String errorType) throws AdapterFailure {
        byte[] decoded = decodeBase64Url(encoded, label, errorType);
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(decoded))
                .toString();
        } catch (CharacterCodingException exc) {
            throw new AdapterFailure(errorType, "Invalid UTF-8 in " + label);
        }
    }

    private static byte[] decodeBase64Url(String encoded, String label, String errorType) throws AdapterFailure {
        try {
            return ChallengeId.b64urlDecode(encoded);
        } catch (IllegalArgumentException exc) {
            throw new AdapterFailure(errorType, "Invalid base64url in " + label + ": " + exc.getMessage());
        }
    }

    private static String stableJson(Object value, String errorType) throws AdapterFailure {
        try {
            return MAPPER.writeValueAsString(value);
        } catch (JsonProcessingException exc) {
            throw new AdapterFailure(errorType, "JSON serialization failed: " + exc.getMessage());
        }
    }

    private static String extractPaymentAuthParams(String header) {
        String lower = header.toLowerCase(Locale.ROOT);
        if (lower.startsWith("payment ")) {
            return header.substring("payment ".length());
        }
        int index = lower.indexOf(", payment ");
        if (index >= 0) {
            return header.substring(index + ", payment ".length());
        }
        return null;
    }

    private static Map<String, String> parseAuthParams(String input) throws AdapterFailure {
        Map<String, String> params = new LinkedHashMap<>();
        Matcher matcher = AUTH_PARAM.matcher(input);
        while (matcher.find()) {
            String key = matcher.group(1);
            if (params.containsKey(key)) {
                throw new AdapterFailure("parse_error", "Duplicate parameter: " + key);
            }
            String value = matcher.group(2) != null ? matcher.group(2) : matcher.group(3);
            if (matcher.group(2) != null) {
                value = value.replace("\\\"", "\"").replace("\\\\", "\\");
            }
            params.put(key, value);
        }
        return params;
    }

    private static Map<String, Object> readJsonObject(String input, String label, String errorType) throws AdapterFailure {
        try {
            Object value = MAPPER.readValue(input, Object.class);
            return requireMap(value, label, errorType);
        } catch (IOException exc) {
            throw new AdapterFailure(errorType, "Invalid JSON for " + label + ": " + exc.getMessage());
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> requireMap(Object value, String label, String errorType) throws AdapterFailure {
        if (value instanceof Map) {
            return (Map<String, Object>) value;
        }
        throw new AdapterFailure(errorType, label + " must be an object");
    }

    private static String requiredString(Map<String, Object> data, String key, String errorType, boolean allowEmpty) throws AdapterFailure {
        if (!data.containsKey(key)) {
            throw new AdapterFailure(errorType, "Missing " + key);
        }
        Object value = data.get(key);
        if (!(value instanceof String)) {
            throw new AdapterFailure(errorType, key + " must be a string");
        }
        return requirePresent((String) value, key, errorType, allowEmpty);
    }

    private static String optionalString(Map<String, Object> data, String key, String errorType) throws AdapterFailure {
        if (!data.containsKey(key) || data.get(key) == null) {
            return null;
        }
        Object value = data.get(key);
        if (!(value instanceof String)) {
            throw new AdapterFailure(errorType, key + " must be a string");
        }
        return (String) value;
    }

    private static String requirePresent(String value, String key, String errorType, boolean allowEmpty) throws AdapterFailure {
        if (value == null || (!allowEmpty && value.isEmpty())) {
            throw new AdapterFailure(errorType, "Missing " + key);
        }
        return value;
    }

    private static void putIfNotNull(Map<String, Object> target, String key, Object value) {
        if (value != null) {
            target.put(key, value);
        }
    }

    private static Map<String, Object> adapterSuccess(Object value) {
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        response.put("value", value);
        return response;
    }

    private static Map<String, Object> adapterError(String message, String errorType) {
        Map<String, Object> error = new LinkedHashMap<>();
        error.put("type", errorType);
        error.put("message", message == null ? "" : message);

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", false);
        response.put("error", error);
        return response;
    }

    private static Map<String, Object> mapOf(String key, Object value) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put(key, value);
        return result;
    }

    private static String quote(String value) {
        if (value.indexOf('\r') >= 0 || value.indexOf('\n') >= 0) {
            throw new IllegalArgumentException("Header values must not contain CR or LF");
        }
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private static void writeJson(Object value) throws IOException {
        System.out.println(MAPPER.writeValueAsString(value));
    }

    private static final class AdapterFailure extends Exception {
        private final String type;

        private AdapterFailure(String type, String message) {
            super(message);
            this.type = type;
        }
    }
}
