//! 顶部原生菜单栏（文件 / 编辑 / 视图 / 帮助），对标 Codex 等桌面客户端。
//!
//! 菜单是原生控件、由 Rust 侧 `on_menu_event` 处理，**不经 WebView**——因此不受「远程源
//! 下 Tauri IPC 不可靠」影响，是驱动壳层能力（新建对话 / 设置服务器 / 检查更新）的可靠入口。
//! 编辑、全屏等用系统预定义项（`PredefinedMenuItem`），撤销/复制/粘贴等由系统直接作用于
//! 焦点输入框，无需自己接线。

use tauri::menu::{AboutMetadataBuilder, Menu, MenuEvent, MenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::brand;
use crate::Shared;

/// macOS 使用系统应用菜单；Windows/Linux 主窗口使用与标题同一行的 WebView 菜单。
#[allow(dead_code)]
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let config = crate::config::load(&app.state::<Shared>().config_dir);
    let hybrid =
        brand::HYBRID_ONLY || config.provision_mode() == crate::config::ProvisionMode::Dual;
    let local_capable = config.provision_mode() != crate::config::ProvisionMode::CloudOnly;
    #[cfg(target_os = "macos")]
    {
        let about = AboutMetadataBuilder::new()
            .name(Some(brand::NAME.to_string()))
            .version(Some(app.package_info().version.to_string()))
            .build();
        let application = SubmenuBuilder::new(app, brand::NAME)
            .about(Some(about))
            .separator()
            .text("server_config", "设置…")
            .text("check_update", "检查更新…")
            .separator()
            .services()
            .separator()
            .hide()
            .hide_others()
            .show_all()
            .separator()
            .quit()
            .build()?;

        let mut file = SubmenuBuilder::new(app, "文件")
            .item(&MenuItem::with_id(app, "new_window", "新建窗口", true, Some("CmdOrCtrl+Shift+N"))?)
            .text("new_chat", "新建对话");
        // 仅交付混合模式的包没有别的形态可切，不摆一个点了也没意义的入口。
        if !hybrid {
            file = file.text("run_mode", "运行模式…");
        }
        if local_capable {
            file = file.text("open_folder", "打开文件夹…");
        }
        if !hybrid {
            file = file.text("local_server", "本机服务…");
        }
        let file = file.separator().quit().build()?;

        let edit = SubmenuBuilder::new(app, "编辑")
            .undo()
            .redo()
            .separator()
            .cut()
            .copy()
            .paste()
            .select_all()
            .build()?;

        let view = SubmenuBuilder::new(app, "显示")
            .text("reload", "重新加载")
            .separator()
            .fullscreen()
            .build()?;

        let window = SubmenuBuilder::new(app, "窗口")
            .minimize()
            .maximize()
            .build()?;

        let help = SubmenuBuilder::new(app, "帮助")
            .text("website", "访问官网")
            .build()?;

        return Menu::with_items(app, &[&application, &file, &edit, &view, &window, &help]);
    }

    #[cfg(not(target_os = "macos"))]
    {
        let mut file = SubmenuBuilder::new(app, "文件")
            .item(&MenuItem::with_id(app, "new_window", "新建窗口", true, Some("CmdOrCtrl+Shift+N"))?)
            .text("new_chat", "新建对话");
        // 仅交付混合模式的包没有别的形态可切，不摆一个点了也没意义的入口。
        if !hybrid {
            file = file.text("run_mode", "运行模式…");
        }
        if local_capable {
            file = file.text("open_folder", "打开文件夹…");
        }
        if !hybrid {
            file = file
                .text("server_config", "设置服务器地址…")
                .text("local_server", "本机服务…");
        }
        let file = file.separator().quit().build()?;

        // 编辑：交给系统预定义项，直接作用于焦点输入框。
        let edit = SubmenuBuilder::new(app, "编辑")
            .undo()
            .redo()
            .separator()
            .cut()
            .copy()
            .paste()
            .select_all()
            .build()?;

        let view = SubmenuBuilder::new(app, "视图")
            .text("reload", "重新加载")
            .separator()
            .fullscreen()
            .build()?;

        let about = AboutMetadataBuilder::new()
            .name(Some(brand::NAME.to_string()))
            .version(Some(app.package_info().version.to_string()))
            .build();
        let help = SubmenuBuilder::new(app, "帮助")
            .text("check_update", "检查更新…")
            .text("website", "访问官网")
            .separator()
            .about(Some(about))
            .build()?;

        Menu::with_items(app, &[&file, &edit, &view, &help])
    }
}

/// 菜单事件分发。托盘的同名动作也复用这里（见 `build_tray`）。
pub fn handle(app: &AppHandle, event: MenuEvent) {
    dispatch(app, event.id.as_ref());
}

/// 按菜单项 id 执行动作。抽出来让托盘菜单也能直接调。
pub fn dispatch(app: &AppHandle, id: &str) {
    dispatch_for_window(app, id, &crate::active_desktop_label(app));
}

pub fn dispatch_for_window(app: &AppHandle, id: &str, label: &str) {
    match id {
        "new_window" => crate::new_desktop_window(app),
        // 新建对话：主窗口整页导航回首页（= 全新对话就绪态）。
        "new_chat" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "open_folder" => {
            let shared = app.state::<Shared>();
            let cfg = crate::config::load(&shared.config_dir);
            if cfg.provision_mode() == crate::config::ProvisionMode::CloudOnly {
                return;
            }
            let app = app.clone();
            let label = label.to_string();
            app.clone().dialog().file().pick_folder(move |picked| {
                let Some(path) = picked.and_then(|p| p.into_path().ok()) else { return };
                if let Some(window) = app.get_webview_window(&label) {
                    let detail = serde_json::to_string(&path.to_string_lossy()).unwrap();
                    let _ = window.eval(format!(
                        "if(window.dispatchEvent(new CustomEvent('hugagent:open-project-folder',{{detail:{detail},cancelable:true}}))){{sessionStorage.setItem('hugagent:pending-project-folder',{detail});window.location.replace('/');}}"
                    ));
                }
            });
        }
        "server_config" => crate::open_server_config_in(app, label),
        // 运行模式选择页（本机 / 云端 / 双模式）——初始化选型的再次入口。
        "run_mode" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/__desktop/init?manage=1')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "local_server" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/__desktop/setup?manage=1')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "reload" => {
            if let Some(w) = app.get_webview_window(label) {
                let _ = w.eval("window.location.reload()");
            }
        }
        "check_update" => {
            let update_base = app.state::<Shared>().update_base.clone();
            crate::update::check_and_install(app.clone(), update_base, false);
        }
        "website" => {
            let shared = app.state::<Shared>();
            let target = if brand::WEBSITE_URL.is_empty() {
                shared.server_base.clone()
            } else {
                brand::WEBSITE_URL.to_string()
            };
            let _ = app.opener().open_url(target, None::<String>);
        }
        _ => {
            // about / 系统预定义项由系统自行处理，这里无需接管；未知 id 兜底提示。
            if id == "about" {
                app.dialog()
                    .message(format!(
                        "{} 桌面客户端\n当前版本：{}",
                        brand::NAME,
                        app.package_info().version
                    ))
                    .title("关于")
                    .blocking_show();
            }
        }
    }
}
