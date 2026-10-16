pub mod core {
    pub mod autostart;
    pub mod everything;
    pub mod explorer;
    pub mod hotkey;
    pub mod icon_provider;
    pub mod injector;
    pub mod ranking;
    pub mod scale_manager;
    pub mod single_instance;
    pub mod window_helper;
}

pub mod host {
    pub mod message_host;
    pub mod tray;
}

pub mod utils {
    pub mod logger;
    pub mod paths;
}
