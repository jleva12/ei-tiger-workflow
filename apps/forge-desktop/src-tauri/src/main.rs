//! Forge Agents: the chat agents organizations share as chat pages, in a
//! desktop window.
//!
//! The window shows the web console's chat pages (apps/forge-web's
//! `src/desktop`, built into `dist-desktop/`); it talks to each agent's
//! Forge server over HTTPS as a browser would. This shell adds what a
//! browser tab can't: the window, its saved size and place, a store for the
//! added agents (`agents.json` in the app's data folder), links in answers
//! opening in the person's own browser instead of the window, and its own
//! links: `forge-agents://add?server=…&token=cp_…` (a chat page's "Open in
//! desktop app") reaches the app, already open or not, and the screen offers
//! to add the agent (`src/desktop/deep-links.ts`).

// No console window beside the app on Windows, in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, Url, WebviewWindowBuilder, Wry};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_opener::OpenerExt;

/// The window tauri.conf.json describes.
const MAIN: &str = "main";

/// Main entry point for the Forge Agents application.
///
/// This function initializes the Tauri application and sets up the required plugins, handlers,
/// and configurations for the application to function correctly. The application uses multiple
/// Tauri plugins to enable various functionalities, including deep linking, persistent window
/// state, URL opening, and data storage.
///
/// # Functionality Overview
/// 1. **Single-Instance Handling**:
///    - Ensures only one instance of the application is running. Subsequent instances hand over
///      their data to the existing running instance and stop execution.
///    - Managed using the `tauri_plugin_single_instance` plugin with a custom callback that
///      triggers the `show` function.
///
/// 2. **Deep Linking**:
///    - Enables the application to handle custom schemes (e.g., `forge-agents://`) for deep links.
///    - The `tauri_plugin_deep_link` plugin is used to register and process deep links. On Windows
///      and Linux, it uses runtime registration. On macOS, registration is derived from
///      `Info.plist`.
///
/// 3. **Window State Persistence**:
///    - Saves and restores the application's window state (e.g., size, position) between sessions.
///    - Managed through `tauri_plugin_window_state`.
///
/// 4. **Data Storage**:
///    - Provides a local, persistent storage mechanism for the application.
///    - Handled by the `tauri_plugin_store` plugin.
///
/// 5. **External URL Handling**:
///    - Configures navigation and new window behavior based on URLs.
///    - URLs not owned by the application are opened externally using the `open_outside` function.
///
/// 6. **Window Initialization**:
///    - Creates and configures the main application window described in `tauri.conf.json`.
///    - Configures navigation handling with the `on_navigation` callback and prevents new windows
///      from being opened using `on_new_window`.
///
/// # Important Notes
/// - The `tauri.conf.json` file must define window configurations with `"create": false`.
///   This ensures the window is programmatically created using the Tauri API during runtime.
/// - The `dev_url` specified in the configuration is used to determine whether a URL should be
///   handled internally or externally.
///
/// # Error Handling
/// - If the application fails to find any window configuration in `tauri.conf.json`, an error
///   is returned, and the application will terminate.
/// - Ensures proper error handling during window creation and plugin setup to avoid unexpected
///   crashes.
///
/// # Dependencies
/// - The application relies on the following Tauri plugins:
///   - `tauri_plugin_single_instance`
///   - `tauri_plugin_deep_link`
///   - `tauri_plugin_window_state`
///   - `tauri_plugin_store`
///   - `tauri_plugin_opener`
///
/// # Example Call Sequence
/// ```rust
/// fn main() {
///     tauri::Builder::default()
///         .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
///             show(app)
///         }))
///         .plugin(tauri_plugin_deep_link::init())
///         .plugin(tauri_plugin_window_state::Builder::new().build())
///         .plugin(tauri_plugin_store::Builder::new().build())
///         .plugin(tauri_plugin_opener::init())
///         .setup(|app| { /* setup logic */ })
///         .run(tauri::generate_context!())
///         .expect("Forge Agents couldn't start");
/// }
/// ```
fn main() {
    tauri::Builder::default()
        // First: a second start (a link opened on Windows or Linux) hands its
        // link to this one, which the deep-link plugin passes on, and stops.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app)
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // Installers register forge-agents:// with the system; a build run
            // from here (tauri dev, an AppImage) registers itself. macOS reads
            // it from the app's Info.plist only.
            #[cfg(any(windows, target_os = "linux"))]
            app.deep_link().register_all()?;
            let opened = app.handle().clone();
            app.deep_link().on_open_url(move |_| show(&opened));

            // The window is described in tauri.conf.json ("create": false) and
            // made here, to hook its navigation.
            let config = app
                .config()
                .app
                .windows
                .first()
                .cloned()
                .ok_or("tauri.conf.json describes no window")?;
            let dev_url = app.config().build.dev_url.clone();
            let navigating = app.handle().clone();
            let opening = app.handle().clone();
            WebviewWindowBuilder::from_config(app, &config)?
                .on_navigation(move |url| {
                    if is_own(url, dev_url.as_ref()) {
                        return true;
                    }
                    open_outside(&navigating, url);
                    false
                })
                .on_new_window(move |url, _features| {
                    open_outside(&opening, &url);
                    NewWindowResponse::Deny
                })
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Forge Agents couldn't start");
}

