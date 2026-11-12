//! The `http.server` operation: a payment-protected HTTP server for the pair
//! tests, built on mpp-rs's server engine. Each route is an `Mpp` handler per
//! offer behind mpp-rs's Tower `PaymentLayer`, which issues challenges,
//! verifies credentials and attaches receipts. Connections are read and
//! written here, one request each, like the adapter's other local servers.

use std::collections::{HashMap, HashSet};
use std::convert::Infallible;
use std::future::{poll_fn, Future};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use mpp::protocol::core::{
    format_receipt, format_www_authenticate_many, parse_authorization, Base64UrlJson,
    PaymentChallenge, PaymentCredential, Receipt,
};
use mpp::protocol::intents::ChargeRequest;
use mpp::server::middleware::{PaymentLayer, PaymentVerifier};
use mpp::server::Mpp;
use mpp::{ChargeMethod, ErrorCode, MppError, VerificationError};
use serde::Deserialize;
use serde_json::{json, Value};
use tower_layer::Layer;
use tower_service::Service;

/// The fixed receipt timestamp every stand-in settlement returns.
const RECEIPT_TIMESTAMP: &str = "2026-01-01T00:00:00Z";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct HttpServerInput {
    #[serde(default)]
    port: u16,
    realm: String,
    secret_key: String,
    #[serde(default)]
    requires_auth: bool,
    routes: Vec<RouteInput>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RouteInput {
    path: String,
    offers: Vec<OfferInput>,
    #[serde(default)]
    echo_headers: Vec<String>,
}

#[derive(Deserialize)]
struct OfferInput {
    method: String,
    #[serde(default)]
    intent: Option<String>,
    request: ChargeRequest,
    #[serde(default)]
    expires: Option<String>,
    #[serde(default)]
    meta: Option<Value>,
}

/// The stand-in charge method every offer uses: its payload is
/// `{ type: "transaction", signature }`, it declines the signature
/// `0xdecline`, settles each challenge once and returns a fixed receipt.
///
/// mpp-rs's expected-request verification runs Tempo transfer checks on any
/// credential whose method is `tempo`, which the stand-in's request (recipient
/// `merchant`) cannot pass, so each instance carries its route's request and
/// rejects a credential issued for another one, as that verification would.
#[derive(Clone)]
struct ConformanceChargeMethod {
    name: String,
    route_request: ChargeRequest,
    settled: Arc<Mutex<HashSet<String>>>,
}

impl ConformanceChargeMethod {
    fn settle(
        &self,
        credential: &PaymentCredential,
        request: &ChargeRequest,
    ) -> Result<Receipt, VerificationError> {
        if request.amount != self.route_request.amount
            || request.currency != self.route_request.currency
            || request.recipient != self.route_request.recipient
        {
            return Err(VerificationError::with_code(
                "credential was issued for a different route",
                ErrorCode::InvalidChallenge,
            ));
        }
        let payload = &credential.payload;
        let signature = match (
            payload.get("type").and_then(Value::as_str),
            payload.get("signature").and_then(Value::as_str),
        ) {
            (Some("transaction"), Some(signature)) => signature,
            _ => {
                return Err(VerificationError::with_code(
                    "payload must be { type: \"transaction\", signature: string }",
                    ErrorCode::InvalidPayload,
                ))
            }
        };
        if signature == "0xdecline" {
            return Err(VerificationError::new("payment declined"));
        }
        let first_use = self
            .settled
            .lock()
            .map(|mut settled| settled.insert(credential.challenge.id.clone()))
            .unwrap_or(false);
        if !first_use {
            return Err(VerificationError::with_code(
                "credential already used",
                ErrorCode::CredentialReplayed,
            ));
        }
        let mut receipt = Receipt::success(self.name.as_str(), format!("ref-{signature}"));
        receipt.timestamp = RECEIPT_TIMESTAMP.to_string();
        Ok(receipt)
    }
}

impl ChargeMethod for ConformanceChargeMethod {
    fn method(&self) -> &str {
        &self.name
    }

    fn verify(
        &self,
        credential: &PaymentCredential,
        request: &ChargeRequest,
    ) -> impl Future<Output = Result<Receipt, VerificationError>> + Send {
        std::future::ready(self.settle(credential, request))
    }
}

/// One offer of a route: its handler and the challenge it issues.
struct RouteOffer {
    payment: Mpp<ConformanceChargeMethod>,
    request: Base64UrlJson,
    expires: Option<String>,
    opaque: Option<Base64UrlJson>,
}

/// The `PaymentVerifier` of one route: one challenge per offer, and each
/// credential verified by the handler of the offer it echoes.
struct RouteVerifier {
    secret_key: String,
    offers: Arc<Vec<RouteOffer>>,
    credential_header: String,
}

impl RouteVerifier {
    fn challenges(&self) -> Vec<PaymentChallenge> {
        self.offers
            .iter()
            .map(|offer| {
                let payment = &offer.payment;
                PaymentChallenge::with_secret_key_full(
                    &self.secret_key,
                    payment.realm(),
                    payment.method_name(),
                    "charge",
                    offer.request.clone(),
                    offer.expires.as_deref(),
                    None,
                    None,
                    offer.opaque.clone(),
                    payment.requires_auth().then(|| payment.credential_header()),
                )
            })
            .collect()
    }
}

/// The offer whose method and request the credential echoes, else the first
/// offer of its method, else the first offer (whose handler rejects it).
fn offer_for<'a>(offers: &'a [RouteOffer], credential: &PaymentCredential) -> &'a RouteOffer {
    let method = credential.challenge.method.as_str();
    let request = credential.challenge.request.raw();
    offers
        .iter()
        .find(|offer| offer.payment.method_name() == method && offer.request.raw() == request)
        .or_else(|| {
            offers
                .iter()
                .find(|offer| offer.payment.method_name() == method)
        })
        .unwrap_or(&offers[0])
}

