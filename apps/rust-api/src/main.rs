use std::net::SocketAddr;

use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    if let Some(name) = std::env::args().nth(1) {
        if ai_agent_stack_api::ops_cli::is_ops_command(&name) {
            let config = Config::from_env()?;
            let state = AppState::new(config)?;
            let args: Vec<String> = std::env::args().skip(2).collect();
            let code = match ai_agent_stack_api::ops_cli::run(&state, &name, &args).await {
                Ok(code) => code,
                Err(error) => {
                    eprintln!("{error}");
                    1
                }
            };
            std::process::exit(code);
        }
        eprintln!("unknown command: {name}");
        std::process::exit(2);
    }
    let config = Config::from_env()?;
    let address: SocketAddr = config.listen.parse()?;
    let state = AppState::new(config)?;
    state.migrate_identity().await?;
    ai_agent_stack_api::worker::spawn(state.clone());
    let listener = TcpListener::bind(address).await?;

    tracing::info!(%address, "rust api listening");
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