/// Shows the main application window by unminimizing it, making it visible, and setting focus to it.
///
/// # Arguments
///
/// * `app` - A reference to the `AppHandle<Wry>` instance, which provides access to the application's state and windows.
///
/// # Behavior
///
/// This function attempts to retrieve the main window of the application by its identifier (`MAIN`).
/// If the window is found:
/// - It is unminimized if it was previously minimized.
/// - It is made visible.
/// - Focus is set to the window, bringing it to the foreground.
///
/// If any of the operations fail (e.g., due to platform limitations or invalid state),
/// the errors are ignored silently using the `_ = ...` assignment.
///
/// # Example
///
/// ```rust
/// show(&app);
/// ```
///
/// Note: Ensure the `MAIN` constant or identifier is properly defined and associated with the target window.
fn show(app: &AppHandle<Wry>) {
    if let Some(window) = app.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Determines if the given URL is considered "own" by the application.
///
/// This function is used to check whether a URL belongs to the application's domain or falls
/// within certain predefined schemes and conditions that are associated with trusted or internal
/// resources.
///
/// # Parameters
///
/// - `url`: A reference to a `Url` object representing the URL to be checked.
/// - `dev_url`: An optional reference to a `Url` object representing the base development URL
///   used during development mode.
///
/// # Returns
///
/// - `true` if the URL is considered "own" based on the following conditions:
///   - URLs with the scheme `tauri` (typically bundled files on macOS and Linux).
///   - URLs with the `http` or `https` scheme and a host of `tauri.localhost` (typically for Windows).
///   - URLs with the `about` or `blob` scheme (e.g., file previews, downloads).
///   - URLs that match the `origin` of the provided `dev_url` (when in development mode and
///     `dev_url` is provided).
/// - `false` otherwise.
///
/// # Notes
///
/// - This function makes use of the `tauri::is_dev` function to determine if the application
///   is running in development mode.
/// - The `is_some_and` method is used to match the `dev_url` only if it exists and its origin
///   matches the origin of the provided `url`.
///
/// # Example
///
/// ```rust
/// let url = Url::parse("http://tauri.localhost").unwrap();
/// let dev_url = Some(Url::parse("http://localhost:3000").unwrap());
/// assert!(is_own(&url, dev_url.as_ref()));
///
/// let unknown_url = Url::parse("http://example.com").unwrap();
/// assert!(!is_own(&unknown_url, dev_url.as_ref()));
/// ```
fn is_own(url: &Url, dev_url: Option<&Url>) -> bool {
    match url.scheme() {
        // Bundled files: tauri://localhost (macOS, Linux), http(s)://tauri.localhost (Windows).
        "tauri" => true,
        "http" | "https" if url.host_str() == Some("tauri.localhost") => true,
        // Documents the screen made itself (file previews, downloads). Not
        // data: URLs, which an answer's link could carry a whole page in.
        "about" | "blob" => true,
        _ => tauri::is_dev() && dev_url.is_some_and(|dev| dev.origin() == url.origin()),
    }
}

/// Opens a URL using the platform's default application associated with the URL's scheme.
///
/// # Parameters
/// - `app`: A reference to the `AppHandle<Wry>` instance, which provides access to the application
///          context and its capabilities, including the `opener` method.
/// - `url`: A reference to a `Url` object representing the URL to be opened.
///
/// # Behavior
/// - The function checks the URL's scheme and ensures it is one of the following:
///   - `http`
///   - `https`
///   - `mailto`
/// - If the URL's scheme does not match the allowed values, the function returns early without performing any action.
/// - If the URL scheme is valid, the function attempts to open the URL using the `app.opener().open_url` method.
/// - If the operation fails, an error message is printed to `stderr` specifying the URL and the error encountered.
///
/// # Examples
/// ```
/// use tauri::AppHandle;
/// use url::Url;
///
/// // Assuming `app` is a valid `AppHandle` instance and `url` is a properly constructed URL
/// let url = Url::parse("https://example.com").unwrap();
/// open_outside(&app, &url);
/// ```
///
/// # Errors
/// - Errors that occur during the `open_url` operation are logged to `stderr`, but no further action is taken by this function.
///
/// # Notes
/// - This function is platform-dependent and delegates the opening behavior to the platform-specific handlers for the provided URL scheme.
fn open_outside(app: &AppHandle<Wry>, url: &Url) {
    if !matches!(url.scheme(), "http" | "https" | "mailto") {
        return;
    }
    if let Err(error) = app.opener().open_url(url.as_str(), None::<&str>) {
        eprintln!("couldn't open {url}: {error}");
    }
}