impl PaymentVerifier for RouteVerifier {
    fn challenge(&self) -> Result<String, String> {
        format_www_authenticate_many(&self.challenges())
            .map(|values| values.join(", "))
            .map_err(|e| format!("Failed to format challenge: {e}"))
    }

    fn verify(
        &self,
        credential: &str,
    ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send>> {
        let verified = self.verify_credential(credential, None);
        Box::pin(async move { verified.await.map_err(|e| e.to_string()) })
    }

    fn verify_credential(
        &self,
        credential: &str,
        _body: Option<&[u8]>,
    ) -> Pin<Box<dyn Future<Output = Result<String, MppError>> + Send>> {
        let offers = Arc::clone(&self.offers);
        let credential = credential.to_string();
        Box::pin(async move {
            let credential = parse_authorization(&credential)?;
            let receipt = offer_for(&offers, &credential)
                .payment
                .broadcast_credential(&credential)
                .await?;
            format_receipt(&receipt)
        })
    }

    fn credential_header(&self) -> &str {
        &self.credential_header
    }
}

/// The paid response of a route: `{ ok: true, path }` plus each echoed
/// request header's value, or null.
#[derive(Clone)]
struct PaidRoute {
    path: String,
    echo_headers: Arc<Vec<String>>,
}

impl Service<http::Request<()>> for PaidRoute {
    type Response = http::Response<Vec<u8>>;
    type Error = Infallible;
    type Future = std::future::Ready<Result<Self::Response, Infallible>>;

