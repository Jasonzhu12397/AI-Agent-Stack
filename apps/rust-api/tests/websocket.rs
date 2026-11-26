use std::net::SocketAddr;
use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::http::{Request as AxumRequest, StatusCode};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::Message;

struct UpstreamHandshake {
    host: String,
    cookie: String,
    protocols: String,
    uri: String,
}

#[allow(clippy::result_large_err)]
async fn start_echo_upstream() -> (String, oneshot::Receiver<UpstreamHandshake>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (tx, rx) = oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut sender = Some(tx);
        let callback = move |request: &Request, mut response: Response| {
            if let Some(sender) = sender.take() {
                let _ = sender.send(UpstreamHandshake {
                    host: header(request, "host"),
                    cookie: header(request, "cookie"),
                    protocols: header(request, "sec-websocket-protocol"),
                    uri: request.uri().to_string(),
                });
            }
            response
                .headers_mut()
                .insert("sec-websocket-protocol", "aiagentstack".parse().unwrap());
            Ok(response)
        };
        let mut socket = tokio_tungstenite::accept_hdr_async(stream, callback)
            .await
            .unwrap();
        while let Some(Ok(message)) = socket.next().await {
            match message {
                Message::Text(text) => {
                    let _ = socket.send(Message::Text(text)).await;
                }
                Message::Close(frame) => {
                    let _ = socket.send(Message::Close(frame)).await;
                    break;
                }
                _ => {}
            }
        }
    });
    (format!("ws://{address}"), rx)
}

fn header(request: &Request, name: &str) -> String {
    request
        .headers()
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("missing")
        .to_string()
}

async fn start_edge(upstream: String) -> SocketAddr {
    let state = AppState::new(
        Config::new(
            "127.0.0.1:0".into(),
            upstream,
            Duration::from_secs(2),
            Duration::from_secs(10),
        )
        .unwrap(),
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(
            listener,
            router(state).into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await
        .unwrap();
    });
    address
}

fn client_request(address: SocketAddr, path: &str) -> AxumRequest<()> {
    let uri = format!("ws://{address}{path}");
    let mut request = uri.into_client_request().unwrap();
    request.headers_mut().insert(
        "cookie",
        "sessionid=edge-test-session; csrftoken=edge-test-csrf"
            .parse()
            .unwrap(),
    );
    request.headers_mut().insert(
        "sec-websocket-protocol",
        "aiagentstack, session.edge-test-session".parse().unwrap(),
    );
    request
}

#[tokio::test]
async fn forwards_websocket_handshake_headers_and_messages() {
    let (upstream, handshake) = start_echo_upstream().await;
    let http_upstream = upstream.replace("ws://", "http://");
    let edge = start_edge(http_upstream).await;

    let (mut socket, response) =
        tokio_tungstenite::connect_async(client_request(
            edge,
            "/ws/tenants/11111111-1111-1111-1111-111111111111/projects/22222222-2222-2222-2222-222222222222/events/?since=2026-11-26T00:00:00Z",
        ))
        .await
        .expect("edge must complete the browser handshake");

    assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
    assert_eq!(
        response
            .headers()
            .get("sec-websocket-protocol")
            .and_then(|value| value.to_str().ok()),
        Some("aiagentstack")
    );

    let observed = handshake.await.expect("upstream must be handed a request");
    assert_eq!(
        observed.cookie,
        "sessionid=edge-test-session; csrftoken=edge-test-csrf"
    );
    assert_eq!(
        observed.protocols,
        "aiagentstack, session.edge-test-session"
    );
    assert_eq!(
        observed.uri,
        "/ws/tenants/11111111-1111-1111-1111-111111111111/projects/22222222-2222-2222-2222-222222222222/events/?since=2026-11-26T00:00:00Z"
    );
    assert!(!observed.host.is_empty());

    socket
        .send(Message::Text("ready-ping".into()))
        .await
        .unwrap();
    let echoed = tokio::time::timeout(Duration::from_secs(3), socket.next())
        .await
        .expect("echo must arrive")
        .expect("socket must stay open")
        .expect("frame must decode");
    assert_eq!(echoed, Message::Text("ready-ping".into()));

    socket.close(None).await.unwrap();
}

#[tokio::test]
async fn reports_an_unavailable_websocket_upstream_with_close_1011() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream = listener.local_addr().unwrap();
    drop(listener);

    let edge = start_edge(format!("http://{upstream}")).await;
    let (mut socket, response) = tokio_tungstenite::connect_async(client_request(
        edge,
        "/ws/tenants/11111111-1111-1111-1111-111111111111/projects/22222222-2222-2222-2222-222222222222/events/",
    ))
    .await
    .expect("the browser handshake completes before the upstream is dialled");

    assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
    let close = tokio::time::timeout(Duration::from_secs(5), socket.next())
        .await
        .expect("edge must close promptly")
        .expect("a frame must arrive")
        .expect("close frame must decode");
    match close {
        Message::Close(Some(frame)) => assert_eq!(u16::from(frame.code), 1011),
        other => panic!("expected close 1011, received {other:?}"),
    }
}
