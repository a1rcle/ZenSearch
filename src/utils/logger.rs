use crate::utils::paths::get_log_dir;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter, Layer};

pub fn init_logger(debug_mode: bool) -> WorkerGuard {
    let log_dir = get_log_dir();
    let file_appender = tracing_appender::rolling::daily(&log_dir, "zensearch.log");
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);

    let file_layer = tracing_subscriber::fmt::layer()
        .with_writer(non_blocking)
        .with_ansi(false)
        .with_target(true)
        .with_thread_ids(true);

    let console_filter = if debug_mode {
        EnvFilter::new("zensearch=trace,info")
    } else {
        EnvFilter::new("zensearch=info,warn,error")
    };

    let console_layer = if debug_mode {
        Some(
            tracing_subscriber::fmt::layer()
                .with_writer(std::io::stdout)
                .with_ansi(true)
                .with_filter(console_filter),
        )
    } else {
        None
    };

    let _ = tracing_subscriber::registry()
        .with(file_layer)
        .with(console_layer)
        .try_init();

    tracing::info!("Logger initialized. Log directory: {:?}", log_dir);
    guard
}