    fn poll_ready(
        &mut self,
        _: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Result<(), Infallible>> {
        std::task::Poll::Ready(Ok(()))
    }

    fn call(&mut self, request: http::Request<()>) -> Self::Future {
        let mut body = serde_json::Map::new();
        body.insert("ok".into(), json!(true));
        body.insert("path".into(), json!(self.path));
        for name in self.echo_headers.iter() {
            let value = request
                .headers()
                .get(name.as_str())
                .and_then(|value| value.to_str().ok());
            body.insert(name.clone(), json!(value));
        }
        let mut response = http::Response::new(Value::Object(body).to_string().into_bytes());
        response.headers_mut().insert(
            http::header::CONTENT_TYPE,
            http::HeaderValue::from_static("application/json"),
        );
        std::future::ready(Ok(response))
    }
}

type ResponseFuture = Pin<Box<dyn Future<Output = http::Response<Vec<u8>>> + Send>>;
type RouteHandler = Arc<dyn Fn(http::Request<()>) -> ResponseFuture + Send + Sync>;

/// Wrap a route's paid response in mpp-rs's payment layer with RFC 9457
/// problem bodies, as mppx answers.
fn protect(verifier: RouteVerifier, route: PaidRoute) -> RouteHandler {
    let service = PaymentLayer::new(verifier)
        .with_problem_details(|body| body.to_vec())
        .layer(route);
    Arc::new(move |request| {
        let mut service = service.clone();
        Box::pin(async move {
            let ready = poll_fn(|cx| service.poll_ready(cx)).await;
            match ready {
                Ok(()) => match service.call(request).await {
                    Ok(response) => response,
                    Err(never) => match never {},
                },
                Err(never) => match never {},
            }
        })
    })
}

fn build_routes(input: HttpServerInput) -> Result<HashMap<String, RouteHandler>, String> {
    let settled = Arc::new(Mutex::new(HashSet::new()));
    let mut routes = HashMap::new();
    for route in input.routes {
        if route.offers.is_empty() {
            return Err(format!("route {} has no offers", route.path));
        }
        let mut offers = Vec::with_capacity(route.offers.len());
        for offer in route.offers {
            if offer
                .intent
                .as_deref()
                .is_some_and(|intent| intent != "charge")
            {
                return Err(format!("unsupported intent {:?}", offer.intent));
            }
            let request = Base64UrlJson::from_typed(&offer.request).map_err(|e| e.to_string())?;
            let opaque = offer
                .meta
                .as_ref()
                .map(Base64UrlJson::from_value)
                .transpose()
                .map_err(|e| e.to_string())?;
            let method = ConformanceChargeMethod {
                name: offer.method,
                route_request: offer.request,
                settled: Arc::clone(&settled),
            };
            let mut payment = Mpp::new(method, input.realm.as_str(), input.secret_key.as_str())
                .with_requires_auth(input.requires_auth);
            if let Some(opaque) = &opaque {
                payment = payment.with_opaque(opaque.clone());
            }
            offers.push(RouteOffer {
                payment,
                request,
                expires: offer.expires,
                opaque,
            });
        }
        let credential_header = offers[0].payment.credential_header().to_string();
        let verifier = RouteVerifier {
            secret_key: input.secret_key.clone(),
            offers: Arc::new(offers),
            credential_header,
        };
        let paid = PaidRoute {
            path: route.path.clone(),
            echo_headers: Arc::new(route.echo_headers),
        };
        routes.insert(route.path, protect(verifier, paid));
    }
    Ok(routes)
}

/// A started `http.server`: its listener and route handlers.
pub struct HttpServer {
    listener: TcpListener,
    routes: Arc<HashMap<String, RouteHandler>>,
}

impl HttpServer {
    /// Build the scenario's routes and listen on `127.0.0.1:<port>`.
    pub fn start(input: &Value) -> Result<Self, String> {
        let input: HttpServerInput =
            serde_json::from_value(input.clone()).map_err(|e| e.to_string())?;
        let port = input.port;
        let routes = build_routes(input)?;
        let listener = TcpListener::bind(("127.0.0.1", port)).map_err(|e| e.to_string())?;
        Ok(Self {
            listener,
            routes: Arc::new(routes),
        })
    }

    /// The server's base URL.
    pub fn url(&self) -> Result<String, String> {
        let address = self.listener.local_addr().map_err(|e| e.to_string())?;
        Ok(format!("http://{address}"))
    }

    /// Serve one request per connection until the process is terminated.
    pub fn serve(self) {
        for stream in self.listener.incoming() {
            let Ok(stream) = stream else { continue };
            let routes = Arc::clone(&self.routes);
            std::thread::spawn(move || {
                let _ = answer(stream, &routes);
            });
        }
    }
}

fn answer(stream: TcpStream, routes: &HashMap<String, RouteHandler>) -> io::Result<()> {
    let invalid = |e: http::Error| io::Error::new(io::ErrorKind::InvalidData, e);
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("GET").to_string();
    let target = parts.next().unwrap_or("/").to_string();
    let mut builder = http::Request::builder()
        .method(method.as_str())
        .uri(target.as_str());
    let mut content_length = 0;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 || line == "\r\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            let value = value.trim();
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.parse().unwrap_or(0);
            }
            builder = builder.header(name.trim(), value);
        }
    }
    let mut body = vec![0; content_length];
    reader.read_exact(&mut body)?;
    let request = builder.body(()).map_err(invalid)?;

    let path = target.split('?').next().unwrap_or("/");
    let response = match routes.get(path) {
        Some(handler) => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(handler(request))
        }
        None => http::Response::builder()
            .status(http::StatusCode::NOT_FOUND)
            .body(b"not found".to_vec())
            .map_err(invalid)?,
    };

    let mut stream = stream;
    let status = response.status();
    write!(
        stream,
        "HTTP/1.1 {} {}\r\n",
        status.as_u16(),
        status.canonical_reason().unwrap_or("")
    )?;
    for (name, value) in response.headers() {
        if name == http::header::CONTENT_LENGTH || name == http::header::CONNECTION {
            continue;
        }
        stream.write_all(name.as_str().as_bytes())?;
        stream.write_all(b": ")?;
        stream.write_all(value.as_bytes())?;
        stream.write_all(b"\r\n")?;
    }
    let body = response.body();
    write!(
        stream,
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body)?;
    stream.flush()
}
