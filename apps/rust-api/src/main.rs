use std::net::SocketAddr;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    let config = Config::from_env()?;
    let address: SocketAddr = config.listen.parse()?;
    let state = AppState::new(config)?;
    let listener = TcpListener::bind(address).await?;

    tracing::info!(%address, "rust api edge listening");
    axum::serve(
        listener,
        router(state).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
