//! Check the actual Tauri updater without installing or changing user application data.
//! cargo run --release --example check-update -- <endpoint> <installed-version> [--download]
use tauri_plugin_updater::UpdaterExt;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let endpoint: url::Url = args.get(1).ok_or("missing endpoint")?.parse()?;
    let installed = args.get(2).ok_or("missing installed version")?.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .build(tauri::generate_context!())?;
    let updater = app.handle().updater_builder()
        .endpoints(vec![endpoint])?
        .version_comparator(move |_, release| release.version > installed.parse().expect("valid version"))
        .build()?;
    tauri::async_runtime::block_on(async {
        match updater.check().await? {
            Some(update) => {
                println!("available: {} {}", update.version, update.download_url);
                if args.iter().any(|arg| arg == "--download") {
                    let bytes = update.download(|_, _| {}, || {}).await?;
                    println!("downloaded and signature verified: {} bytes", bytes.len());
                }
            }
            None => println!("no update"),
        }
        Ok::<(), Box<dyn std::error::Error>>(())
    })
}
